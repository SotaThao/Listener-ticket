"""Pipeline-owned assistant wiring. Disabled pipelines never instantiate this module.

Shadow admission is synchronous and bounded; model work starts on the next sweep,
in a separate worker after foreground commits. Queue, attempts and breakers are
in memory (lost on restart). The worker has no GitHub/Telegram writing interface.
"""
import copy
import hashlib
import json
import logging
import threading
import time
from collections import OrderedDict
from dataclasses import replace
from types import SimpleNamespace

from agentfleet_common.ai_transport import redact, read_key, ProviderError

from . import assistant, classification_log, router_transport
from .assistant_contract import PHASE_DECIDE, GroupDecision
from .assistant_errors import ToolDenied, ToolUnavailable
from .assistant_github import GitHubReader
from .assistant_local import LocalReader
from .assistant_pack import AssistantPackError
from .assistant_loop import AssistantQueue, AttemptTracker, CircuitBreaker, DailyCap, LINKS, RateLimiter
from .assistant_tools import ToolBox

LOG = logging.getLogger(__name__)
SHUTDOWN_JOIN_S = 5.0  # systemd/tg-triage.service TimeoutStopSec=150
LIVE_DRAIN_S = 20
LIVE_GRANT_LEASE_S = 2 * LIVE_DRAIN_S
TOPIC_PROPOSAL_CACHE_SIZE = 256


def _attempt_outcome(result):
    if result is None or result.outcome == 'infra' or result.infra:
        return 'infra'
    return 'ok' if result.outcome == 'ok' else 'failed'


def _batch_outcome(result, parts):
    outcomes = [_attempt_outcome(part) for part in parts]
    if 'failed' in outcomes: return 'failed'
    if 'infra' in outcomes: return 'infra'
    return 'ok' if result is not None else 'failed'


def batch_fingerprint(sources):
    keys = sorted([m.chat_id, m.message_id] for m in sources)
    return hashlib.sha256(classification_log._json(keys).encode('utf-8')).hexdigest()


def _topic_evidence(user):
    return hashlib.sha256(classification_log._json(user).encode('utf-8')).digest()


def redact_data(text):
    """Use CloudProvider's SAME redactor, preserving typed IDs in JSON packets."""
    try:
        value = json.loads(text)
    except (ValueError, TypeError):
        return redact(text)
    if not isinstance(value, (dict, list)):
        return redact(text)
    def clean(item):
        if isinstance(item, str): return redact(item)
        if isinstance(item, list): return [clean(v) for v in item]
        if isinstance(item, dict): return {k: clean(v) for k, v in item.items()}
        return item
    return json.dumps(clean(value), ensure_ascii=False)


def _decide_payload(data):
    """Read the rules_extraction packet and its separately serialized pixels."""
    body = data.get('body', data)
    if 'messages' in body:
        user = body['messages'][1]
        return json.loads(user['content']), user.get('images', ())
    packet = data.get('packet', data)
    if isinstance(packet, str):
        packet = json.loads(packet)
    return {key: value for key, value in packet.items()
            if key not in ('images', 'image_paths', 'documents', 'read_context')}, ()


def _decide_outcomes(result, sources, repo, recent_targets):
    groups = result.decisions or ()
    violations = result.violations or ()
    recent = [{key: row[key] for key in ('number', 'source_message_id', 'received_at', 'evidence')}
              for row in recent_targets.values()]
    projected = []
    for index, group in enumerate(groups):
        projected.append(dict(
            message_ids=list(group.message_ids), state=group.state, rule=group.rule,
            rule_text=group.rule_text, template=group.template,
            ticket_ref=(f"https://github.com/{repo}/issues/{group.target['number']}"
                        if group.target and group.targets and group.state in ('APPEND', 'APPEND_CLOSED')
                        and group.target['basis'] != 'recent'
                        and result.outcome == 'ok' else None),
            confidence=group.confidence, reasoning_summary=redact(group.reasoning_summary),
            violations=[dict(guard=v.guard, detail=redact(v.detail)) for v in violations
                        if v.group_index in (None, index)],
            related=list(dict.fromkeys(
                [ref['number'] for ref in group.refs] + list(group.related) +
                ([group.target['number']] if group.target and group.targets and group.relation
                 and group.state not in ('APPEND', 'APPEND_CLOSED') else []))),
            question_block=group.question['block_id'] if group.question else None))
        if recent:
            projected[-1]['recent_ticket_candidates'] = recent
            projected[-1]['target'] = dict(group.target) if group.target else None
            if group.question_candidate_numbers:
                projected[-1]['question_candidate_numbers'] = list(group.question_candidate_numbers)
    if not groups and (violations or recent):
        projected.append(dict(message_ids=[m.message_id for m in sources], state='HOLD',
            rule=None, rule_text=None, template=None, ticket_ref=None,
            confidence=None, reasoning_summary=None, related=[], question_block=None,
            violations=[dict(guard=v.guard, detail=redact(v.detail)) for v in violations]))
        if recent:
            projected[-1]['recent_ticket_candidates'] = recent
    return classification_log._json(projected)


class _BreakerView:
    def __init__(self, runtime, breaker):
        self.runtime, self.breaker = runtime, breaker

    def allow(self):
        return self.breaker.allow()

    def record_success(self):
        self.breaker.record_success()

    def cancel_probe(self):
        self.breaker.cancel_probe()

    def record_failure(self, kind):
        # Local admission failure is not a failure of an upstream link.
        if self.runtime._budget_blocked:
            self.breaker.cancel_probe()
            return
        self.breaker.record_failure(kind)
        if kind == 'auth':
            LOG.warning('Assistant auth breaker open link=%s cooldown_s=3600', self.breaker.name)
            # Shadow's audit-only boundary also applies to auth notices.
            if self.runtime.settings.mode == 'live' and self.runtime.ops_notice:
                try:
                    self.runtime.ops_notice('Assistant auth breaker open: ' + self.breaker.name)
                except Exception as error:
                    LOG.warning('Assistant ops notice failed exception=%s', type(error).__name__)


class AssistantExtractor:
    """One instance per pipeline/process; one serialized turn, one ToolBox per run."""
    def __init__(self, config, store, pack_text, *, chat=None, clock=time.monotonic,
                 wall_clock=time.time, ops_notice=None, core_pack=None):
        self.config, self.settings, self.store = config, config.assistant, store
        self.pack_text, self.clock, self.ops_notice = pack_text, clock, ops_notice
        core_enabled = getattr(self.settings, 'pack_mode', 'full') == 'core'
        self.core_pack = core_pack if core_enabled else None
        if core_enabled and self.core_pack is None:
            raise AssistantPackError('core mode requires the verified assistant core bundle')
        self.wall_clock = wall_clock
        self.chat = chat or (lambda model, messages, **kw: router_transport.chat(
            self.settings.router_base_url, self.settings.router_key, model, messages, **kw))
        self.breakers = {name: CircuitBreaker(name, clock=clock) for name in LINKS}
        self.attempts = AttemptTracker(clock=clock)
        self.limiter = RateLimiter(self.settings.max_calls_per_minute, clock=clock)
        self.cap = DailyCap(self.settings.daily_cap, clock=wall_clock)
        self.queue = AssistantQueue(clock=clock, max_size=self.settings.queue_max)
        self._jobs = {}
        self._queue_lock = threading.Lock()
        self._turn_lock = threading.Lock()
        self._worker = None
        self._budget_blocked = False
        self._outage = False
        self._blocked_until = 0
        self._budget_reason = None
        self._closing = False
        self._live_waiting = set()
        self._live_ready = None
        self._live_ready_at = None
        self._live_grant_at = 0
        self._live_admitted = set()
        self._topic_proposals = OrderedDict()
        if self.settings.enabled and not self.settings.router_key:
            LOG.warning('Assistant unavailable: router key is not configured')

    def waiting(self, sources, *, admit=True, fingerprint=None):
        fp = fingerprint or batch_fingerprint(sources)
        if self.settings.mode != 'live':
            return self.attempts.status(fp), self.attempts.due(fp) and self.clock() >= self._blocked_until
        with self._queue_lock:
            now = self.clock()
            ready = self._live_ready
            if ready is not None:
                state = self.attempts.status(ready)
                discard = state in ('NEW', 'DONE', 'ESCALATED') or ready not in self._live_waiting
                expired = self._live_ready_at is None or now >= self._live_ready_at + LIVE_GRANT_LEASE_S
                if discard or expired:
                    self._live_ready = self._live_ready_at = None
                    if discard or not self.queue.push(ready):
                        self._live_waiting.discard(ready)
            # A pre-reservation peek can be blocked before ever joining the FIFO.
            # It still belongs to the same bounded infrastructure wait.
            if fp in self._live_waiting or self._outage or now < self._blocked_until:
                self.attempts.wait_infra(fp)
            status = self.attempts.status(fp)
            due = self.attempts.due(fp) and now >= self._blocked_until
            if not due or not admit:
                return status, due
            if fp in self._live_admitted:
                return status, True
            if not self._outage and not self._live_waiting:
                self._live_admitted.add(fp)
                return status, True
            if fp not in self._live_waiting:
                status = self.attempts.wait_infra(fp)
                if status == 'ESCALATED': return status, False
                if not self.queue.push(fp): return status, False
                self._live_waiting.add(fp)
            active = ('gemini', 'gpt', 'deepseek') if self.config.model.provider == 'deepseek' and self.config.model.cloud_consent else ('gemini', 'gpt')
            if all(self.breakers[n].state == 'open' for n in active):
                return status, False
            if self._outage:
                self.queue.mark_recovered()
                self._outage = False
            if self.clock() < self._live_grant_at:
                return status, False
            if self._live_ready is None:
                self._live_ready = self.queue.pop_ready()
                self._live_ready_at = now if self._live_ready is not None else None
                if self._live_ready is not None and not self.attempts.due(self._live_ready):
                    if self.attempts.status(self._live_ready) in ('DONE', 'ESCALATED'):
                        self._live_waiting.discard(self._live_ready)
                    else:
                        self.queue.push(self._live_ready)
                    self._live_ready = None
                    self._live_ready_at = None
            if self._live_ready != fp:
                return status, False
            self._live_ready = None
            self._live_ready_at = None
            self._live_waiting.discard(fp)
            self._live_grant_at = self.clock() + LIVE_DRAIN_S
            self._live_admitted.add(fp)
            return status, True

    def _queue_live(self, fp, outcome):
        if self.settings.mode == 'live' and outcome == 'infra':
            with self._queue_lock:
                self._outage = True
                self.attempts.wait_infra(fp)
                if fp not in self._live_waiting and self.queue.push(fp):
                    self._live_waiting.add(fp)

    def evaluate(self, evaluate, snapshot, provider, sources, *, topic_id=None, ref_sources=None,
                 retry_fingerprint=None, **kwargs):
        """Keep the caller's pure decision code and freeze reply context for shadow."""
        current = classification_log.CURRENT.get()
        topic_refs = ref_sources is not None or (current is not None and current.values.get('id_space') == 'topic_ref')
        if topic_refs and self.settings.mode != 'live':
            return evaluate(snapshot, provider, sources, **kwargs)
        fp = current.values['batch_fingerprint'] if current is not None else batch_fingerprint(sources)
        if topic_refs:
            keys = (tuple((ref, m.chat_id, m.message_id) for ref, m in ref_sources)
                    if ref_sources is not None else current.values.get('_source_keys', ()))
            fp = classification_log.topic_fingerprint(topic_id or (current and current.values.get('topic_id')), keys)
        proposal_fp = fp
        fp = retry_fingerprint or fp
        if self.settings.mode == 'live':
            status = self.attempts.status(fp)
            if status == 'DONE' and not topic_refs:
                # A valid proposal need not have passed foreground reconciliation.
                # Preserve dedupe; let the existing extractor spend later attempts.
                return evaluate(snapshot, provider, sources, **kwargs)
            if status == 'ESCALATED':
                return None  # No refund: foreground retries must reach escalation.
        targets = {}
        original = kwargs.get('reply_target_for')
        if original is not None:
            def target(members):
                value = original(members)
                for m in members:
                    targets[m.message_id] = value
                return value
            kwargs['reply_target_for'] = target
        jobs = []
        live_begun = False
        live_results = []
        accepted_proposal = None
        deadline = self.clock() + self.settings.turn_deadline_s

        def extract(p, snap, members, body, parse, **data):
            nonlocal live_begun, accepted_proposal
            job = dict(provider=p, snapshot=snap, sources=tuple(members),
                       body=copy.deepcopy(body), parse=parse, data=data)
            current = classification_log.CURRENT.get()
            if current is not None:
                job['fingerprint'] = current.values['batch_fingerprint']
                job['id_space'] = current.values['id_space']
                job['topic_id'] = current.values.get('topic_id')
            if topic_refs:
                if current is not None:
                    current.replaced = True
                job.update(id_space='topic_ref', topic_id=topic_id or job.get('topic_id'), source_keys=keys)
                if (len(keys) != len(members) or
                        {(ref, chat_id) for ref, chat_id, _ in keys} != {(m.message_id, m.chat_id) for m in members}):
                    return None
                if status == 'DONE':
                    cached = self._topic_proposals.get(proposal_fp)
                    if cached is None or cached[0] != _topic_evidence(body['messages'][1]):
                        return None
                    self._topic_proposals.move_to_end(proposal_fp)
                    return copy.deepcopy(cached[1])
            if self.settings.mode == 'shadow':
                if len(members) != len(sources):
                    job['fingerprint'] = hashlib.sha256((fp + batch_fingerprint(members)).encode()).hexdigest()
                result = p._ask(body, parse, attempts=1)
                jobs.append(job)
                return result
            if not live_begun:
                _, admitted = self.waiting(sources, fingerprint=fp)
                if not admitted:
                    self._refund()
                    return None
                with self._queue_lock:
                    self._live_admitted.discard(fp)
                if not self.attempts.begin(fp):
                    if self.attempts.status(fp) not in ('DONE', 'ESCALATED'):
                        self._refund()
                    return None
                live_begun = True
            job.update(fingerprint=proposal_fp, deadline=deadline)
            if retry_fingerprint is not None:
                job['retry_fingerprint'] = retry_fingerprint
            result = self.run(job, decision_kwargs=kwargs, track_attempt=False)
            live_results.append((job, result))
            if result is None:
                return None
            groups = parse(json.dumps(result.proposal)) if result.proposal else None
            if topic_refs and groups is not None:
                validate = getattr(type(p), 'accept_extraction', None)
                if validate is not None:
                    groups = validate(p, body, groups)
                if groups is not None:
                    # Keep only a digest of the evidence packet alongside the parsed facts.
                    evidence = _topic_evidence(body['messages'][1])
                    accepted_proposal = (evidence, copy.deepcopy(groups))
            return groups

        try:
            result = evaluate(snapshot, provider, sources, extract=extract, **kwargs)
        except BaseException:
            if live_begun:
                outcome = _batch_outcome(None, [r for _, r in live_results] + [None])
                self.attempts.finish(fp, outcome)
                self._queue_live(fp, outcome)
                if outcome == 'infra': self._refund()
            raise
        if live_begun:
            outcome = _batch_outcome(result, [r for _, r in live_results])
            if topic_refs and outcome == 'ok' and accepted_proposal is not None:
                self._topic_proposals[proposal_fp] = accepted_proposal
                if len(self._topic_proposals) > TOPIC_PROPOSAL_CACHE_SIZE:
                    self._topic_proposals.popitem(last=False)
            self.attempts.finish(fp, outcome)
            self._queue_live(fp, outcome)
            if outcome == 'infra': self._refund()
            retry = self.attempts.retry_at(fp)
            retry = (self.store.clock().timestamp() + max(0, max(retry, self._blocked_until) - self.clock())) if retry is not None else None
            with self.store._lock, self.store.connection:
                for job, _ in live_results:
                    if 'run_id' in job:
                        self.store.connection.execute('UPDATE classification_runs SET retry_at=? WHERE run_id=?',
                                                      (retry, job['run_id']))
        if self.settings.mode == 'shadow':
            frozen = dict(kwargs)
            if original is not None:
                frozen['reply_target_for'] = lambda members: next(
                    (targets[m.message_id] for m in members if m.message_id in targets), 'none')
            for job in jobs:
                self.enqueue(job, frozen, ready=False)
        return result

    def enqueue(self, job, decision_kwargs, *, ready=True):
        fp = job.get('fingerprint') or batch_fingerprint(job['sources'])
        with self._queue_lock:
            if fp in self._jobs or not self.attempts.due(fp) or not self.queue.push(fp):
                return False
            job['decision_kwargs'] = decision_kwargs
            job['ready'] = ready
            self._jobs[fp] = job
        return True

    def publish(self):
        """Called by the serialized foreground owner only after its commits."""
        with self._queue_lock:
            for job in self._jobs.values():
                job['ready'] = True

    def drain(self, *, background=True):
        """Start at most one worker; never wait for model I/O on the sweep thread."""
        if self._closing:
            return
        if background:
            with self._queue_lock:
                if self._worker is not None and self._worker.is_alive():
                    return
                if not self._jobs:
                    return
                self._worker = threading.Thread(target=self._drain_one, daemon=True,
                                                name='assistant-shadow')
                self._worker.start()
        else:
            self._drain_one()

    def close(self):
        """Leave time for audit flush/store close within systemd's stop budget."""
        self._closing = True
        worker = self._worker
        if worker is not None:
            worker.join(timeout=SHUTDOWN_JOIN_S)
            if worker.is_alive():
                LOG.warning('Assistant shadow shutdown wait expired; continuing shutdown')

    def _drain_one(self):
        with self._queue_lock:
            if self._closing or self.clock() < self._blocked_until:
                return
            active = ('gemini', 'gpt', 'deepseek') if self.config.model.provider == 'deepseek' and self.config.model.cloud_consent else ('gemini', 'gpt')
            if all(self.breakers[n].state == 'open' for n in active):
                self._outage = True
                return
            if self._outage:
                self.queue.mark_recovered()
                self._outage = False
            fp = self.queue.pop_ready()
            if fp is None:
                return
            job = self._jobs.get(fp)
            if job is None:
                return
            if self.attempts.status(fp) in ('DONE', 'ESCALATED'):
                self._jobs.pop(fp, None)
                return
            if not job.get('ready', True) or not self.attempts.due(fp):
                self.queue.push(fp)
                return
        try:
            self.run(job, decision_kwargs=job['decision_kwargs'])
        except Exception as error:
            LOG.warning('Assistant shadow deferred exception=%s', type(error).__name__)
        finally:
            with self._queue_lock:
                if not self._closing and self.attempts.status(fp) in ('RETRY', 'WAIT_INFRA'):
                    self.queue.push(fp)
                else:
                    self._jobs.pop(fp, None)

    def _acquire(self):
        if not self.limiter.try_acquire():
            self._budget_reason = 'rate_limited'
            self._blocked_until = max(self._blocked_until, self.clock() + 20)
            self._budget_blocked = True
            raise router_transport.RouterError('rate_limited')
        if not self.cap.try_acquire():
            self._budget_reason = 'daily_cap'
            wall = self.wall_clock()
            midnight = ((wall + 7*3600) // 86400 + 1) * 86400 - 7*3600
            self._blocked_until = self.clock() + midnight - wall
            self._budget_blocked = True
            raise router_transport.RouterError('rate_limited')

    def _refund(self):
        """Infrastructure/admission waits must not use the foreground retry counter."""
        from .thinking import CURRENT
        turn = CURRENT.get()
        if turn is not None:
            topic = (turn.key.removeprefix('rules:followup:')
                     if turn.key.startswith('rules:followup:') else turn.root_key)
            with self.store._lock, self.store.connection:
                self.store.connection.execute('''UPDATE rules_topics SET attempts=max(0,attempts-1),
                    attempt_reserved=0 WHERE topic_id=? AND attempt_reserved=1''', (topic,))

    def _deepseek(self, job, deadline, usage, start_call):
        """Bind the zero-arg callback to ONE bounded hosted request; never Ollama."""
        c = self.config.model
        if c.provider != 'deepseek' or not c.cloud_consent:
            return None
        called = False

        def call():
            nonlocal called
            if self._closing:
                raise ConnectionError('Assistant is stopping')
            if called:
                raise ValueError('DeepSeek already called')
            called = True
            from .rules_extraction import partition_and_extract
            from .thinking import CLOUD_REQUEST_BUDGET_BYTES
            from .thinking_transport import complete
            bodies = []
            partition_and_extract(job['provider'], job['snapshot'], job['sources'],
                documents=job['data']['documents'], read_context=job['data']['read_context'],
                allow_mixed=len({m.chat_id for m in job['sources']}) > 1,
                limit=CLOUD_REQUEST_BUDGET_BYTES, body_out=bodies)
            if not bodies:
                raise router_transport.RouterError('too_large')
            body = bodies[0]
            from .provider import CloudProvider
            messages = CloudProvider._cloud_messages(job['provider'], body['messages'])
            try:
                key = read_key(c.key_file, c.provider + '|' + c.base_url, group_name='tglistener')
            except ProviderError:
                raise ConnectionError('DeepSeek credential unavailable') from None
            if not key:
                raise ConnectionError('DeepSeek credential unavailable')
            timeout = min(self.settings.deepseek_timeout_s, deadline - self.clock())
            if timeout <= 0:
                raise assistant.TurnBudgetExhausted()
            self._acquire()
            start_call(timeout)
            started = self.clock()
            try:
                value = complete('deepseek', c.base_url, c.name, messages, body['format'],
                    key=key, timeout=timeout, max_output=c.max_output, usage_sink=usage.update)
            except ProviderError as error:
                kind = {'auth_failed': 'auth', 'invalid_json': 'schema',
                        'rate_limited': 'rate_limited', 'input_too_large': 'too_large',
                        'response_too_large': 'too_large'}.get(error.code, 'transient')
                if error.code == 'invalid_json':
                    usage['schema_error_kind'] = 'invalid_json'
                raise router_transport.RouterError(kind) from None
            if self.clock() >= deadline or self.clock() - started >= timeout:
                raise assistant.TurnBudgetExhausted()
            return value
        return call

    def run(self, job, *, decision_kwargs=None, track_attempt=True):
        return self._run(job, decision_kwargs=decision_kwargs, track_attempt=track_attempt)

    def decide(self, job) -> tuple[list[GroupDecision] | None, int | None, str]:
        """Admit one guarded decision turn and return only executable groups."""
        if (not self.settings.enabled or self.settings.mode != 'live'
                or self.settings.authority != 'decide'
                or (self.settings.decide_chat_ids and job['chat_id'] not in self.settings.decide_chat_ids)
                or job['id_space'] != 'message'):
            raise ValueError('assistant decide is not enabled for this batch')
        result = self._run(job, guard_ctx=job['guard_ctx'])
        if result is None:
            status = self.attempts.status(job['fingerprint'])
            return None, None, 'model_error' if status in ('DONE', 'ESCALATED') else 'infra'
        run_id = job['run_id']
        if result.outcome == 'ok':
            return [replace(group, run_id=run_id) for group in result.decisions], run_id, 'done'
        if result.outcome == 'guard':
            return None, run_id, 'guard'
        return None, run_id, 'infra' if _attempt_outcome(result) == 'infra' else 'model_error'

    def _run(self, job, *, decision_kwargs=None, track_attempt=True, guard_ctx=None):
        if self._closing:
            return None
        deciding = guard_ctx is not None
        run_fp = job.get('fingerprint') or batch_fingerprint(job['sources'])
        fp = job.get('retry_fingerprint', run_fp)
        if not self._turn_lock.acquire(blocking=False):
            if self.settings.mode == 'live':
                self._refund()
            return None
        local = None
        begun = False
        try:
            if deciding:
                status, admitted = self.waiting(job['sources'], fingerprint=fp)
                if not admitted:
                    if status not in ('DONE', 'ESCALATED'):
                        self._refund()
                    return None
                with self._queue_lock:
                    self._live_admitted.discard(fp)
            if track_attempt and not self.attempts.begin(fp):
                if self.settings.mode == 'live' and self.attempts.status(fp) not in ('DONE', 'ESCALATED'):
                    self._refund()
                return None
            begun = track_attempt
            self._budget_blocked = False
            self._budget_reason = None
            started = self.clock()
            sources, snapshot = job['sources'], job['snapshot']
            values = dict(created_at=self.store.clock().timestamp(), turn_key='assistant:' + run_fp,
                phase='assistant_' + self.settings.mode, mode=self.settings.mode,
                batch_fingerprint=run_fp, chat_id=sources[0].chat_id,
                message_ids=classification_log._json([m.message_id for m in sources]),
                id_space=job.get('id_space', 'message'), topic_id=job.get('topic_id'),
                rules_fingerprint=snapshot.fingerprint,
                rules_version=getattr(self.store, 'assistant_pack_fingerprint', None),
                final_state='HOLD', error_class='infra')
            if values['id_space'] == 'message':
                values['_source_keys'] = classification_log.source_keys(sources)
            topic_refs = values['id_space'] == 'topic_ref'
            topic_sources = None
            if topic_refs:
                values['_source_keys'] = job['source_keys']
                values['image_ids'] = classification_log._json(sorted(job['data']['images'] or {}))
                topic_sources = {ref: (chat_id, mid) for ref, chat_id, mid in job['source_keys']}
            if deciding:
                values.update(phase=PHASE_DECIDE, rules_version=assistant.decide_prefix_hash(
                                  self.pack_text, core_pack=self.core_pack),
                              code_precheck_json=classification_log._json(job['code_precheck']),
                              log_thread_id=job['log_thread_id'], outcomes_json='[]')
            with self.store._lock:
                if self._closing: return None
                with self.store.connection:
                    run_id = classification_log._insert_run(self.store.connection, values)
            job['run_id'] = run_id
            if deciding:
                packet, pixels = _decide_payload(job['data'])
                local_images = job['data'].get('image_paths', job['data'].get('images', {}))
            else:
                packet = json.loads(job['body']['messages'][1]['content'])
                pixels = job['body']['messages'][1].get('images', [])
                local_images = job['data']['images']
                if topic_refs and len(sources) == 1:
                    packet['sources'][0]['reply_to_id'] = None
            local = LocalReader(self.config.db_path, sources[0].chat_id,
                batch_message_ids=[m.message_id for m in sources],
                images={str(k): v for k, v in local_images.items()} if isinstance(local_images, dict) else {},
                pack_text=self.pack_text, core_pack=self.core_pack if deciding else None,
                topic_sources=topic_sources)
            if deciding and not pixels:
                pixels = {}
                for image_id in packet.get('images_order', ()):
                    try:
                        pixels[image_id] = local.get_image(str(image_id))['bytes_base64']
                    except (ToolDenied, ToolUnavailable):
                        pass
                packet['images_order'] = list(pixels)
            toolbox = ToolBox(GitHubReader(self.settings.github_token, self.settings.github_repos),
                local, store=self.store, run_id=run_id, max_calls=self.settings.max_tool_calls,
                max_rule_lookups=self.settings.max_rule_lookups if deciding and self.core_pack is not None else None,
                clock=self.clock, wall_clock=self.wall_clock)
            usage = {'calls': 0}
            call_timing = None

            def start_call(timeout):
                nonlocal call_timing
                usage['calls'] += 1
                call_timing = (self.wall_clock(), self.clock(), timeout)

            def record_call(link, model, reply, *, error_class=None, schema_error_kind=None,
                            content_text=None, finish_reason=None):
                nonlocal call_timing
                timing, call_timing = call_timing, None
                if timing is None or self._closing:
                    return  # Local admission failures do not make a provider call.
                started_at, started_monotonic, timeout = timing
                try:
                    details = dict(link=link, model=model, started_at=started_at, timeout_s=timeout,
                                   latency_ms=max(0, int((self.clock() - started_monotonic) * 1000)),
                                   error_class=error_class, schema_error_kind=schema_error_kind,
                                   finish_reason=finish_reason)
                    if reply is not None:
                        content_text = reply.content
                        calls = [dict(id=classification_log.bounded_capture(c.id),
                                      name=classification_log.bounded_capture(c.name),
                                      arguments=classification_log.bounded_capture(c.arguments))
                                 for c in reply.tool_calls]
                        details.update(model=getattr(reply, 'model', None) or model,
                                       latency_ms=getattr(reply, 'latency_ms', details['latency_ms']),
                                       finish_reason=reply.finish_reason,
                                       prompt_tokens=getattr(reply, 'prompt_tokens', 0),
                                       completion_tokens=getattr(reply, 'completion_tokens', 0),
                                       reasoning_tokens=getattr(reply, 'reasoning_tokens', None),
                                       cached_tokens=getattr(reply, 'cached_tokens', 0),
                                       reasoning_text=classification_log.bounded_capture(getattr(reply, 'reasoning_text', None)),
                                       tool_calls_json=json.dumps(calls, ensure_ascii=False))
                    elif link == 'deepseek' and not deciding:
                        details.update(model=self.config.model.name,
                                       prompt_tokens=usage.get('prompt_tokens', 0),
                                       completion_tokens=usage.get('completion_tokens', 0),
                                       cached_tokens=usage.get('cache_hit_tokens', 0),
                                       schema_error_kind=schema_error_kind or usage.get('schema_error_kind'))
                    classification_log.record_model_call(self.store, run_id, usage['calls'],
                        content_text=classification_log.bounded_capture(content_text), **details)
                except Exception as error:
                    LOG.warning('Assistant capture failed exception=%s', type(error).__name__)

            def chat(model, messages, **kw):
                if self._closing:
                    raise ConnectionError('Assistant is stopping')
                if not self.settings.router_key:
                    raise ConnectionError('Router credential unavailable')
                self._acquire()
                start_call(kw['timeout'])
                reply = self.chat(model, messages, **kw)
                if self._closing:
                    raise ConnectionError('Assistant is stopping')
                return reply

            def packed_deepseek(model, messages, **kw):
                if self._closing:
                    raise ConnectionError('Assistant is stopping')
                c = self.config.model
                try:
                    key = read_key(c.key_file, c.provider + '|' + c.base_url, group_name='tglistener')
                except ProviderError:
                    raise ConnectionError('DeepSeek credential unavailable') from None
                if not key:
                    raise ConnectionError('DeepSeek credential unavailable')
                self._acquire()
                start_call(kw['timeout'])
                reply = router_transport.deepseek_chat(c.base_url, key, model, messages,
                                                       max_output=c.max_output, **kw)
                if self._closing:
                    raise ConnectionError('Assistant is stopping')
                return reply

            payload = list(packet['sources']) if deciding else packet['sources']
            if deciding:
                visible = frozenset(packet.get('images_order', ()))
                payload = [dict(row, pixels_available=row['message_id'] in visible) for row in payload]
                guard_ctx = replace(guard_ctx, seen_image_ids=guard_ctx.seen_image_ids & visible)
                packet['decision_context'] = dict(
                    write_repo=guard_ctx.gh_repo, append_confidence=guard_ctx.append_confidence,
                    question_block_ids=sorted(guard_ctx.question_block_ids),
                    reply_targets=[dict(message_id=mid, number=number, state=state)
                                   for mid, (number, state) in sorted(guard_ctx.reply_targets.items())],
                    recent_ticket_candidates=[
                        {key: value for key, value in row.items() if key != 'authority_message_id'}
                        for row in guard_ctx.recent_targets.values()])
            # Documents and historical context remain explicitly untrusted source data.
            if payload:
                payload[0] = dict(payload[0], batch_context={k: v for k, v in packet.items()
                                  if k not in ('sources', 'source_ids', 'images_order')})
                if topic_refs:
                    payload[0]['batch_context']['id_space'] = 'topic_ref'
            from .provider import _image_mime
            image_pairs = (pixels.items() if isinstance(pixels, dict)
                           else zip(packet.get('images_order', ()), pixels))
            images = {str(mid): encoded if deciding and encoded.startswith('data:')
                      else 'data:' + (_image_mime(encoded) or 'image/png') + ';base64,' + encoded
                      for mid, encoded in image_pairs}
            deadline = min(started + self.settings.turn_deadline_s, job.get('deadline', float('inf')))
            decide_kwargs = {}
            if deciding:
                decide_kwargs.update(batch=sources, guard_ctx=guard_ctx, core_pack=self.core_pack)
            if deciding or topic_refs:
                if self.config.model.provider == 'deepseek' and self.config.model.cloud_consent:
                    decide_kwargs.update(deepseek_link=assistant.Link('deepseek', self.config.model.name,
                        self.settings.deepseek_timeout_s), decide_deepseek=packed_deepseek)
            links = [assistant.Link(n, m, t) for n, m, t in zip(('gemini', 'gpt'), self.settings.models,
                         (self.settings.gemini_timeout_s, self.settings.gpt_timeout_s))]
            if topic_refs:
                links.reverse()
            result = assistant.run_turn(sources_payload=payload, images=images, pack_text=self.pack_text,
                links=links,
                chat=chat, toolbox=toolbox, breakers={n: _BreakerView(self, b) for n, b in self.breakers.items()},
                redact=redact_data, deepseek=None if deciding or topic_refs else self._deepseek(job, deadline, usage, start_call),
                images_carry_data=any(m.has_image for m in sources),
                deadline_s=max(0, deadline - self.clock()), clock=self.clock, record_call=record_call,
                topic_refs=topic_refs, **decide_kwargs)
            result.model_calls = usage['calls']
            if self._closing:
                return None  # A late worker must not write after store.close().
            if self._budget_blocked:
                result.outcome, result.error_class = 'infra', self._budget_reason
                result.infra = True
            if track_attempt:
                self.attempts.finish(fp, 'ok' if result.outcome == 'guard' else _attempt_outcome(result))
                self._queue_live(fp, _attempt_outcome(result))
            begun = False
            if _attempt_outcome(result) == 'infra' and self.settings.mode == 'live':
                self._refund()
            projected = {}
            if deciding:
                states = result.decisions if result.outcome == 'ok' else []
                projected['outcomes_json'] = _decide_outcomes(
                    result, sources, guard_ctx.gh_repo, guard_ctx.recent_targets)
                template = states[0].template if len(states) == 1 else None
                if template:
                    template += '.md'
            else:
                states, template, projected = self._evaluate_proposal(job, result, decision_kwargs)
            values = dict(provider='deepseek' if result.link == 'deepseek' else '9router',
                model=result.model or (self.config.model.name if result.link == 'deepseek' else None),
                model_calls=result.model_calls, tokens_in=result.prompt_tokens + usage.get('prompt_tokens', 0),
                tokens_out=result.completion_tokens + usage.get('completion_tokens', 0),
                cache_hit_tokens=result.cached_tokens + usage.get('cache_hit_tokens', 0),
                reasoning_tokens=result.reasoning_tokens, finish_reason=result.finish_reason,
                fallback=int(result.fallback), fallback_reason=result.fallback_reason,
                error_class=result.error_class, latency_ms=result.latency_ms,
                circuit_state=','.join(n + ':' + b.state for n, b in self.breakers.items()),
                retry_at=(self.store.clock().timestamp() + max(0, max(self.attempts.retry_at(fp) or 0,
                    self._blocked_until) - self.clock())) if self.attempts.retry_at(fp) is not None else None,
                final_state=states[0].state if len(states) == 1 else 'MULTI' if states else
                    'ESCALATED' if self.attempts.status(fp) == 'ESCALATED' else 'HOLD',
                decision_rule=states[0].rule if len(states) == 1 else
                    result.error_class if deciding and result.outcome == 'guard' else None,
                template=template, **projected)
            with self.store._lock:
                if self._closing: return None
                with self.store.connection:
                    self.store.connection.execute('UPDATE classification_runs SET ' +
                        ','.join(k + '=?' for k in values) + ' WHERE run_id=?', (*values.values(), run_id))
            return result
        finally:
            if begun:
                self.attempts.finish(fp, 'infra')
                if self.settings.mode == 'live':
                    self._refund()
            if local is not None:
                local.close()
            self._turn_lock.release()

    def _evaluate_proposal(self, job, result, decision_kwargs):
        from .rules_intake import _evaluate_batch

        groups = job['parse'](json.dumps(result.proposal)) if result.proposal else None
        outcomes = None
        # Evaluate with the exact same grounding/partition/decision code, without review I/O.
        if groups is not None:
            class ProposalProvider:
                def __getattr__(self, name):
                    return getattr(job['provider'], name)
            outcomes = _evaluate_batch(job['snapshot'], ProposalProvider(), job['sources'],
                **(decision_kwargs or {}), proposal_groups=groups, review=False)
        projected = SimpleNamespace(values={})
        token = classification_log.CURRENT.set(projected)
        try:
            classification_log.note_proposal(groups, job['snapshot'])
        finally:
            classification_log.CURRENT.reset(token)
        states = [d for _, d in outcomes or ()]
        template = None
        if len(states) == 1 and states[0].state == 'READY' and states[0].intent in ('bug', 'task', 'feedback'):
            template = states[0].intent + '.md'
        return states, template, projected.values

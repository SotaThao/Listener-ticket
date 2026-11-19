#!/usr/bin/env python3
"""P8 canonical-example preflight against a real local router (no writable tools).

Reuses RulesSnapshot's parsed `examples` table and CodeFacts construction from
its self-check. Seen media use neutral valid PNGs plus canonical descriptions as
image-message captions (source.text), through production partition_and_extract's
images map and pixels_available flag. These are caption-based synthetic fixtures,
not screenshot/OCR evidence. Unseen media have neither pixels nor inferred
captions. The table's image-only example has no phantom text message. JSONL
records this fixture design and per-router-call diagnostics. No production DB
is opened. Default models come from AssistantConfig (flash-medium, then GPT).
Captions come ONLY from the narrative, never expected Dữ kiện facts. X20 has
seven photos without a visual description: their captions remain empty and its
text source retains the narrative POS report. X8 explicitly has no caption.
Other seen media lacking a narrative description are fixture errors with case ID.
"""
import argparse
from collections import Counter
from functools import lru_cache
import json
import math
import os
import re
import struct
from pathlib import Path
import sys
import tempfile
import time
import zlib
from dataclasses import replace
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'listener' / 'src'))

from tg_triage.__main__ import _load_assistant_pack, _load_decision_rules
from tg_triage.assistant_contract import GUARDS, DecideRuntime, GuardContext
from tg_triage.assistant_errors import ToolDenied
from tg_triage.assistant_extractor import AssistantExtractor, batch_fingerprint
from tg_triage.assistant_local import LocalReader
from tg_triage.config import (AssistantConfig, Config, ModelConfig, OllamaConfig,
                             ProxyConfig, TelegramConfig, _assistant_token, assistant_models,
                             load_config)
from tg_triage.decision_rules import CodeFacts
from tg_triage.models import IncomingMessage
from tg_triage.router_transport import validate_base_url
from tg_triage.rules_extraction import partition_and_extract, parse_grouped_facts
from tg_triage.store import Store
from tg_triage.ticket_templates import CATALOG


def cases(snapshot):
    """Use the canonical parsed table, not a second Markdown parser or case list."""
    return tuple(snapshot.tables['examples']['examples'])


def code_facts(row):
    seen, unseen = int(row.cells['media_seen']), int(row.cells['media_unseen'])
    images = frozenset(range(2, 2 + seen + unseen))
    text_ids = frozenset({1}) if not images or row.one('text') == 'có' or row.one('command') != '∅' else frozenset()
    return CodeFacts(row.one('command'), row.one('command_payload'), row.one('text'),
        row.one('extraction_ok'), row.one('reply_target'), images | text_ids, images,
        frozenset(range(2, 2 + seen)), text_ids, frozenset())


def _png():
    """One neutral RGB pixel; actual description lives in the production caption."""
    def chunk(kind, data):
        return struct.pack('!I', len(data)) + kind + data + struct.pack('!I', zlib.crc32(kind + data))
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('!2I5B', 1, 1, 8, 2, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(b'\x00\xc0\xc0\xc0')) + chunk(b'IEND', b''))


def media_description(row):
    """Parse narrative media only; never expose expected extraction facts."""
    narrative = row.cells['Tin (rút gọn)'].replace('“', '"').replace('”', '"')
    # A plus inside a quoted caption/code span is not a source separator.
    parts = re.split(r'\s+\+\s+(?=(?:[^"`]*["`][^"`]*["`])*[^"`]*$)', narrative)
    media = next((p for p in parts if re.search(r'\bảnh\b', p, re.I)), '')
    description = re.sub(r'^\s*(?:(?:một|\d+)\s+)?ảnh\s*', '', media, flags=re.I)
    description = re.sub(r'\s*\([^)]*đã xem[^)]*\)\s*$', '', description).strip()
    if re.fullmatch(r',?\s*không chữ', description, re.I):
        return ''
    if description:
        return description.replace('`', '').replace('"', '').strip()
    if (row.cells['#'] == 'X20' and media.strip() == '7 ảnh (5 đã xem, 2 chưa xem)'
            and parts[0].strip() == '"Máy in POS in ra giấy trắng"'):
        return ''  # Canonical narrative specifies counts, not photo contents.
    raise FixtureError('P8 fixture ' + row.cells['#'] + ': seen image lacks narrative description')


def build_sources(row, index, directory):
    """Return production-shaped sources and seen image paths for one example."""
    facts = code_facts(row)
    narrative = row.cells['Tin (rút gọn)'].replace('“', '"').replace('”', '"')
    source = IncomingMessage(chat_id=-10000-index, message_id=1,
        received_at=datetime.now(timezone.utc), sender_id=1,
        sender_name='P8 synthetic example', text=narrative)
    if not facts.image_ids:
        return [source], {}, dict(kind='text_only', seen_ids=[], unseen_ids=[])
    parts = re.split(r'\s+\+\s+(?=(?:[^"`]*["`][^"`]*["`])*[^"`]*$)', narrative)
    text_parts = [p for p in parts if not re.search(r'\bảnh\b', p, re.I)]
    text = ' '.join(match for p in text_parts for match in re.findall(r'"([^"]+)"', p))
    if not text and facts.command != '∅':
        text = facts.command
    sources = [replace(source, text=text)] if 1 in facts.topic_ids else []
    description = media_description(row) if facts.seen_image_ids else ''
    images = {}
    for mid in sorted(facts.image_ids):
        seen = mid in facts.seen_image_ids
        sources.append(replace(source, message_id=mid, text=description if seen else '', has_image=True))
        if seen:
            path = Path(directory) / f'{mid}.png'
            path.write_bytes(_png())
            images[mid] = path
    return sources, images, dict(kind='synthetic_neutral_png_with_canonical_caption',
        seen_ids=sorted(images), unseen_ids=sorted(facts.image_ids - facts.seen_image_ids),
        seen_caption=description, limitation='caption fixture; not screenshot/OCR evidence')


def template(state, intent):
    return intent + '.md' if state == 'READY' and intent in ('bug', 'task', 'feedback') else None


class _EvalExtractor(AssistantExtractor):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        chat = self.chat
        self.calls = []
        def measured(model, messages, **kw):
            started = time.monotonic()
            call = dict(link=('gemini', 'gpt')[self.settings.models.index(model)],
                        model=model, timeout_s=kw['timeout'], error_class=None, finish_reason=None)
            try:
                reply = chat(model, messages, **kw)
                call['finish_reason'] = reply.finish_reason
                call.update(prompt_tokens=reply.prompt_tokens, completion_tokens=reply.completion_tokens,
                            cached_tokens=reply.cached_tokens)
                if reply.finish_reason == 'length':
                    call['error_class'] = 'truncated'
                elif not reply.tool_calls:
                    from tg_triage.assistant import router_proposal_content, schema_rejection_kind
                    if parse_grouped_facts(router_proposal_content(reply.content), self.diagnostic_snapshot, self.diagnostic_ids) is None:
                        call['error_class'] = 'schema'
                        call['schema_error_kind'] = schema_rejection_kind(reply.content)
                return reply
            except Exception as error:
                from tg_triage.assistant import _kind
                call['error_class'] = _kind(error)
                if call['error_class'] == 'schema': call.setdefault('schema_error_kind', 'unavailable')
                raise
            finally:
                call['latency_ms'] = max(0, round((time.monotonic() - started) * 1000))
                self.calls.append(call)
        self.chat = measured

    def _acquire(self):
        # Preflight can wait; the foreground runtime never sleeps on admission.
        while not self.limiter.try_acquire():
            time.sleep(.1)


class _BodyProvider:
    settings = type('Settings', (), {'model': 'assistant-preflight'})()
    think = False
    request_budget_bytes = 24000
    def _ask(self, *args, **kwargs):
        raise AssertionError('preflight must only use the assistant')


class FixtureError(ValueError):
    """Canonical case cannot be represented faithfully; fixed message has ID only."""


class DecideRuntimeUnavailable(RuntimeError):
    """T2 has not landed in this worktree."""


def expected_case(row):
    expected = row.one('Kết quả')
    intent = {'R9': 'bug', 'R10': 'task', 'R11': 'feedback'}.get(row.cells['Luật'], row.one('intent (model)'))
    return expected, template(expected, intent)


def recorded_template(value):
    return value + '.md' if value in ('bug', 'task', 'feedback') else value


def load_pack(authority, pack_mode='full'):
    if pack_mode not in ('full', 'core') or (pack_mode == 'core' and authority != 'decide'):
        raise FixtureError('core pack mode requires decide authority')
    return _load_assistant_pack(pack_mode=pack_mode)


def offline_config(path):
    env = dict(os.environ, TG_TRIAGE_MODE='dry-run', TG_API_ID='1', TG_API_HASH='0' * 32, GH_TOKEN='')
    config = load_config(path, environ=env)
    repos = config.assistant.github_repos
    return replace(config, gh_repo=repos[0]) if not config.gh_repo and len(repos) == 1 else config


def rule_lookup(db_path, pack_text, snapshot):
    @lru_cache(maxsize=None)
    def lookup(rule):
        reader = LocalReader(db_path, 0, batch_message_ids=(), images={}, pack_text=pack_text)
        try:
            return reader.get_rule(rule)['text']
        except ToolDenied:
            return ''
        finally:
            reader.close()

    ids = {row.cells['#'] for row in snapshot.tables['readiness']['decision']}
    return frozenset(ids | {'F1', 'F2', 'F3', 'F4', 'F5', 'F6'}), lookup


def decide_job(config, snapshot, rules, sources, *, images=None, reply_targets=None):
    sources = tuple(sources)
    images = images or {}
    bodies = []
    partition_and_extract(_BodyProvider(), snapshot, sources, images=images, body_out=bodies)
    if not bodies:
        raise FixtureError('Batch cannot be serialized within the extraction packet budget')
    body = bodies[0]
    packet = json.loads(body['messages'][1]['content'])
    if reply_targets:
        packet['reply_targets'] = [
            dict(message_id=mid, number=number, state=state, repo=config.gh_repo)
            for mid, (number, state) in reply_targets.items()
        ]
        body['messages'][1]['content'] = json.dumps(packet, ensure_ascii=False)
    rule_ids, rule_text = rules
    chat_id = sources[0].chat_id
    ctx = GuardContext(frozenset(packet['source_ids']), chat_id, config.gh_repo,
                       reply_targets or {}, {}, rule_ids, rule_text,
                       frozenset(block for block in CATALOG['group-replies.md']
                                 if block.startswith(('ask.', 'question.'))),
                       max_value_chars=snapshot.limits['max_value_chars'],
                       max_fields_per_ask=snapshot.limits['max_fields_per_ask'],
                       seen_image_ids=frozenset(images),
                       append_confidence=config.assistant.append_confidence)
    return dict(sources=sources, snapshot=snapshot,
                data=dict(body=body, images=images, documents=[], read_context=()),
                fingerprint=batch_fingerprint(sources), id_space='message', topic_id=None,
                chat_id=chat_id, guard_ctx=ctx, code_precheck=None, log_thread_id=None,
                deadline=time.monotonic() + config.assistant.turn_deadline_s)


class _PacedLimiter:
    def __init__(self, limiter):
        self.limiter = limiter

    def try_acquire(self):
        while not self.limiter.try_acquire():
            time.sleep(.1)
        return True


def real_decide_runtime(config, store, pack_text, *, chat=None, core_pack=None) -> DecideRuntime:
    if not callable(getattr(AssistantExtractor, 'decide', None)):
        raise DecideRuntimeUnavailable('AssistantExtractor.decide is unavailable until T2 is integrated')
    runtime = AssistantExtractor(config, store, pack_text, chat=chat, core_pack=core_pack)
    runtime.limiter = _PacedLimiter(runtime.limiter)
    return runtime


def decide_runtime(config, store, pack, factory, **kwargs):
    if factory is not None:
        return factory(config, store, pack.core.text if pack.core else pack.text, **kwargs)
    return real_decide_runtime(config, store, pack.text, core_pack=pack.core, **kwargs)


def decide_result(runtime: DecideRuntime, store, job):
    started = time.monotonic()
    groups, run_id, outcome = runtime.decide(job)
    latency = round((time.monotonic() - started) * 1000)
    audit, calls, rules = {}, [], []
    rule_calls = 0
    if run_id is not None:
        row = store.connection.execute(
            'SELECT * FROM classification_runs WHERE run_id=?', (run_id,)).fetchone()
        if row is not None:
            audit = dict(row)
        calls = [dict(call) for call in store.connection.execute(
            'SELECT link, model, error_class, prompt_tokens, completion_tokens, cached_tokens, latency_ms '
            'FROM classification_model_calls WHERE run_id=? ORDER BY seq', (run_id,))]
        for (encoded,) in store.connection.execute(
                "SELECT result_json FROM classification_tool_calls WHERE run_id=? AND tool='get_rule' ORDER BY seq",
                (run_id,)):
            rule_calls += 1
            try:
                result = json.loads(encoded or '{}')
            except (TypeError, ValueError):
                continue
            if isinstance(result, dict) and result.get('ok') and isinstance(result.get('untrusted_data'), dict):
                if rule_id := result['untrusted_data'].get('rule_id'):
                    rules.append(rule_id)
    outcomes = json.loads(audit.get('outcomes_json') or '[]')
    terminal_guards = Counter(item['guard'] for group in outcomes or [] for item in group.get('violations', [])
                              if item.get('guard') in GUARDS)
    rejected_calls = Counter(call['error_class'] for call in calls if call['error_class'] in GUARDS)
    guards = list((terminal_guards | rejected_calls).elements())
    if not guards and outcome == 'guard':
        guard = audit.get('error_class') or audit.get('decision_rule')
        guards.append(guard if guard in GUARDS else 'unknown_guard')
    return groups or [], dict(outcome=outcome, calls=calls, guard_violations=guards,
        model_calls=audit.get('model_calls', len(calls)), model=audit.get('model'),
        tokens_in=sum(call['prompt_tokens'] or 0 for call in calls) if calls else audit.get('tokens_in', 0),
        tokens_out=sum(call['completion_tokens'] or 0 for call in calls) if calls else audit.get('tokens_out', 0),
        cached_tokens=sum(call['cached_tokens'] or 0 for call in calls) if calls else audit.get('cache_hit_tokens', 0),
        get_rule_calls=rule_calls, get_rule_ids=rules, latency_ms=latency)


TICKET_STATES = {'READY', 'APPEND'}


def pack_metrics(rows):
    comparable = [row for row in rows if row.get('comparable', True)]
    create = [row.get('creation_agrees', False) for row in comparable
              if row.get('creation_agrees', False) is not None]
    latencies = sorted(row['latency_ms'] for row in rows if row.get('latency_ms') is not None)
    ids = sorted({rule for row in rows for rule in row.get('get_rule_ids', ())})
    def percentile(q):
        return latencies[math.ceil(q * len(latencies)) - 1] if latencies else 'n/a'
    return {
        'Input tokens': sum(row.get('tokens_in', 0) or 0 for row in rows),
        'Output tokens': sum(row.get('tokens_out', 0) or 0 for row in rows),
        'Cached tokens': sum(row.get('cached_tokens', 0) or 0 for row in rows),
        'get_rule calls': sum(row.get('get_rule_calls', 0) for row in rows),
        'get_rule ids (successful)': ', '.join(ids) or '—',
        'State agreement': f"{sum(row['actual_state'] == row['expected_state'] for row in comparable)}/{len(comparable)}",
        'Create ticket agreement (READY)': f'{sum(create)}/{len(create)}' if create else 'n/a',
        'Template agreement': f"{sum(bool(row.get('template_agrees')) for row in comparable)}/{len(comparable)}",
        'Latency p50 / p95 (ms)': f'{percentile(.50)} / {percentile(.95)}',
    }


def print_pack_comparison(results):
    metrics = {mode: pack_metrics(rows) for mode, rows in results.items()}
    print('\nMetric\tfull\tcore')
    for name in next(iter(metrics.values())):
        print(f"{name}\t{metrics.get('full', {}).get(name, '—')}\t{metrics.get('core', {}).get(name, '—')}")
    if 'full' in metrics and 'core' in metrics and metrics['full']['Input tokens']:
        reduction = 1 - metrics['core']['Input tokens'] / metrics['full']['Input tokens']
        print(f'Core input token reduction: {reduction:.1%}')


def print_summary(rows):
    comparable = [row for row in rows if row.get('comparable', True)]
    ticket_matches = sum(row.get('ticket_agrees',
        row['actual_state'] not in ('ERROR', 'NO_DECISION') and
        (row['expected_state'] in TICKET_STATES) == (row['actual_state'] in TICKET_STATES))
        for row in comparable)
    template_matches = sum(row.get('template_agrees', row['passed'] or
        (row['actual_state'] not in ('ERROR', 'NO_DECISION', 'MULTI') and
         row['expected_template'] == row['actual_template'])) for row in comparable)
    in_tokens, out_tokens, cached = (sum(row.get(key, 0) or 0 for row in rows)
                                    for key in ('tokens_in', 'tokens_out', 'cached_tokens'))
    latencies = sorted(row['latency_ms'] for row in rows if row.get('latency_ms') is not None)
    guards = Counter(guard for row in rows for guard in row.get('guard_violations', ()))

    def percentile(q):
        return latencies[math.ceil(q * len(latencies)) - 1] if latencies else 'n/a'

    print('\nMetric                         Value')
    print(f'Exact state + template         {sum(row["passed"] for row in comparable)}/{len(comparable)}')
    print(f'Ticket agreement (READY/APPEND) {ticket_matches}/{len(comparable)}')
    print(f'Template agreement             {template_matches}/{len(comparable)}')
    print(f'Guard violations               {sum(guards.values())} {dict(sorted(guards.items()))}')
    print(f'Tokens in / out                {in_tokens} / {out_tokens}')
    print(f'Cached tokens / input tokens   {cached}/{in_tokens} ({cached / in_tokens:.1%})'
          if in_tokens else 'Cached tokens / input tokens   n/a')
    print(f'Batch latency p50 / p95 (ms)   {percentile(.50)} / {percentile(.95)}')


def evaluate_case(runtime, snapshot, row, index):
    # Paths are consumed synchronously by runtime.run, and removed afterwards.
    runtime.calls.clear()
    with tempfile.TemporaryDirectory(prefix='p8-media-') as directory:
        return _evaluate_case(runtime, snapshot, row, index, directory)


def _evaluate_case(runtime, snapshot, row, index, directory):
    facts = code_facts(row)
    sources, images, fixture = build_sources(row, index, directory)
    for message in sources:
        runtime.store.enqueue(message)
    provider, bodies = _BodyProvider(), []
    partition_and_extract(provider, snapshot, sources, images=images, body_out=bodies)
    ids = [m.message_id for m in sources]
    job = dict(provider=provider, snapshot=snapshot, sources=sources, body=bodies[0],
               parse=lambda raw: parse_grouped_facts(raw, snapshot, ids),
               data=dict(images=images, documents=[], read_context=()))
    runtime.calls.clear()
    runtime.diagnostic_snapshot, runtime.diagnostic_ids = snapshot, ids
    result = runtime.run(job)
    links = {}
    for call in runtime.calls:
        item = links.setdefault(call['link'], dict(model=call['model'], error_class=None, fallback_reason=None))
        if call['error_class']:
            item['error_class'] = call['error_class']
            item['fallback_reason'] = item['fallback_reason'] or call['error_class']
    if result and result.link:
        item = links.setdefault(result.link, dict(model=result.model, error_class=None, fallback_reason=None))
        item['error_class'] = result.error_class or item['error_class']
    proposed = (result.proposal or {}).get('groups', []) if result else []
    # Examples describe one subject; multiple groups fail preflight explicitly.
    decision = snapshot.evaluate(proposed[0]['facts'] if len(proposed) == 1 else {},
                                facts if result and result.outcome == 'ok' else replace(facts, extraction_ok='false'))
    expected, expected_template = expected_case(row)
    actual_template = template(decision.state, decision.intent)
    return dict(case=row.cells['#'], expected_state=expected, actual_state=decision.state,
        expected_template=expected_template, actual_template=actual_template,
        passed=(decision.state == expected and actual_template == expected_template and len(proposed) <= 1),
        decision_rule=decision.rule, model=result.model if result else None,
        model_calls=result.model_calls if result else 0,
        error_class=result.error_class if result else 'unavailable',
        proposal_link=result.link if result and result.proposal else None,
        fallback_reason=result.fallback_reason if result else None,
        calls=list(runtime.calls), links=links, media_fixture=fixture,
        tokens_in=sum(call.get('prompt_tokens') or 0 for call in runtime.calls),
        tokens_out=sum(call.get('completion_tokens') or 0 for call in runtime.calls),
        cached_tokens=sum(call.get('cached_tokens') or 0 for call in runtime.calls),
        latency_ms=sum(call['latency_ms'] for call in runtime.calls),
        pack_fingerprint=runtime.store.assistant_pack_fingerprint)


def evaluate_decide_case(runtime, config, store, snapshot, rules, row, index):
    expected, expected_template = expected_case(row)
    with tempfile.TemporaryDirectory(prefix='p8-media-') as directory:
        sources, images, fixture = build_sources(row, index, directory)
        reply_targets = {}
        target = row.one('reply_target')
        if target in ('ticket_open', 'ticket_closed'):
            sources[0] = replace(sources[0], reply_to_id=100)
            reply_targets[sources[0].message_id] = (1, target.removeprefix('ticket_'))
        for message in sources:
            store.enqueue(message)
        job = decide_job(config, snapshot, rules, sources, images=images, reply_targets=reply_targets)
        groups, metrics = decide_result(runtime, store, job)
    actual = groups[0].state if len(groups) == 1 else 'MULTI' if groups else 'NO_DECISION'
    actual_template = recorded_template(groups[0].template) if len(groups) == 1 else None
    return dict(case=row.cells['#'], expected_state=expected, expected_template=expected_template,
        actual_state=actual, actual_template=actual_template, media_fixture=fixture,
        passed=len(groups) == 1 and actual == expected and actual_template == expected_template,
        ticket_agrees=bool(groups) and any(g.state in TICKET_STATES for g in groups) == (expected in TICKET_STATES),
        creation_agrees=bool(groups) and any(g.state == 'READY' for g in groups) == (expected == 'READY'),
        template_agrees=len(groups) == 1 and actual_template == expected_template,
        decision_rule=groups[0].rule if len(groups) == 1 else None,
        pack_fingerprint=store.assistant_pack_fingerprint, **metrics)


def run_eval(*, router_url, key_file, output, models=None, chat=None, authority='propose',
             runtime_factory=None, pack_mode='full', append_output=False):
    validate_base_url(router_url)
    key_path = Path(key_file)
    if not key_path.is_absolute():
        raise ValueError('key file must be absolute')
    key = _assistant_token(str(key_path))
    if not key:
        raise ValueError('router key unavailable')
    snapshot, pack = _load_decision_rules(), load_pack(authority, pack_mode)
    selected = assistant_models(list(models or AssistantConfig.models))
    rows = []
    with tempfile.TemporaryDirectory(prefix='assistant-p8-') as directory:
        db = str(Path(directory) / 'examples.db')
        store = Store(db)
        try:
            store.assistant_pack_fingerprint = pack.fingerprint
            config = Config('dry-run', TelegramConfig('unused', (), 0, ProxyConfig()),
                OllamaConfig('http://127.0.0.1:11434', 'unused', 1, .7), 1, db, 1, '0'*32,
                model=ModelConfig(fallback='none'),
                gh_repo='example/p8' if authority == 'decide' else '',
                assistant=AssistantConfig(enabled=True, mode='live' if authority == 'decide' else 'shadow',
                    authority=authority, pack_mode=pack_mode, router_base_url=router_url,
                    router_key_file=str(key_path), router_key=key, models=selected))
            if authority == 'decide':
                runtime = decide_runtime(config, store, pack, runtime_factory, chat=chat)
                rules = rule_lookup(db, pack.text, snapshot)
            else:
                runtime = _EvalExtractor(config, store, pack.text, chat=chat)
            with Path(output).open('a' if append_output else 'w', encoding='utf-8') as artifact:
                for index, row in enumerate(cases(snapshot), 1):
                    try:
                        if authority == 'decide':
                            result = evaluate_decide_case(runtime, config, store, snapshot, rules, row, index)
                        else:
                            result = evaluate_case(runtime, snapshot, row, index)
                    except FixtureError:
                        raise
                    except Exception as error:
                        result = dict(case=row.cells['#'], expected_state=row.one('Kết quả'),
                            actual_state='ERROR', expected_template=None, actual_template=None,
                            passed=False, error_class=type(error).__name__, model=None,
                            proposal_link=None, fallback_reason=None, calls=list(getattr(runtime, 'calls', ())),
                            links={}, media_fixture={'kind': 'fixture_or_turn_error'})
                    rows.append(result)
                    result['pack_mode'] = pack_mode
                    if pack.core:
                        result['core_pack_fingerprint'] = pack.core.fingerprint
                    artifact.write(json.dumps(result, ensure_ascii=False) + '\n')
                    artifact.flush()
                    print(f"{result['case']}: expected {result['expected_state']}/{result['expected_template']} "
                          f"actual {result['actual_state']}/{result['actual_template']} "
                          f"{'PASS' if result['passed'] else 'FAIL'}")
        finally:
            store.close()
    total = sum(row['passed'] for row in rows)
    print(f'{total}/{len(rows)}')
    print_summary(rows)
    if authority == 'decide':
        print_pack_comparison({pack_mode: rows})
    return rows


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--router-url', required=True)
    parser.add_argument('--key-file', required=True)
    parser.add_argument('--output', required=True, help='JSONL artifact path')
    parser.add_argument('--models', nargs=2, metavar=('PRIMARY', 'SECONDARY'))
    parser.add_argument('--authority', choices=('propose', 'decide'), default='propose')
    parser.add_argument('--pack-mode', choices=('full', 'core'), default='full')
    parser.add_argument('--compare', action='store_true', help='Run both pack modes on the same decide cases')
    args = parser.parse_args(argv)
    try:
        if args.compare and args.authority != 'decide':
            raise FixtureError('--compare requires --authority decide')
        modes = ('full', 'core') if args.compare else (args.pack_mode,)
        results = {mode: run_eval(router_url=args.router_url, key_file=args.key_file,
                                  output=args.output, models=args.models, authority=args.authority,
                                  pack_mode=mode, append_output=index > 0)
                   for index, mode in enumerate(modes)}
        if args.compare:
            print_pack_comparison(results)
        return 0 if all(row['passed'] for rows in results.values() for row in rows) else 1
    except (FixtureError, DecideRuntimeUnavailable) as error:
        print(str(error), file=sys.stderr)
        return 2
    except Exception as error:
        print('P8 failed: ' + type(error).__name__, file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())

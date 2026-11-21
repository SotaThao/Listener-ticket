"""Best-effort rules-attempt accounting. Proposals retain structure and IDs only.

The recorded decision precedes execution overrides. Classic intake and unread-document
branches do not enter this scope. This module also owns the assistant capture tables.
"""

import hashlib
import json
import logging
import re
import sqlite3
from collections import deque
from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone

from agentfleet_common.ai_transport import redact

from .router_transport import CAPTURE_TEXT_BYTES, CAPTURE_TRUNCATION, bounded_text

LOG = logging.getLogger(__name__)
CURRENT = ContextVar('classification_run', default=None)
MAX_PENDING_RUNS = 500
THREAD_LOOKBACK_DAYS = 30
THREAD_LOOKUP_LIMIT = 500
DROP_WARNING_SECONDS = 60
_JSON_STRING = re.compile(r'"(?:[^"\\]|\\.)*"')
_CAPTURE_QUOTED = re.compile(r'"(?:[^"\\]|\\.)*(?P<end>"|\\?\Z)', re.DOTALL)


def capture_text(text):
    """Redact JSON strings (including escapes) while preserving the original layout."""
    if text is None:
        return None

    def clean(match):
        try:
            value = json.loads(match[0])
        except ValueError:
            return redact(match[0])
        redacted = redact(value)
        return match[0] if redacted == value else json.dumps(redacted, ensure_ascii=False)

    try:
        structured = isinstance(json.loads(text), (dict, list))
    except (ValueError, RecursionError):
        structured = False
    if structured:
        return _JSON_STRING.sub(clean, text)
    return redact(_JSON_STRING.sub(clean, text))


def bounded_capture(text, limit=CAPTURE_TEXT_BYTES):
    clipped = bounded_text(text, limit)
    if clipped is not None and clipped.endswith(CAPTURE_TRUNCATION):
        # A clipped JSON escape may hide sensitive text from the redactor.
        for match in _CAPTURE_QUOTED.finditer(clipped):
            if match['end'] != '"' and '\\' in match[0]:
                clipped = clipped[:match.start()] + CAPTURE_TRUNCATION
    return bounded_text(capture_text(clipped), limit)


def init_store(connection):
    """Apply additive accounting schema changes in the caller's transaction."""
    connection.execute('''CREATE TABLE IF NOT EXISTS classification_runs (
        run_id INTEGER PRIMARY KEY AUTOINCREMENT, created_at REAL NOT NULL,
        turn_key TEXT NOT NULL, topic_id TEXT, phase TEXT NOT NULL,
        mode TEXT NOT NULL DEFAULT 'live', batch_fingerprint TEXT NOT NULL,
        attempt_no INTEGER, chat_id INTEGER NOT NULL, message_ids TEXT NOT NULL,
        id_space TEXT NOT NULL, image_ids TEXT NOT NULL DEFAULT '[]', rules_version TEXT,
        rules_fingerprint TEXT NOT NULL, provider TEXT, model TEXT, effort TEXT,
        model_calls INTEGER NOT NULL DEFAULT 0, latency_ms INTEGER NOT NULL DEFAULT 0,
        tokens_in INTEGER NOT NULL DEFAULT 0, tokens_out INTEGER NOT NULL DEFAULT 0,
        cache_hit_tokens INTEGER NOT NULL DEFAULT 0, reasoning_tokens INTEGER,
        finish_reason TEXT, cost REAL NOT NULL DEFAULT 0, fallback INTEGER NOT NULL DEFAULT 0,
        fallback_reason TEXT, error_class TEXT, circuit_state TEXT NOT NULL DEFAULT 'none',
        retry_at REAL, proposal_json TEXT, reasoning_summary TEXT, final_state TEXT,
        decision_rule TEXT, outcomes_json TEXT, template TEXT, ticket_ref TEXT, error TEXT)''')
    columns = {row[1] for row in connection.execute('PRAGMA table_info(classification_runs)')}
    for name, sql_type in (('code_precheck_json', 'TEXT'), ('log_thread_id', 'INTEGER')):
        if name not in columns:
            connection.execute(f'ALTER TABLE classification_runs ADD COLUMN {name} {sql_type}')
    connection.execute('CREATE INDEX IF NOT EXISTS idx_classification_created ON classification_runs(created_at)')
    connection.execute('CREATE INDEX IF NOT EXISTS idx_classification_batch ON classification_runs(batch_fingerprint, attempt_no)')
    connection.execute('CREATE INDEX IF NOT EXISTS idx_classification_topic ON classification_runs(topic_id)')
    connection.execute('CREATE INDEX IF NOT EXISTS idx_classification_chat_run ON classification_runs(chat_id,run_id)')
    connection.execute('CREATE INDEX IF NOT EXISTS idx_classification_thread ON classification_runs(COALESCE(log_thread_id,run_id))')
    connection.execute('''CREATE TABLE IF NOT EXISTS classification_run_sources (
        run_id INTEGER NOT NULL, ordinal INTEGER NOT NULL, input_id INTEGER NOT NULL,
        chat_id INTEGER NOT NULL, message_id INTEGER NOT NULL, PRIMARY KEY(run_id,ordinal))''')
    connection.execute('''CREATE INDEX IF NOT EXISTS idx_classification_source_runs
        ON classification_run_sources(chat_id,message_id,run_id)''')
    connection.execute('''CREATE TABLE IF NOT EXISTS classification_tool_calls (
        run_id INTEGER NOT NULL, seq INTEGER NOT NULL, tool TEXT NOT NULL, args_json TEXT,
        result_bytes INTEGER NOT NULL DEFAULT 0, latency_ms INTEGER NOT NULL DEFAULT 0,
        error TEXT, result_json TEXT, started_at REAL, args_full_json TEXT,
        PRIMARY KEY(run_id, seq))''')
    columns = {row[1] for row in connection.execute('PRAGMA table_info(classification_tool_calls)')}
    for name, sql_type in (('result_json', 'TEXT'), ('started_at', 'REAL'), ('args_full_json', 'TEXT')):
        if name not in columns:
            connection.execute(f'ALTER TABLE classification_tool_calls ADD COLUMN {name} {sql_type}')
    connection.execute('''CREATE TABLE IF NOT EXISTS classification_model_calls (
        run_id INTEGER NOT NULL, seq INTEGER NOT NULL, link TEXT NOT NULL,
        model TEXT, started_at REAL NOT NULL, latency_ms INTEGER NOT NULL DEFAULT 0,
        timeout_s REAL, finish_reason TEXT, prompt_tokens INTEGER NOT NULL DEFAULT 0,
        completion_tokens INTEGER NOT NULL DEFAULT 0, reasoning_tokens INTEGER,
        cached_tokens INTEGER NOT NULL DEFAULT 0, reasoning_text TEXT, content_text TEXT,
        tool_calls_json TEXT, error_class TEXT, schema_error_kind TEXT,
        PRIMARY KEY(run_id, seq))''')


def _failed(error):
    LOG.warning('Classification accounting failed exception=%s', type(error).__name__)


def _code(value):
    # These are host/provider codes, never exception messages or arbitrary prose.
    return value if isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_:+.-]{1,120}', value) else None


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), sort_keys=True)


def _ids(values):
    return [value for value in values if type(value) is int and value > 0]


def _notice(store, kind, level, message, *args):
    now = store.clock().timestamp()
    notices = getattr(store, '_classification_notices', None)
    if notices is None:
        notices = store._classification_notices = {}
    previous = notices.get(kind)
    if previous is not None and now - previous < DROP_WARNING_SECONDS:
        return False
    LOG.log(level, message, *args)
    notices[kind] = now
    return True


def _warn_drops(store):
    dropped = getattr(store, '_classification_dropped', 0)
    if not dropped:
        return
    if _notice(store, 'overflow', logging.WARNING,
               'Classification accounting queue overflow dropped=%s', dropped):
        store._classification_dropped = 0


def _warn_poison(store, error=None):
    if error is not None:
        store._classification_poison_dropped = getattr(store, '_classification_poison_dropped', 0) + 1
        store._classification_poison_error = type(error).__name__
    dropped = getattr(store, '_classification_poison_dropped', 0)
    if dropped and _notice(store, 'poison', logging.WARNING,
            'Classification accounting invalid row dropped=%s exception=%s',
            dropped, store._classification_poison_error):
        store._classification_poison_dropped = 0


def _enqueue(store, values):
    # Caller holds the Store RLock. Keep scalar metadata, never a Turn or sources.
    queue = getattr(store, '_classification_pending', None)
    if queue is None:
        queue = store._classification_pending = deque()
    if len(queue) >= MAX_PENDING_RUNS:
        queue.popleft()
        store._classification_dropped = getattr(store, '_classification_dropped', 0) + 1
    queue.append(dict(values))
    _warn_drops(store)


def _insert_run(connection, values):
    values = dict(values)
    source_keys = values.pop('_source_keys', ())
    columns = ','.join(values)
    run_id = connection.execute(
        f'INSERT INTO classification_runs ({columns}) VALUES ({",".join("?" for _ in values)})',
        tuple(values.values())).lastrowid
    if 'log_thread_id' in values and values['log_thread_id'] is None:
        connection.execute('UPDATE classification_runs SET log_thread_id=? WHERE run_id=?',
                           (run_id, run_id))
    if source_keys:
        # Preserve the existing run even when optional source indexing fails.
        connection.execute('SAVEPOINT classification_source_index')
        try:
            connection.executemany('''INSERT INTO classification_run_sources
                (run_id,ordinal,input_id,chat_id,message_id) VALUES (?,?,?,?,?)''',
                ((run_id, ordinal, *key) for ordinal, key in enumerate(source_keys)))
        except sqlite3.Error as error:
            connection.execute('ROLLBACK TO classification_source_index')
            _failed(error)
        finally:
            connection.execute('RELEASE classification_source_index')
    return run_id


def source_keys(sources):
    return tuple((m.message_id, m.chat_id, m.message_id) for m in sources)


def topic_fingerprint(topic_id, ref_sources):
    key = dict(id_space='topic_ref', topic_id=topic_id, sources=sorted(ref_sources))
    return hashlib.sha256(_json(key).encode('utf-8')).hexdigest()


def find_thread(store, chat_id, *, reply_to_ids=(), topic_id=None, ticket_ref=None):
    """Match at most 500 recent runs in this chat and resolve the thread root."""
    try:
        replies = set(_ids(reply_to_ids))
        if not replies and topic_id is None and ticket_ref is None:
            return None
        # Intake may have only the issue number while accounting stores the URL.
        ticket_pattern = (f'https://github.com/*/*/issues/{ticket_ref}'
                          if type(ticket_ref) is int and ticket_ref > 0 else None)
        cutoff = (store.clock() - timedelta(days=THREAD_LOOKBACK_DAYS)).timestamp()
        with store._lock:
            rows = store.connection.execute('''SELECT run_id, log_thread_id, message_ids,
                (topic_id=:topic OR ticket_ref=:ticket OR ticket_ref GLOB :pattern) AS linked
                FROM (SELECT run_id, log_thread_id, message_ids, topic_id, ticket_ref, created_at
                      FROM classification_runs WHERE chat_id=:chat ORDER BY run_id DESC LIMIT :limit)
                WHERE created_at>=:cutoff
                ORDER BY run_id DESC''', dict(topic=topic_id, ticket=ticket_ref,
                    pattern=ticket_pattern, chat=chat_id, cutoff=cutoff, limit=THREAD_LOOKUP_LIMIT)).fetchall()
        for run_id, root, encoded, linked in rows:
            if linked or (replies and replies.intersection(json.loads(encoded))):
                return run_id if root is None else root
    except Exception as error:
        _failed(error)
    return None


def set_thread(store, run_id, thread_id):
    """Join once and keep roots with children in place."""
    try:
        with store._lock:
            connection = store.connection
            with nullcontext() if connection.in_transaction else connection:
                connection.execute('''UPDATE classification_runs AS run SET log_thread_id=?
                    WHERE run.run_id=? AND (run.log_thread_id IS NULL OR run.log_thread_id=run.run_id)
                      AND NOT EXISTS (SELECT 1 FROM classification_runs AS child
                        WHERE COALESCE(child.log_thread_id,child.run_id)=run.run_id
                          AND child.run_id<>run.run_id)''',
                    (thread_id, run_id))
    except Exception as error:
        _failed(error)


def flush(store):
    """Commit queued runs FIFO only outside a caller transaction; never raise.

    Commit each row separately, retaining it on connection/storage failure.
    Row-specific binding/constraint errors discard only that row, with counted,
    rate-limited warnings, so later rows and maintenance can proceed. The bounded
    memory queue is not durable across process exit. All access uses the Store RLock.
    """
    try:
        with store._lock:
            connection = store.connection
            if connection.in_transaction:
                return False
            queue = getattr(store, '_classification_pending', None)
            while queue:
                values = queue[0]
                try:
                    with connection:
                        _insert_run(connection, values)
                except (sqlite3.IntegrityError, sqlite3.InterfaceError, sqlite3.ProgrammingError) as error:
                    # A closed/unusable connection is not a poison row. Probe it
                    # after our transaction rolled back before discarding data.
                    connection.execute('SELECT 1').fetchone()
                    queue.popleft()
                    _warn_poison(store, error)
                else:
                    queue.popleft()
            _warn_drops(store)
            _warn_poison(store)
            return True
    except Exception as error:
        _failed(error)
        return False


class _Attempt:
    def __init__(self, turn, snapshot, sources, images, phase, topic_id=None, *,
                 code_precheck=None, log_thread_id=None, ref_sources=None):
        if any(type(source.chat_id) is not int or source.chat_id == 0 for source in sources):
            raise ValueError('invalid chat ID')
        self.turn = turn
        self.result = None
        self.started = turn.store.clock()
        topic_id = turn.root_key if topic_id is None else topic_id
        keys = sorted([m.chat_id, m.message_id] for m in sources)
        id_space = 'topic_ref' if ref_sources is not None or phase in ('pending_merge', 'followup_confirm') else 'message'
        fingerprint = (topic_fingerprint(topic_id, keys) if id_space == 'topic_ref'
                       else hashlib.sha256(_json(keys).encode('utf-8')).hexdigest())
        self.replaced = False
        self.values = dict(
            created_at=self.started.timestamp(), turn_key=turn.key, topic_id=topic_id,
            phase=phase, batch_fingerprint=fingerprint,
            chat_id=sources[0].chat_id, message_ids=_json([m.message_id for m in sources]),
            id_space=id_space, rules_version=getattr(turn.store, 'assistant_pack_fingerprint', None),
            image_ids=_json(sorted(images or {})), rules_fingerprint=snapshot.fingerprint,
            model_calls=0, tokens_in=0, tokens_out=0, cache_hit_tokens=0, cost=0.0, fallback=0)
        if id_space == 'message':
            self.values['_source_keys'] = source_keys(sources)
        elif ref_sources is not None:
            originals = {(ref, m.chat_id): m.message_id for ref, m in ref_sources}
            self.values['_source_keys'] = tuple(
                (m.message_id, m.chat_id, originals[(m.message_id, m.chat_id)])
                for m in sources if (m.message_id, m.chat_id) in originals
            )
        self.code_precheck = code_precheck
        if code_precheck is not None:
            self.values['code_precheck_json'] = _json(code_precheck)
        if code_precheck is not None or log_thread_id is not None:
            self.values['log_thread_id'] = log_thread_id

    def write(self):
        values = self.values
        values['latency_ms'] = max(0, int((self.turn.store.clock() - self.started).total_seconds() * 1000))
        values['error'] = _code(self.turn.reason) or values.get('error')
        if self.result is None:
            values['final_state'] = 'HOLD'
            values['error'] = values['error'] or 'rules_extraction_failed'
        else:
            outcomes = [dict(message_ids=[m.message_id for m in members],
                             final_state=decision.state, decision_rule=decision.rule,
                             intent=decision.intent,
                             template=f'{decision.intent}.md' if decision.state == 'READY'
                             and decision.intent in ('bug', 'task', 'feedback') else None)
                        for members, decision in self.result]
            if len(outcomes) == 1:
                values.update({key: outcomes[0][key] for key in ('final_state', 'decision_rule', 'template')})
            else:
                values.update(final_state='MULTI', outcomes_json=_json(outcomes))
            if self.code_precheck is not None:
                for outcome in outcomes:
                    outcome['rule_text'] = self.code_precheck.get('rule_text')
                values['outcomes_json'] = _json(outcomes)
        store = self.turn.store
        with store._lock:
            connection = store.connection
            safe = not connection.in_transaction
            flushed = flush(store) if safe else False
            if values['phase'] != 'followup_confirm':
                if connection.execute("SELECT 1 FROM sqlite_master WHERE name='rules_topics' AND type='table'").fetchone():
                    row = connection.execute('SELECT attempts FROM rules_topics WHERE topic_id=?',
                                             (self.turn.root_key,)).fetchone()
                    values['attempt_no'] = row[0] if row else None
            _enqueue(store, values)
            if not safe:
                _notice(store, 'deferred', logging.INFO,
                        'Classification accounting deferred queued=%s', len(store._classification_pending))
            if safe and flushed:
                flush(store)


def record_decision(store, snapshot, topic_id, sources, decision, *, phase='early',
                    code_precheck=None, log_thread_id=None):
    """Record a used, model-free rules decision; no CURRENT scope or bot side effects.

    Construct a private metadata Turn without entering it or journaling it. Proposal
    and provider fields stay NULL; source content and decision field values are never
    projected. This API follows the same deferred-write and failure-isolation rules.
    """
    try:
        from .thinking import Turn
        sources = tuple(sources)
        if not sources:
            return
        turn = Turn(store, 'rules:' + topic_id, topic_id, sources[0].message_id)
        attempt = _Attempt(turn, snapshot, sources, None, phase, code_precheck=code_precheck,
                           log_thread_id=log_thread_id)
        attempt.result = ((sources, decision),)
        attempt.write()
    except Exception as error:
        _failed(error)


@contextmanager
def run(snapshot, sources, images=None, phase='extract', *, topic_id=None,
        code_precheck=None, log_thread_id=None, ref_sources=None):
    """Isolate accounting failures; never suppress exceptions from evaluation.

    topic_id names the referenced topic when its refs differ from the Turn root's
    namespace. Omitting it retains the Turn root; the Turn and its budget stay intact.
    """
    attempt = None
    token = None
    try:
        from .thinking import CURRENT as THINKING
        turn = THINKING.get()
        if turn is not None and turn.key.startswith('rules:') and sources:
            attempt = _Attempt(turn, snapshot, sources, images, phase, topic_id,
                               code_precheck=code_precheck, log_thread_id=log_thread_id,
                               ref_sources=ref_sources)
            token = CURRENT.set(attempt)
    except Exception as error:
        _failed(error)
    try:
        yield attempt
    finally:
        try:
            if token is not None:
                CURRENT.reset(token)
            if attempt is not None and not attempt.replaced:
                attempt.write()
        except Exception as error:
            _failed(error)


def note_call(provider, model, role, outcome, usage=None, cost=0):
    """Accumulate actual UsageRecorder calls in the current evaluation only."""
    try:
        attempt = CURRENT.get()
        if attempt is None:
            return
        values = attempt.values
        usage = usage or {}
        values.update(provider=provider, model=model)
        values['model_calls'] += 1
        for target, source in (('tokens_in', 'prompt_tokens'), ('tokens_out', 'completion_tokens'),
                               ('cache_hit_tokens', 'cache_hit_tokens')):
            count = usage.get(source, 0)
            values[target] += count if type(count) is int and count >= 0 else 0
        values['cost'] += cost
        values['fallback'] |= int(role == 'fallback')
        if outcome != 'ok':
            values['error'] = _code(outcome)
    except Exception as error:
        _failed(error)


def note_proposal(groups, snapshot):
    """Project validated proposal metadata; discard all field/task text and unknown keys."""
    try:
        attempt = CURRENT.get()
        if attempt is None or groups is None:
            return
        intents = (snapshot.intents - {'unknown'}) | {'analyze', 'status'}
        lifecycle_kinds = {'evidence', 'recurrence', 'unclear', 'reused_success'}
        proposal = []
        for ids, facts in groups:
            intent = facts.get('intent', {}).get('value')
            kind = facts.get('lifecycle', {}).get('kind')
            proposal.append(dict(message_ids=_ids(ids), intent=intent if intent in intents else 'other',
                fields={name: _ids(facts[name]['source_ids']) for name in sorted(snapshot.text_fields | {'intent', 'media_ids'})
                        if name in facts},
                media_ids=_ids(facts.get('media_ids', {}).get('value', [])),
                has_task=bool(facts.get('task')), lifecycle_kind=kind if kind in lifecycle_kinds else None))
        attempt.values['proposal_json'] = _json(proposal)
    except Exception as error:
        _failed(error)


def _tool_args(args):
    """Describe argument structure without retaining strings or nested content."""
    if args is None:
        return None
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except ValueError:
            pass

    def shape(value):
        types = {type(None): 'null', bool: 'boolean', int: 'integer', float: 'number',
                 str: 'string', list: 'array', dict: 'object'}
        result = dict(type=types.get(type(value), 'unknown'))
        if type(value) in (str, list, dict):
            result['length'] = len(value)
        return result

    result = shape(args)
    if type(args) is dict:
        result['arguments'] = {}
        for name, value in args.items():
            if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,63}', name):
                continue
            item = shape(value)
            if (name == 'id' or name.endswith('_id')) and type(value) is int:
                item['id'] = value
            elif (name == 'ids' or name.endswith('_ids')) and type(value) is list and all(type(i) is int for i in value):
                item['ids'] = value
            result['arguments'][name] = item
    return _json(result)


def record_tool_call(store, run_id, seq, tool, args_json=None, result_bytes=0, latency_ms=0, error=None,
                     *, result_json=None, started_at=None, args_full_json=None):
    """Store the legacy argument projection and optional redacted admin detail.

    Only integer values named id/*_id and integer lists named ids/*_ids survive.
    Nested object/array content and non-identifier argument names are discarded.
    args_json accepts JSON text or a decoded JSON value; invalid JSON retains only
    the input's type and length. None remains SQL NULL.
    """
    try:
        values = (run_id, seq, tool, _tool_args(args_json), result_bytes, latency_ms, _code(error),
                  bounded_text(result_json), started_at, bounded_capture(args_full_json, 16384))
        with store._lock:
            if store.connection.in_transaction:
                LOG.warning('Classification accounting skipped: active transaction')
                return
            with store.connection:
                store.connection.execute('''INSERT INTO classification_tool_calls
                    (run_id,seq,tool,args_json,result_bytes,latency_ms,error,result_json,started_at,args_full_json)
                    VALUES (?,?,?,?,?,?,?,?,?,?)''',
                    values)
    except Exception as failure:
        _failed(failure)


def record_model_call(store, run_id, seq, *, link, started_at, model=None, latency_ms=0,
                      timeout_s=None, finish_reason=None, prompt_tokens=0, completion_tokens=0,
                      reasoning_tokens=None, cached_tokens=0, reasoning_text=None, content_text=None,
                      tool_calls_json=None, error_class=None, schema_error_kind=None):
    """Store one model attempt with caller-redacted text, on a best-effort basis."""
    try:
        values = (run_id, seq, link, model, started_at, latency_ms, timeout_s, finish_reason, prompt_tokens,
                  completion_tokens, reasoning_tokens, cached_tokens, bounded_text(reasoning_text),
                  bounded_text(content_text), bounded_text(tool_calls_json), _code(error_class),
                  _code(schema_error_kind))
        with store._lock:
            if store.connection.in_transaction:
                LOG.warning('Classification accounting skipped: active transaction')
                return
            with store.connection:
                store.connection.execute('''INSERT INTO classification_model_calls
                    (run_id,seq,link,model,started_at,latency_ms,timeout_s,finish_reason,prompt_tokens,
                     completion_tokens,reasoning_tokens,cached_tokens,reasoning_text,content_text,
                     tool_calls_json,error_class,schema_error_kind) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                    values)
    except Exception as failure:
        _failed(failure)


def maintain(store, retention_days=90, *, config=None):
    """Hourly bounded deletions and recent single-group ticket-link backfill."""
    try:
        if config is not None:
            retention_days = getattr(getattr(config, 'assistant', None), 'retention_days', 90)
        with store._lock:
            # Every periodic call may flush; only retention/backfill are hourly.
            connection = store.connection
            if connection.in_transaction:
                return
            flush(store)
            if connection.in_transaction:
                return
            now = store.clock().timestamp()
            previous = getattr(store, '_classification_maintained_at', None)
            if previous is not None and now - previous < 3600:
                return
            cutoff = now - retention_days * 86400
            while True:
                with connection:
                    connection.execute('''DELETE FROM classification_tool_calls WHERE run_id IN
                        (SELECT run_id FROM classification_runs WHERE created_at < ? ORDER BY run_id LIMIT 5000)''', (cutoff,))
                    connection.execute('''DELETE FROM classification_model_calls WHERE run_id IN
                        (SELECT run_id FROM classification_runs WHERE created_at < ? ORDER BY run_id LIMIT 5000)''', (cutoff,))
                    connection.execute('''DELETE FROM classification_run_sources WHERE run_id IN
                        (SELECT run_id FROM classification_runs WHERE created_at < ? ORDER BY run_id LIMIT 5000)''', (cutoff,))
                    deleted = connection.execute('''DELETE FROM classification_runs WHERE run_id IN
                        (SELECT run_id FROM classification_runs WHERE created_at < ? ORDER BY run_id LIMIT 5000)''', (cutoff,)).rowcount
                if deleted < 5000:
                    break
            from . import collection_log
            with connection:
                collection_log.prune(connection, cutoff)
                rows = connection.execute('''SELECT run_id,chat_id,message_ids FROM classification_runs
                    WHERE ticket_ref IS NULL AND id_space='message' AND outcomes_json IS NULL
                    AND final_state IN ('READY','APPEND','APPEND_CLOSED','RELATED') AND created_at > ?''',
                    (now - 7 * 86400,)).fetchall()
                for run_id, chat_id, encoded in rows:
                    urls = set()
                    for mid in json.loads(encoded):
                        row = connection.execute('SELECT issue_url FROM messages WHERE chat_id=? AND message_id=?',
                                                 (chat_id, mid)).fetchone()
                        if row and row[0]:
                            urls.add(row[0])
                    if len(urls) == 1:
                        connection.execute('UPDATE classification_runs SET ticket_ref=? WHERE run_id=?',
                                           (urls.pop(), run_id))
            store._classification_maintained_at = now
    except Exception as error:
        _failed(error)


def daily_summary(connection, days=30, tz='+07:00'):
    """Read-only aggregates for local calendar days, including today."""
    if type(days) is not int or days < 1:
        raise ValueError('days must be a positive integer')
    match = re.fullmatch(r'([+-])(\d{2}):(\d{2})', tz)
    if not match or int(match[2]) > 23 or int(match[3]) > 59:
        raise ValueError('tz must be a signed HH:MM offset')
    offset = (int(match[2]) * 60 + int(match[3])) * (1 if match[1] == '+' else -1)
    zone = timezone(timedelta(minutes=offset))
    today = datetime.now(zone).replace(hour=0, minute=0, second=0, microsecond=0)
    start = (today - timedelta(days=days - 1)).timestamp()
    end = (today + timedelta(days=1)).timestamp()
    tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    result = dict(tz=tz, days=days, model_usage=[], classification_runs=[])
    modifier = f'{offset:+d} minutes'
    if 'model_usage' in tables:
        columns = ('day', 'provider', 'model', 'calls', 'ok', 'error', 'prompt_tokens',
                   'completion_tokens', 'cache_hit_tokens', 'cost')
        rows = connection.execute('''SELECT date(ts,'unixepoch',?),provider,model,count(*),
            sum(CASE WHEN outcome='ok' THEN 1 ELSE 0 END),sum(CASE WHEN outcome='ok' THEN 0 ELSE 1 END),
            sum(prompt_tokens),sum(completion_tokens),sum(cache_hit_tokens),sum(cost)
            FROM model_usage WHERE ts>=? AND ts<? GROUP BY 1,2,3 ORDER BY 1,2,3''', (modifier, start, end))
        result['model_usage'] = [dict(zip(columns, row)) for row in rows]
    if 'classification_runs' in tables:
        columns = ('day', 'phase', 'final_state', 'runs', 'runs_with_model_calls', 'reasoning_tokens')
        rows = connection.execute('''SELECT date(created_at,'unixepoch',?),phase,final_state,count(*),
            sum(CASE WHEN model_calls>0 THEN 1 ELSE 0 END),sum(reasoning_tokens)
            FROM classification_runs WHERE created_at>=? AND created_at<? GROUP BY 1,2,3 ORDER BY 1,2,3''',
            (modifier, start, end))
        result['classification_runs'] = [dict(zip(columns, row)) for row in rows]
    return result

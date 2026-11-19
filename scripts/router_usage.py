"""Read and attribute 9router billing data for offline evaluation scripts."""

from collections import Counter
from contextlib import closing, contextmanager
from functools import wraps
import json
from pathlib import Path
import sqlite3
import time


DEFAULT_DB = Path('~/.9router/db/data.sqlite')
TOKEN_FIELDS = ('cached_tokens', 'cache_creation_input_tokens', 'reasoning_tokens')
BILLING_FIELDS = ('prompt_tokens', 'cached_tokens', 'cost_usd')


class RouterUsageError(ValueError):
    pass


class RouterUsage:
    def __init__(self, db_path):
        self.path = Path(db_path).expanduser().resolve()

    @contextmanager
    def _connect(self):
        uri = self.path.as_uri() + '?mode=ro'
        with closing(sqlite3.connect(uri, uri=True, timeout=.25)) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute('PRAGMA query_only=ON')
            yield connection

    def since_id(self):
        with self._connect() as connection:
            return connection.execute('SELECT COALESCE(MAX(id), 0) FROM usageHistory').fetchone()[0]

    def rows_after(self, since_id):
        with self._connect() as connection:
            rows = connection.execute(
                'SELECT id, timestamp, provider, model, status, promptTokens, completionTokens, cost, tokens '
                'FROM usageHistory WHERE id > ? ORDER BY id', (since_id,))
            return [self._parse(row) for row in rows]

    @staticmethod
    def _parse(row):
        result = dict(row)
        try:
            tokens = json.loads(result.pop('tokens'))
        except (ValueError, TypeError, RecursionError):
            tokens = None
        for name in TOKEN_FIELDS:
            value = tokens.get(name) if isinstance(tokens, dict) else None
            result[name] = value if type(value) is int and value >= 0 else None
        return result

    def protect_output(self, output):
        output = Path(output).resolve()
        for path in (self.path, *(Path(str(self.path) + suffix) for suffix in ('-wal', '-shm', '-journal'))):
            if output == path or (output.exists() and path.exists() and output.samefile(path)):
                raise RouterUsageError('Output must be separate from the router usage database and its sidecars.')


def add_db_argument(parser):
    parser.add_argument('--router-usage-db', type=Path, default=DEFAULT_DB, metavar='PATH',
                        help='Read 9router billing from SQLite (default: ~/.9router/db/data.sqlite if readable)')


def open_usage(db_path, *, output=None):
    if db_path is None:
        return None
    try:
        usage = RouterUsage(db_path)
        if output is not None:
            usage.protect_output(output)
        usage.since_id()
        return usage
    except RouterUsageError:
        raise
    except (OSError, sqlite3.Error, RuntimeError, ValueError):
        if db_path == DEFAULT_DB:
            return None
        raise RouterUsageError('Cannot read the router usage database.') from None


def _model_name(model):
    return model.removeprefix('ag/').removeprefix('cx/')


def _billing(row):
    return dict(prompt_tokens=row['promptTokens'], completion_tokens=row['completionTokens'],
                cached_tokens=row['cached_tokens'], cost_usd=row['cost'])


def billing_totals(rows):
    totals = {}
    for field in BILLING_FIELDS:
        values = [row[field] for row in rows]
        totals[field] = sum(values) if values and all(value is not None for value in values) else None
    prompt, cached = totals['prompt_tokens'], totals['cached_tokens']
    totals['cache_ratio'] = cached / prompt if prompt and cached is not None else None
    return totals


def _align_calls(calls, rows):
    """Consume rows in order, requiring a row for each returned completion."""
    remaining = iter(rows)
    row = next(remaining, None)
    matched = []
    for model, completed in calls:
        if (row is not None and row['model'] == _model_name(model)
                and row['status'] and (row['status'] == 'ok') == completed):
            matched.append(row)
            row = next(remaining, None)
        elif completed:
            return None
        else:
            matched.append(None)
    return matched if row is None else None


class UsageWindow:
    """Track the router transport calls within one sequential measurement window."""

    def __init__(self, usage, *, settle_s=3.0):
        self.usage = usage
        self.settle_s = settle_s
        self._pending = Counter()
        self._last_seen_id = 0
        self.start()

    @classmethod
    def from_db(cls, db_path, *, output=None):
        if isinstance(db_path, cls):
            if output is not None and db_path.usage is not None:
                db_path.usage.protect_output(output)
            return db_path
        return cls(open_usage(db_path, output=output))

    def start(self):
        self.calls = []
        self.since = None

    def _snapshot(self):
        if not self._pending:
            return self.usage.since_id()
        # Rows already visible between windows cannot belong to the next call.
        rows = self.usage.rows_after(self._last_seen_id)
        self._pending -= self._completed_models(rows)
        self._advance(rows)
        return self._last_seen_id

    @staticmethod
    def _completed_models(rows):
        return Counter(row['model'] for row in rows if row['status'] == 'ok')

    def _advance(self, rows):
        if rows:
            self._last_seen_id = rows[-1]['id']

    def _settled_rows(self, expected):
        deadline = time.monotonic() + self.settle_s
        while True:
            rows = self.usage.rows_after(self.since)
            observed = self._completed_models(rows)
            remaining = deadline - time.monotonic()
            if observed.total() >= expected or remaining <= 0:
                return rows, observed
            time.sleep(min(.1, remaining))

    def wrap(self, send):
        if self.usage is None:
            return send

        @wraps(send)
        def tracked(model, *args, **kwargs):
            if not self.calls:
                try:
                    self.since = self._snapshot()
                    self._last_seen_id = self.since
                except sqlite3.Error:
                    pass  # Report this window as ambiguous without interrupting the call.
            completed = False
            try:
                reply = send(model, *args, **kwargs)
                completed = True
                return reply
            finally:
                self.calls.append((model, completed))

        return tracked

    def finish(self):
        if not self.calls:
            return dict(usage_source='response')
        expected = Counter(_model_name(model) for model, completed in self.calls if completed)
        rows, observed = [], Counter()
        if self.since is not None:
            try:
                rows, observed = self._settled_rows(expected.total())
            except sqlite3.Error:
                self.since = None
        # A same-model row still in flight from an earlier window can look identical
        # to a current completion. Keep the unresolved count across snapshots.
        overlaps = bool(self._pending & observed)
        self._pending = self._pending + expected - observed
        self._advance(rows)

        if self.since is None or observed.total() < expected.total():
            return dict(usage_source='ambiguous', reason='late_or_missing_row')
        if overlaps or len(rows) > len(self.calls) or observed.total() > expected.total():
            return dict(usage_source='ambiguous', reason='foreign_row')

        # Both directions must agree so a logged error cannot be assigned
        # arbitrarily between repeated failures for the same model.
        matched = _align_calls(self.calls, rows)
        if matched is None:
            return dict(usage_source='ambiguous', reason='model_mismatch')
        latest = _align_calls(reversed(self.calls), reversed(rows))
        if latest is None or matched != latest[::-1]:
            return dict(usage_source='ambiguous', reason='foreign_row')

        calls, billed = [], []
        for (model, _), row in zip(self.calls, matched):
            if row is None:
                calls.append(dict(model=model, usage_source='none'))
                continue
            call = dict(model=model, usage_source='router_db', status=row['status'], **_billing(row))
            call.update(billing_totals([call]))
            calls.append(call)
            billed.append(call)
        if billed:
            return dict(usage_source='router_db', router_calls=calls, router_usage=billing_totals(billed))
        return dict(usage_source='none', router_calls=calls)


def billing_metrics(usage):
    ratio, cost = usage['cache_ratio'], usage['cost_usd']
    return {
        'Router-billed prompt tokens': usage['prompt_tokens'] if usage['prompt_tokens'] is not None else 'n/a',
        'Router cached tokens': usage['cached_tokens'] if usage['cached_tokens'] is not None else 'n/a',
        'Router cache ratio': f'{ratio:.1%}' if ratio is not None else 'n/a',
        'Router cost (USD)': f'${cost:.9f}' if cost is not None else 'n/a',
    }


def print_usage(row, *, response=False):
    if response:
        prompt, output, cached = (row.get(key, 0) or 0 for key in ('tokens_in', 'tokens_out', 'cached_tokens'))
        print(f'  Response prompt / output / cached tokens: {prompt} / {output} / {cached}')
    print(f"  usage_source: {row['usage_source']}")
    if row['usage_source'] == 'ambiguous':
        print(f"  Reason: {row['reason']}")
    if row['usage_source'] == 'router_db':
        print('  ' + ' | '.join(f'{name}: {value}' for name, value in billing_metrics(row['router_usage']).items()))
        statuses = [call['status'] for call in row['router_calls'] if call['usage_source'] == 'router_db']
        print('  Router status: ' + json.dumps(statuses))


def billing_summary(rows):
    matched = [row['router_usage'] for row in rows if row.get('usage_source') == 'router_db']
    return {
        **billing_metrics(billing_totals(matched)),
        'Router DB measurements': len(matched),
        'Ambiguous measurements': sum(row.get('usage_source') == 'ambiguous' for row in rows),
    }

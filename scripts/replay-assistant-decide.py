#!/usr/bin/env python3
"""Compare the assistant with classification history from a standalone SQLite copy."""

import argparse
from collections import Counter
from contextlib import closing
from dataclasses import replace
from functools import partial
from hashlib import sha256
from importlib import import_module
import json
from pathlib import Path, PurePosixPath
import re
import sqlite3
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parent))
p8 = import_module('eval-assistant-p8')

from router_usage import RouterUsageError, UsageWindow, add_db_argument, print_usage
from tg_triage import router_transport
from tg_triage.store import Store, _to_message


LIVE_ROOT = PurePosixPath('/var/lib/agentfleet/tg')


class ReplayInputError(ValueError):
    pass


def _live_path(path):
    posix = path.as_posix()
    if path.drive:
        posix = posix.removeprefix(path.drive.replace('\\', '/')).casefold()
    return PurePosixPath(posix).is_relative_to(LIVE_ROOT)


def _copy_path(path):
    candidate = Path(path).expanduser()
    if _live_path(candidate) or _live_path(candidate.resolve()):
        raise ReplayInputError('Replay refuses the live database path. Pass a SQLite copy outside /var/lib/agentfleet/tg.')
    source = candidate.resolve()
    if not source.is_file():
        raise ReplayInputError('Replay requires an existing SQLite copy.')
    wal = Path(str(source) + '-wal')
    if wal.exists() and wal.stat().st_size:
        raise ReplayInputError('Replay requires a standalone SQLite backup with its WAL checkpointed.')
    return source


def _output_path(path, source, config_path):
    output = Path(path).resolve()
    protected = [source, Path(config_path).resolve()]
    protected.extend(Path(str(source) + suffix) for suffix in ('-wal', '-shm', '-journal'))
    if (_live_path(output) or output in protected or
            (output.exists() and any(item.exists() and output.samefile(item) for item in protected))):
        raise ReplayInputError('Replay output must be separate from the database, its sidecars, and the config.')
    return output


def _sources(db, run):
    if run['id_space'] != 'message':
        return (), 'legacy_id_space'
    try:
        ids = json.loads(run['message_ids'])
    except (TypeError, ValueError):
        return (), 'invalid_message_ids'
    if (not isinstance(ids, list) or not ids
            or any(type(mid) is not int or not 0 < mid < 2**63 for mid in ids)
            or len(set(ids)) != len(ids)):
        return (), 'invalid_message_ids'
    rows = [db.execute('SELECT * FROM messages WHERE chat_id=? AND message_id=?',
                       (run['chat_id'], mid)).fetchone() for mid in ids]
    if any(row is None for row in rows):
        return (), 'missing_source'
    return tuple(_to_message(row) for row in rows), None


def _ticket_number(ref, repo):
    if ref is None:
        return None
    if re.fullmatch(r'#?[1-9][0-9]*', str(ref)):
        return int(str(ref).lstrip('#'))
    match = re.fullmatch(r'https://github\.com/([^/]+/[^/]+)/issues/([1-9][0-9]*)/?', str(ref), re.I)
    return int(match[2]) if match and match[1].casefold() == repo.casefold() else ref


def _comparison(groups, row, repo):
    actual = groups[0].state if len(groups) == 1 else 'MULTI' if groups else 'NO_DECISION'
    actual_template = p8.recorded_template(groups[0].template) if len(groups) == 1 else None
    expected_template = p8.recorded_template(row['template'])
    # A dry run can compare APPEND targets. READY never allocates a new ticket number.
    target_numbers = sorted({group.target['number'] for group in groups
                             if group.state in ('APPEND', 'APPEND_CLOSED') and group.target})
    actual_ticket = target_numbers[0] if len(target_numbers) == 1 else target_numbers or None
    expected_ticket = _ticket_number(row['ticket_ref'], repo)
    expected_create = row['final_state'] == 'READY'
    if row['final_state'] == 'MULTI':
        try:
            outcomes = json.loads(row['outcomes_json'] or '[]')
        except (TypeError, ValueError):
            outcomes = []
        states = [group.get('state') or group.get('final_state') for group in outcomes
                  if isinstance(group, dict)] if isinstance(outcomes, list) else []
        expected_create = 'READY' in states if states and all(states) else None
    return dict(actual_state=actual, actual_template=actual_template, actual_ticket_ref=actual_ticket,
                expected_template=expected_template, actual_groups=[
                    dict(message_ids=list(group.message_ids), state=group.state, template=group.template)
                    for group in groups],
                ticket_ref_agrees=(expected_ticket == actual_ticket
                                   if groups and all(g.state != 'READY' for g in groups)
                                   and row['final_state'] != 'READY' else None),
                ticket_agrees=bool(groups) and any(g.state in p8.TICKET_STATES for g in groups)
                    == (row['final_state'] in p8.TICKET_STATES),
                creation_agrees=(bool(groups) and any(g.state == 'READY' for g in groups) == expected_create
                                 if expected_create is not None else None),
                template_agrees=bool(groups) and actual_template == expected_template,
                passed=bool(groups) and actual == row['final_state'] and actual_template == expected_template)


def _print_confusion(rows):
    counts = Counter((row['expected_state'] or 'UNRECORDED', row['actual_state']) for row in rows)
    expected = sorted({state for state, _ in counts})
    actual = sorted({state for _, state in counts})
    print('Confusion matrix (recorded rows, replay columns)')
    print('\t'.join(['Recorded', *actual]))
    for state in expected:
        print('\t'.join([state, *(str(counts[state, result]) for result in actual)]))
    tickets = [row['ticket_ref_agrees'] for row in rows if row['ticket_ref_agrees'] is not None]
    print(f'Ticket reference agreement {sum(tickets)}/{len(tickets)}')


def run_replay(*, db, config, limit=200, out, runtime_factory=None, pack_mode='full', append_output=False,
               router_usage_db=None):
    source = _copy_path(db)
    output = _output_path(out, source, config)
    if limit < 1:
        raise ReplayInputError('The replay limit must be positive.')
    window = UsageWindow.from_db(router_usage_db, output=output)
    usage = window.usage
    settings = p8.offline_config(config)
    snapshot, pack = p8._load_decision_rules(), p8.load_pack('decide', pack_mode)
    rows = []
    with tempfile.TemporaryDirectory(prefix='assistant-replay-') as directory:
        audit_path = str(Path(directory) / 'replay.sqlite3')
        assistant = replace(settings.assistant, mode='live', authority='decide', enabled=True, pack_mode=pack_mode,
                            decide_chat_ids=(), github_token=None, github_token_file='', github_repos=())
        config = replace(settings, mode='dry-run', db_path=audit_path, assistant=assistant,
                         gh_token='', telegram=replace(settings.telegram, chat_ids=(), listen_all_groups=False,
                                                       ops_chat_id=None, source_reactions=False))
        store = Store(audit_path)
        try:
            transport = {}
            if usage is not None:
                transport['chat'] = window.wrap(partial(router_transport.chat, assistant.router_base_url,
                                                        assistant.router_key))
            runtime = p8.decide_runtime(config, store, pack, runtime_factory, **transport)
            rules = p8.rule_lookup(audit_path, pack.text, snapshot)
            seen = set()
            uri = source.as_uri() + '?mode=ro&immutable=1'
            with closing(sqlite3.connect(uri, uri=True)) as original:
                original.row_factory = sqlite3.Row
                original.execute('PRAGMA query_only=ON')
                batches = original.execute('SELECT * FROM classification_runs ORDER BY run_id DESC LIMIT ?',
                                           (limit,)).fetchall()
                with output.open('a' if append_output else 'w', encoding='utf-8') as artifact:
                    for batch in batches:
                        window.start()
                        sources, skipped = _sources(original, batch)
                        groups, metrics = [], {}
                        if sources:
                            # Restrict local read tools to this batch. Historical DB state
                            # would reveal later messages and decisions to the model.
                            with store.connection:
                                store.connection.execute('DELETE FROM messages')
                            for message in sources:
                                store.enqueue(message)
                            try:
                                job = p8.decide_job(config, snapshot, rules, sources)
                            except p8.FixtureError:
                                skipped = 'batch_cannot_be_serialized'
                            else:
                                fingerprint = job['fingerprint']
                                if fingerprint in seen:
                                    # The live attempts cache would suppress a recorded retry.
                                    job['fingerprint'] = sha256(f'{fingerprint}:{batch["run_id"]}'.encode()).hexdigest()
                                seen.add(fingerprint)
                                groups, metrics = p8.decide_result(runtime, store, job)
                        row = dict(run_id=batch['run_id'], chat_id=batch['chat_id'],
                                   message_ids=[message.message_id for message in sources],
                                   expected_state=batch['final_state'], expected_ticket_ref=batch['ticket_ref'],
                                   skipped=skipped, comparable=not skipped and batch['final_state'] is not None,
                                   pack_mode=pack_mode, pack_fingerprint=pack.fingerprint,
                                   core_pack_fingerprint=pack.core.fingerprint if pack.core else None,
                                   **_comparison(groups, batch, config.gh_repo), **metrics, **window.finish())
                        rows.append(row)
                        artifact.write(json.dumps(row, ensure_ascii=False) + '\n')
                        artifact.flush()
                        if usage is not None:
                            print(f"Batch {row['run_id']}")
                            print_usage(row, response=True)
        finally:
            store.close()
    _print_confusion(rows)
    print(f"Skipped batches {sum(row['skipped'] is not None for row in rows)}/{len(rows)}")
    p8.print_summary(rows, usage_enabled=usage is not None)
    p8.print_pack_comparison({pack_mode: rows}, usage_enabled=usage is not None)
    return rows


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', required=True)
    parser.add_argument('--config', required=True)
    parser.add_argument('--limit', type=int, default=200)
    parser.add_argument('--out', required=True)
    parser.add_argument('--pack-mode', choices=('full', 'core'), default='full')
    parser.add_argument('--compare', action='store_true', help='Replay both pack modes against the same copy')
    add_db_argument(parser)
    args = parser.parse_args(argv)
    try:
        options = vars(args)
        compare = options.pop('compare')
        selected = options.pop('pack_mode')
        modes = ('full', 'core') if compare else (selected,)
        if compare:
            options['router_usage_db'] = UsageWindow.from_db(options['router_usage_db'], output=options['out'])
        results = {mode: run_replay(**options, pack_mode=mode, append_output=index > 0)
                   for index, mode in enumerate(modes)}
        if compare:
            p8.print_pack_comparison(results)
    except (ReplayInputError, p8.DecideRuntimeUnavailable, RouterUsageError) as error:
        print(str(error), file=sys.stderr)
        return 2
    except Exception as error:
        print('Replay failed: ' + type(error).__name__, file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

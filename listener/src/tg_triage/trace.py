"""Bounded offline incident trace. No migration, model, Telegram session or HTTP."""
import json
import re

from .config import ConfigError
from .issue_format import redact

REPO = re.compile(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+')
REASON_CODES = frozenset('''semantic_review_rejected context_budget_exhausted decision_deadline_exhausted
    lookup_budget_exhausted model_call_budget_exhausted initial_partition initial_result
    accounting_verified followup_result update split clarify ignore confirmed_binding
    conflicting_binding partial_binding invalid_accounting invalid_count invalid_output
    invalid_source_roles unproven_binding unproven_count provider_unavailable
    ambiguous_enrichment invalid_enrichment_sources vision_unavailable unresolved_role
    ingress_validation ingress_scope_clarification ingress_packing ingress_review ingress_candidates ingress_result semantic_new semantic_continue semantic_clarify semantic_chat semantic_analyze semantic_status cached_analysis_invalid document_read_failed document_task_incomplete document_context_budget_exhausted document_processing_failed cached_document_invalid
    ingress_pixels_unavailable invalid_ingress_output closed_episode_unclear recurrence_unproven
    lifecycle_unavailable lifecycle_lookup_failed lifecycle_target_unobserved lifecycle_foreign_sources related_create
    cached_repo_changed cached_sources_missing cached_reply_authority_revoked reply_authority_required batch_scope_correction_unclear'''.split())
FIELDS = {
    'followup_job_sources': 'job_id chat_id message_id',
    'triage_current_topics': 'topic_id root_key chat_id case_key ordinal issue_number scope_status active version',
    'triage_current_topic_events': 'topic_id version recorded_at',
    'autonomous_routes': 'chat_id message_id recorded_at action candidate_key case_key reason_code',
    'autonomous_plans': 'chat_id anchor_id case_key recorded_at',
    'autonomous_confirmations': 'chat_id message_id issue_number status notice_message_id recorded_at',
    'autonomous_clarifications': 'case_key chat_id message_id recorded_at',
    'autonomous_outages': 'chat_id cycle anchor_message_id status started_at closed_at',
    'followup_episodes': 'root_key parent_root_key job_id chat_id anchor_message_id issue_number repo created_at',
    'followup_episode_sources': 'chat_id message_id root_key',
    'messages': 'chat_id message_id reply_to_id case_key group_key decision tier type issue_number received_at processed_at has_image has_document',
    'cases': 'case_key chat_id first_message_id status decision message_count issue_number',
    'groups': 'group_key case_key chat_id first_message_id status decision message_count issue_number',
    'group_notices': 'chat_id message_id notice_message_id kind status reserved_at finished_at',
    'followup_roots': 'root_key chat_id source_message_id group_key case_key issue_number',
    'followup_jobs': 'id chat_id message_id root_key status',
    'followup_operations': 'job_id ordinal kind status reserved_at result',
    'attachments': 'id case_key message_id status called_at',
    'reconciliation': 'case_key received issue noise needs_human failed groups issues_created balanced status chat pending_review',
    'triage_topics': 'topic_id chat_id source_kind source_key case_key group_key source_message_id decision issue_number active version membership_truncated split_partition_not_projected',
    'triage_topic_messages': 'topic_id chat_id message_id role provenance active',
    'triage_topic_events': 'topic_id version recorded_at event',
}


def validate_repo(repo):
    if not isinstance(repo, str) or not REPO.fullmatch(repo) or any(p in ('.', '..') for p in repo.split('/')):
        raise ConfigError('trace: --repo or GH_REPO must identify owner/repo')
    return repo


def _json(value):
    try:
        result = json.loads(value or '{}')
        return result if isinstance(result, dict) else {}
    except (ValueError, TypeError, RecursionError):
        return {}


def _private(value, depth=0):
    """Best-effort credential/PII redaction; opt-in output remains private."""
    if depth>=12:
        return '[nested content truncated]'
    if isinstance(value, str):
        value = re.sub(r'(?i)\b(?:gh[pousr]_[A-Za-z0-9_]+|github_pat_[A-Za-z0-9_]+|sk-[A-Za-z0-9_-]+)\b', '[credential]', value)
        value = re.sub(r'(?i)(?:bearer\s+\S+|(?:token|password|secret|api[_-]?key)\s*[:=]\s*[^\s,;]+)', '[credential]', value)
        value = re.sub(r'https?://[^\s<>]+', '[url]', value)
        return redact(''.join(c for c in value if c in '\n\t' or ord(c) >= 32))[:4000]
    if isinstance(value, list):
        return [_private(v,depth+1) for v in value[:100]]
    if isinstance(value, dict):
        return {k: _private(v,depth+1) for k, v in list(value.items())[:100]
                if not re.search(r'(?i)token|password|secret|api.?key|reasoning|chain.?of.?thought', str(k))}
    return value


class Reader:
    def __init__(self, connection, limit, include_text):
        self.db, self.limit, self.remaining, self.include_text = connection, limit, limit, include_text
        self.read_remaining, self.queries_remaining = limit * 8, limit * 12
        self.bytes_remaining = 512000 if include_text else 256000
        self.read_bytes_remaining = 8 * 1024 * 1024
        self.tables = {r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table' LIMIT 1000")}
        self.gaps, self.truncated = [], set()

    def rows(self, table, where='1', values=(), order=None):
        if self.read_remaining <= 0 or self.queries_remaining <= 0 or self.read_bytes_remaining <= 0:
            self.truncated.add('read_budget')
            return []
        self.queries_remaining -= 1
        if table not in self.tables:
            if f'missing_table:{table}' not in self.gaps:
                self.gaps.append(f'missing_table:{table}')
            return []
        available = {r[1] for r in self.db.execute(f'PRAGMA table_info({table})')}
        fields = FIELDS.get(table,'turn_key root_key version record').split()
        needed = dict(messages=['issue_url'],followup_jobs=['plan','result'],followup_roots=['context_state','siblings','split_plan'],followup_episodes=['source_ids'],autonomous_plans=['source_ids'],autonomous_routes=['metadata'],triage_current_topics=['source_ids','human_bindings','unresolved_ids'])
        fields += needed.get(table,[])
        if self.include_text:
            fields += ['text','sender_name','title','summary','error','plan','payload','context_state','metadata']
        columns = sorted(set(fields)&available)
        # Bound large stored JSON/text before fetching, including legacy malformed records.
        selection = ','.join(f'CASE WHEN typeof("{c}")=\'text\' THEN substr("{c}",1,32769) ELSE "{c}" END AS "{c}"' for c in columns)
        if table=='followup_operations' and 'payload' in available:
            selection += ',(payload IS NOT NULL AND length(payload)>0) AS payload_present'
        cap = min(self.limit, self.read_remaining)
        # Legacy snapshots can have fewer columns; callers declare their predicate needs.
        try:
            cursor = self.db.execute(f'SELECT {selection} FROM {table} WHERE {where}' +
                                     (f' ORDER BY {order}' if order else '') + ' LIMIT ?',
                                     (*values, cap + 1))
            result=[]
            for raw in cursor:
                row=dict(raw)
                size=sum(len(v.encode('utf-8')) if isinstance(v,str) else 16 for v in row.values())
                if size>self.read_bytes_remaining:
                    self.truncated.add('read_bytes')
                    self.read_bytes_remaining=0
                    break
                self.read_bytes_remaining-=size
                result.append(row)
        except Exception as error:
            import sqlite3
            if not isinstance(error, sqlite3.OperationalError):
                raise
            self.gaps.append(f'legacy_schema:{table}')
            return []
        if len(result) > cap:
            self.truncated.add(table)
        self.read_remaining -= min(len(result),cap)
        result = [dict(r) for r in result[:cap]]
        for row in result:
            for key, value in row.items():
                if isinstance(value,str) and len(value)>32768:
                    row[key]=value[:32768]
                    self.truncated.add(f'{table}.{key}')
        return result

    def bounded(self, table, value):
        size = len(json.dumps(value,ensure_ascii=False).encode('utf-8'))
        if size > self.bytes_remaining:
            self.truncated.add('output_bytes')
            self.truncated.add(table)
            return False
        self.bytes_remaining -= size
        return True

    def output(self, table, rows):
        if len(rows) > self.remaining:
            self.truncated.add(table)
        selected = rows[:self.remaining]
        self.remaining -= len(selected)
        result = []
        for row in selected:
            value = {k: row[k] for k in FIELDS[table].split() if k in row}
            if table in ('followup_episodes','autonomous_plans'):
                value['source_ids']=_integer_list(row.get('source_ids'))
            if table=='triage_current_topics':
                value['source_ids']=_integer_list(row.get('source_ids'))
                value['unresolved_source_ids']=_integer_list(row.get('unresolved_ids'))
                bindings=_json(row.get('human_bindings'))
                value['human_bindings']={str(k):v for k,v in list(bindings.items())[:1000]
                    if str(k).isdigit() and type(v) is int and 0<=v<1000}
            if table == 'autonomous_routes':
                key=_json(row.get('metadata')).get('decision_turn_key')
                if isinstance(key,str) and re.fullmatch(r'ingress:-?[0-9]+:[0-9]+',key) and key.startswith(f'ingress:{row.get("chat_id")}:'):
                    value['decision_turn_key']=key
                if value.get('reason_code') not in REASON_CODES:
                    value['reason_code']='unrecognized'
            if table == 'followup_operations':
                # Legacy operation results may be prose; only numeric external IDs are metadata.
                if type(value.get('result')) is not int:
                    value.pop('result', None)
                value['payload_present'] = bool(row.get('payload_present'))
            if table == 'followup_jobs':
                plan = _json(row.get('plan'))
                value['action'] = plan.get('action') if plan.get('action') in ('update','split','clarify','ignore') else None
                value['result_present'] = bool(row.get('result'))
            if table == 'followup_roots':
                state = _json(row.get('context_state'))
                snapshot = state.get('managed_snapshot', {})
                value['snapshot_present'] = isinstance(snapshot, dict) and bool(snapshot)
                if isinstance(snapshot, dict):
                    value['snapshot'] = {}
                    if isinstance(snapshot.get('repo'),str) and REPO.fullmatch(snapshot['repo']):
                        value['snapshot']['repo']=snapshot['repo']
                    if type(snapshot.get('issue_number')) is int:
                        value['snapshot']['issue_number']=snapshot['issue_number']
                    if isinstance(snapshot.get('body_sha'),str) and re.fullmatch('[0-9a-f]{64}',snapshot['body_sha']):
                        value['snapshot']['body_sha']=snapshot['body_sha']
                    if isinstance(snapshot.get('source_ids'),list):
                        value['snapshot']['source_ids']=[i for i in snapshot['source_ids'][:1000] if type(i) is int]
                value['siblings'] = _integer_list(row.get('siblings'))
                bindings=state.get('bindings')
                if isinstance(bindings,dict):
                    value['bindings']={str(k):v for k,v in list(bindings.items())[:1000]
                                       if str(k).isdigit() and type(v) is int and 0<=v<1000}
                ids=state.get('unresolved_message_ids')
                if isinstance(ids,list):
                    value['unresolved_message_ids']=[i for i in ids[:1000] if type(i) is int]
            if table=='triage_topics':
                value['projection_scope']='original_intake'
            if self.include_text:
                value['private_content'] = _private({k: row[k] for k in
                    ('text','sender_name','title','summary','error','plan','payload','context_state','metadata') if row.get(k)})
            if self.bounded(table,value):
                result.append(value)
        return result

    def decisions(self, table, root_keys):
        if not root_keys:
            return []
        if table == 'thinking_decisions':
            rows = self.rows(table, f'root_key IN ({_marks(root_keys)})', tuple(root_keys), 'turn_key')
        else:
            # Immutable revisions share the same turn_key with the selected latest decision.
            rows = self.rows(table, f'turn_key IN (SELECT turn_key FROM thinking_decisions WHERE root_key IN ({_marks(root_keys)}))', tuple(root_keys), 'turn_key,version')
        selected = rows[:self.remaining]
        if len(selected) < len(rows):
            self.truncated.add(table)
        self.remaining -= len(selected)
        result = []
        for row in selected:
            record = _json(row.get('record'))
            data = {k: row[k] for k in ('turn_key','root_key','version') if k in row}
            enums=dict(schema={'triage.decision/v1'},harness_owner={'AlexChu'},
                       intent={'initial_partition','initial_result','followup_result','update','split','clarify','ignore','ingress_candidates','ingress_packing','ingress_review','ingress_result','ingress_validation'},
                       status={'planned','done','blocked','ambiguous','dryrun','missing','queued','error','verified'})
            for field in ('schema','harness_owner','intent','status','policy_version','prompt_version'):
                val=record.get(field)
                valid = (isinstance(val,str) and val in enums[field]) if field in enums else (
                    isinstance(val,str) and bool(re.fullmatch('[0-9a-f]{64}' if field=='prompt_version' else r'TG-[0-9.]+TG-[0-9]+/thinking-v[0-9]+',val)))
                if valid:
                    data[field]=val
            if isinstance(record.get('reason_code'),str) and record['reason_code'] in REASON_CODES:
                data['reason_code']=record['reason_code']
            elif 'reason_code' in record:
                data['reason_code']='unrecognized'
            if type(record.get('input_message_id')) is int:
                data['input_message_id']=record['input_message_id']
            budget=record.get('budget')
            if isinstance(budget,dict):
                data['budget']={k:budget[k] for k in ('model_calls_used','max_model_calls','reads_used','max_reads','elapsed_ms')
                                if type(budget.get(k)) is int and budget[k]>=0}
            if isinstance(record.get('validation_errors'),list):
                from .ingress_diagnostics import project
                data['validation_errors']=project(record['validation_errors'])
            elif record.get('intent')=='ingress_validation' and isinstance(record.get('confirmed'),dict):
                from .ingress_diagnostics import project
                data['validation_errors']=project(record['confirmed'].get('errors'))
            if record.get('intent')=='ingress_packing' and isinstance(record.get('confirmed'),dict):
                packed=record['confirmed']
                data['packing']={k:packed[k] for k in ('new_text_cap','candidate_text_cap','text_bytes')
                                 if type(packed.get(k)) is int and packed[k]>=0}
                if type(packed.get('candidate_context_truncated')) is bool:
                    data['packing']['candidate_context_truncated']=packed['candidate_context_truncated']
                for field in ('retained_candidate_ids','omitted_candidate_ids'):
                    if isinstance(packed.get(field),list):
                        data['packing'][field]=[v for v in packed[field][:6] if isinstance(v,str) and re.fullmatch(r'c[1-6]',v)]
            if record.get('intent')=='ingress_review' and isinstance(record.get('confirmed'),dict):
                review=record['confirmed']; safe={}
                safe['source_ids']=[i for i in review.get('source_ids',[])[:20] if type(i) is int] if isinstance(review.get('source_ids'),list) else []
                if review.get('outcome') in ('proposed','accepted','rejected'):
                    safe['outcome']=review['outcome']
                if isinstance(review.get('reason'),str) and review['reason'] in REASON_CODES|{'unlinked_image_competing_history','unlinked_image_partition','host_validated','invalid_output','provider_unavailable','invalid_ingress_output'}:
                    safe['reason']=review['reason']
                safe['groups']=[]
                for group in review.get('groups',[])[:20] if isinstance(review.get('groups'),list) else []:
                    if not isinstance(group,dict) or group.get('action') not in ('NEW','CONTINUE','CLARIFY','CHAT'):
                        continue
                    value=dict(action=group['action'],message_ids=[i for i in group.get('message_ids',[])[:20] if type(i) is int] if isinstance(group.get('message_ids'),list) else [])
                    candidate=group.get('candidate_id')
                    if candidate is None or isinstance(candidate,str) and re.fullmatch(r'c[1-6]',candidate):
                        value['candidate_id']=candidate
                    safe['groups'].append(value)
                data['review']=safe
            if self.include_text:
                data['private_content'] = _private(record)
            if self.bounded(table,data):
                result.append(data)
        return result


def _integer_list(value):
    try:
        result = json.loads(value or '[]')
        return [v for v in result[:100] if type(v) is int] if isinstance(result,list) else []
    except (ValueError, TypeError, RecursionError):
        return []


def _marks(values):
    return ','.join('?' for _ in values)


def trace(store, **kwargs):
    """Read one consistent SQLite snapshot; never commit or recover state."""
    owned_transaction=not store.connection.in_transaction
    if owned_transaction:
        store.connection.execute('BEGIN')
    try:
        return _trace(store,**kwargs)
    finally:
        if owned_transaction:
            store.connection.rollback()


def _trace(store, *, repo, issue=None, chat_id=None, message_id=None, limit=100, include_text=False):
    """Fixed repo + stored associations only; user text/URLs never establish membership.

    Case/group keys expand independently. Issue number alone never merges roots/chats.
    Returned row budget is global, with per-query caps and bounded reply traversal.
    """
    validate_repo(repo)
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise ConfigError('trace: limit must be between 1 and 1000')
    if (issue is None) == (message_id is None) or (message_id is not None and chat_id is None):
        raise ConfigError('trace: choose --issue or --chat-id with --message-id')
    if issue is not None and (type(issue) is not int or issue <= 0):
        raise ConfigError('trace: issue must be positive')
    if message_id is not None and (type(message_id) is not int or message_id<=0 or type(chat_id) is not int):
        raise ConfigError('trace: invalid chat/message ID pair')
    r = Reader(store.connection, limit, include_text)
    if issue is not None:
        where = 'issue_number=? AND issue_url=? COLLATE NOCASE'
        args = [issue, f'https://github.com/{repo}/issues/{issue}']
        if chat_id is not None:
            where += ' AND chat_id=?'; args.append(chat_id)
        seeds = r.rows('messages', where, args, 'chat_id,message_id')
    else:
        seeds = r.rows('messages','chat_id=? AND message_id=?',(chat_id,message_id))
        if not seeds and 'autonomous_confirmations' in r.tables:
            confirmations=r.rows('autonomous_confirmations','chat_id=? AND notice_message_id=? AND status=?',(chat_id,message_id,'sent'))
            if confirmations:
                seeds=r.rows('messages','chat_id=? AND message_id=?',(chat_id,confirmations[0]['message_id']))
        if not seeds:
            notices = r.rows('group_notices','chat_id=? AND notice_message_id=? AND status=?',(chat_id,message_id,'sent'))
            if notices:
                seeds = r.rows('messages','chat_id=? AND message_id=?',(chat_id,notices[0]['message_id']))
    if not seeds:
        r.gaps.append('no_stored_source_for_selector')
    # Each partition is explicit; a historical ungrouped followup does not prove a topic.
    scopes = set()
    sources = {(m['chat_id'],m['message_id']):m for m in seeds}
    for seed in seeds:
        current = seed
        visited = set()
        for depth in range(12):
            pair = (current['chat_id'],current['message_id'])
            if pair in visited:
                r.gaps.append('reply_cycle'); break
            visited.add(pair)
            if current.get('decision') != 'followup' and current.get('case_key'):
                scopes.add((current['chat_id'], 'group_key' if current.get('group_key') else 'case_key', current.get('group_key') or current['case_key']))
                break
            if current.get('decision') == 'followup':
                jobs = r.rows('followup_jobs','chat_id=? AND message_id=?',pair)
                if not jobs:
                    members = r.rows('followup_job_sources','chat_id=? AND message_id=?',pair)
                    for member in members:
                        jobs += r.rows('followup_jobs','chat_id=? AND id=?',(pair[0],member['job_id']))
                if jobs:
                    roots = r.rows('followup_roots','chat_id=? AND root_key=?',(pair[0],jobs[0]['root_key']))
                    if roots:
                        root=roots[0]
                        scopes.add((pair[0], 'group_key' if root.get('group_key') else 'case_key',root.get('group_key') or root['case_key']))
                        break
            reply = current.get('reply_to_id')
            if not reply:
                r.gaps.append('unproven_topic_membership'); break
            notice = r.rows('group_notices','chat_id=? AND notice_message_id=? AND status=?',(pair[0],reply,'sent'))
            target = notice[0]['message_id'] if notice else reply
            ancestors = r.rows('messages','chat_id=? AND message_id=?',(pair[0],target))
            if not ancestors:
                r.gaps.append('missing_reply_ancestor'); break
            current=ancestors[0]; sources[(current['chat_id'],current['message_id'])]=current
        else:
            r.truncated.add('reply_depth')
    sections = {k:[] for k in FIELDS}
    for chat in sorted({chat for chat,_ in sources}):
        sections['autonomous_outages'] += r.rows('autonomous_outages','chat_id=?',(chat,),'cycle')
    root_keys=set()
    for chat,column,key in sorted(scopes):
        originals = r.rows('messages', f'chat_id=? AND {column}=? AND decision!=?',(chat,key,'followup'),'message_id')
        # Do not expand an ungrouped case into other, separately proven groups.
        if column == 'case_key':
            originals = [m for m in originals if not m.get('group_key')]
        sources.update({(m['chat_id'],m['message_id']):m for m in originals})
        roots = r.rows('followup_roots',f'chat_id=? AND {column}=?',(chat,key),'root_key')
        if column == 'case_key':
            roots = [root for root in roots if not root.get('group_key')]
        sections['followup_roots'] += roots
        for root in roots:
            root_keys.add(root['root_key'])
            snapshot = _json(root.get('context_state')).get('managed_snapshot',{})
            if snapshot and (not isinstance(snapshot,dict) or not isinstance(snapshot.get('repo'),str) or snapshot['repo'].casefold() != repo.casefold()):
                r.gaps.append('snapshot_repository_mismatch')
                continue
            if _integer_list(root.get('siblings')):
                r.gaps.append('split_siblings_require_separate_trace')
            if root.get('split_plan') or _integer_list(root.get('siblings')):
                r.gaps.append('split_partition_not_projected')
            jobs = r.rows('followup_jobs','chat_id=? AND root_key=?',(chat,root['root_key']),'id')
            sections['followup_jobs'] += jobs
            for job in jobs:
                for m in r.rows('messages','chat_id=? AND message_id=?',(chat,job['message_id'])):
                    sources[(chat,m['message_id'])]=m
                sections['followup_operations'] += r.rows('followup_operations','job_id=?',(job['id'],),'ordinal')
                members = r.rows('followup_job_sources','job_id=? AND chat_id=?',(job['id'],chat),'message_id')
                sections['followup_job_sources'] += members
                for member in members:
                    for m in r.rows('messages','chat_id=? AND message_id=?',(chat,member['message_id'])):
                        sources[(chat,m['message_id'])]=m
        root_keys.add(key)  # Initial case THINKING journals use original accounting key.
        root_keys.add(f'{chat}:{key}')
        sample = originals[0] if originals else next((m for m in seeds if m['chat_id']==chat),{})
        case_key = sample.get('case_key')
        if case_key:
            root_keys.add(case_key)
            sections['cases'] += r.rows('cases','chat_id=? AND case_key=?',(chat,case_key))
            sections['reconciliation'] += r.rows('reconciliation','case_key=?',(case_key,))
        if column=='group_key':
            sections['groups'] += r.rows('groups','chat_id=? AND group_key=?',(chat,key))
        sections['attachments'] += r.rows('attachments','case_key=?',(key,),'id')
        topics = r.rows('triage_topics',f'chat_id=? AND {column}=?',(chat,key),'topic_id')
        if column=='case_key':
            topics = [t for t in topics if not t.get('group_key')]
        sections['triage_topics'] += topics
        for topic in topics:
            sections['triage_topic_messages'] += r.rows('triage_topic_messages','topic_id=?',(topic['topic_id'],),'message_id')
            sections['triage_topic_events'] += r.rows('triage_topic_events','topic_id=?',(topic['topic_id'],),'version')
    # Autonomous plans contain prose; only source IDs and host-owned metadata are
    # exported by default. Episode relations are separate from split siblings.
    for chat,mid in list(sources):
        root_keys.add(f'{chat}:{mid}')
        routes=r.rows('autonomous_routes','chat_id=? AND message_id=?',(chat,mid))
        sections['autonomous_routes'] += routes
        for route in routes:
            turn_key=_json(route.get('metadata')).get('decision_turn_key')
            if isinstance(turn_key,str) and re.fullmatch(r'ingress:-?[0-9]+:[0-9]+',turn_key) and turn_key.startswith(f'ingress:{chat}:'):
                for decision in r.rows('thinking_decisions','turn_key=?',(turn_key,)):
                    root_keys.add(decision['root_key'])
        sections['autonomous_confirmations'] += r.rows('autonomous_confirmations','chat_id=? AND message_id=?',(chat,mid))
        episode_sources=r.rows('followup_episode_sources','chat_id=? AND message_id=?',(chat,mid))
        sections['followup_episode_sources'] += episode_sources
        for association in episode_sources:
            episodes=r.rows('followup_episodes','chat_id=? AND root_key=? AND repo=? COLLATE NOCASE',(chat,association['root_key'],repo))
            sections['followup_episodes'] += episodes
            for episode in episodes:
                root_keys.add(episode['root_key'])
                root_keys.add(episode['parent_root_key'])
        case=sources[(chat,mid)].get('case_key')
        if case:
            sections['autonomous_plans'] += r.rows('autonomous_plans','chat_id=? AND case_key=?',(chat,case),'anchor_id')
            sections['autonomous_clarifications'] += r.rows('autonomous_clarifications','chat_id=? AND case_key=?',(chat,case),'recorded_at')
    for root_key in list(root_keys):
        current_topics=r.rows('triage_current_topics','root_key=?',(root_key,),'ordinal')
        sections['triage_current_topics'] += current_topics
        for current in current_topics:
            sections['triage_current_topic_events'] += r.rows('triage_current_topic_events','topic_id=?',(current['topic_id'],),'version')
        sections['followup_episodes'] += r.rows('followup_episodes','(root_key=? OR parent_root_key=?) AND repo=? COLLATE NOCASE',(root_key,root_key,repo),'created_at')
    sections['messages'] = [sources[k] for k in sorted(sources)]
    for chat,mid in sources:
        sections['group_notices'] += r.rows('group_notices','chat_id=? AND message_id=?',(chat,mid))
    output={}
    for table,rows in sections.items():
        # Several partition scopes may point to the same parent case/notice.
        unique=list({json.dumps(row,sort_keys=True,default=str):row for row in rows}.values())
        output[table]=r.output(table,unique)
    output['thinking_decisions']=r.decisions('thinking_decisions',sorted(root_keys))
    output['thinking_decision_events']=r.decisions('thinking_decision_events',sorted(root_keys))
    if len(scopes)>1:
        r.gaps.append('multiple_partitions_kept_separate')
    partitions=[]
    for c,k,v in sorted(scopes):
        partition=dict(chat_id=c,kind=k,key=v,
                       provenance='stored_group' if k=='group_key' else 'case_provisional')
        if r.bounded('partitions',partition):
            partitions.append(partition)
    journal_mode=store.connection.execute('PRAGMA journal_mode').fetchone()[0]
    return dict(schema='triage.trace/v1',harness_owner='AlexChu',repo=repo,
                selector=dict(issue=issue,chat_id=chat_id,message_id=message_id),
                read_only=True,journal_mode=journal_mode,
                filesystem_note='WAL readers may touch shared-memory sidecars; use a consistent backup for immutable forensics' if journal_mode=='wal' else 'read-only SQLite connection; no application writes',
                include_text=include_text,limit=limit,
                privacy='private opt-in output; redaction is best effort' if include_text else 'metadata only',
                gaps=sorted(set(r.gaps)),truncated=sorted(r.truncated),
                partitions=partitions,sections=output)

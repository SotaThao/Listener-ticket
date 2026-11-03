"""Bounded, content-free inspection; recovery safety is never conversion authority."""
import json
import sqlite3
from .rules_pending_store import RulesHalt,check_connection_before_recovery,local_binding_blockers
from .offline_snapshot import isolated_snapshot

MAX_ROWS=1000
MAX_JSON_BYTES=256*1024
# Known categories only: unknown database names/values are never returned to the operator.
REGISTRY={}
def _register(family,names,required,status=None,terminal=(),pending=(),uncertain=(),json_fields=()):
    for name in names.split():REGISTRY[name]=dict(family=family,required=set(required.split()),status=status,
        terminal=tuple(terminal),pending=tuple(pending),uncertain=tuple(uncertain),json_fields=tuple(json_fields),enums={},text_fields=())

_register('core','messages','chat_id message_id decision github_called_at issue_number issue_url case_key group_key reason reply_to_id has_image has_document received_at',
    'decision',('chat','noise','issue','followup','needs_human','dry_run_issue','dry_run_needs_human','rate_limited'),
    ('queued','classifying','in_case','pending','pending_review'),('error',))
_register('core','cases groups','status github_called_at issue_number issue_url', 'status',
    ('done','chat','noise','issue','needs_human','pending_review','dry_run_issue','dry_run_needs_human'),('open','planned','analyzing'),('error',))
_register('core','reconciliation','case_key status balanced','status',('balanced','ok','done'),(),('error','unbalanced'))
_register('core','attachments','id chat_id message_id case_key status','status',('uploaded','prewrite_error'),(),('uploading','ambiguous','error'))
_register('core','group_notices','chat_id message_id kind status notice_message_id','status',('sent',),(),('reserved','failed','ambiguous'))
_register('accounting','model_usage','id outcome')
_register('followup','followup_roots','root_key chat_id source_message_id case_key issue_number siblings split_plan',json_fields=('siblings','split_plan'))
_register('followup','followup_jobs','id chat_id message_id root_key status plan result','status',('done','blocked'),('queued','planned','analyzing'),('reserved','ambiguous','error'),('plan',))
_register('followup','followup_operations','job_id ordinal kind status payload','status',('done',),('planned',),('reserved','ambiguous','error'),('payload',))
_register('followup','followup_analysis_retries','job_id edit_status original_plan original_result','edit_status',('done',),('planned',),('reserved','ambiguous','error'),('original_plan',))
_register('followup','followup_episodes','root_key parent_root_key job_id chat_id anchor_message_id issue_number repo source_ids',json_fields=('source_ids',))
_register('followup','followup_episode_sources','chat_id message_id root_key')
_register('followup','followup_job_sources','job_id chat_id message_id')
_register('autonomous','autonomous_routes','chat_id message_id action metadata','action',('NEW','CONTINUE','CLARIFY','CHAT','ANALYZE','STATUS'),(),(),('metadata',))
_register('autonomous','autonomous_confirmations','chat_id message_id issue_number status notice_message_id','status',('sent',),(),('reserved','ambiguous'))
_register('autonomous','autonomous_outages','chat_id cycle anchor_message_id status','status',('closed',),('open',),())
_register('autonomous','autonomous_clarifications','case_key question_key chat_id message_id')
_register('autonomous','autonomous_plans','chat_id anchor_id source_ids case_key plan',json_fields=('source_ids','plan'))
_register('lineage','triage_topic_hydration','singleton last_case_id')
_register('lineage','triage_topics','topic_id chat_id source_kind source_key source_message_id active version','active',(0,),(1,),())
_register('lineage','triage_topic_messages','topic_id chat_id message_id role provenance active','active',(0,),(1,),())
_register('lineage','triage_topic_events','topic_id version event metadata',json_fields=('metadata',))
_register('lineage','triage_current_topics','topic_id root_key chat_id source_ids human_bindings unresolved_ids scope_status active version','scope_status',('verified_scope',),('awaiting_issue','unresolved_partition'),(),('source_ids','human_bindings','unresolved_ids'))
_register('lineage','triage_current_topic_events','topic_id version metadata',json_fields=('metadata',))
_register('rules','rules_topics','topic_id source_chat_id status phase','status',('CLOSED','EXPIRED','ESCALATED'),('OPEN',),())
_register('rules','rules_sources','topic_id ref chat_id message_id')
_register('rules','rules_ops_replies','chat_id message_id topic_id ref')
_register('rules','rules_inflight_topics','topic_id batch_id ordinal')
_register('rules','rules_deferred_ops','chat_id message_id topic_id')
_register('rules','rules_notices','topic_id sequence ops_chat_id kind status notice_id','status',('sent',),(),('reserved','ambiguous','failed'))
_register('rules','rules_decisions','id topic_id state fingerprint present_sources','state',('READY','APPEND','APPEND_CLOSED','NEED_INFO','MEDIA_ONLY','CHAT','HOLD','ANALYZE','STATUS','LIFECYCLE_QUESTION','RELATED'),(),(),('present_sources',))
_register('rules','rules_batches','batch_id fingerprint')
_register('rules','rules_batch_groups','batch_id ordinal plan status','status',('DONE','BLOCKED'),('PLANNED',),('RESERVED','AMBIGUOUS'),('plan',))
_register('rules','rules_comments rules_append_completions','chat_id message_id status','status',('done',),(),('reserved','ambiguous','error'))
_register('rules','rules_readonly_notices','chat_id message_id kind plan_id body_hash body')
_register('rules','rules_lifecycle_questions','chat_id source_id topic_id parent_number repo plan_id batch_id ordinal')
_register('rules','rules_lifecycle_answers','chat_id source_id answer_id choice status','status',('done',),(),('reserved','ambiguous'))
_register('rules','rules_related_episodes','chat_id repo parent_number source_id answer_id status issue_number','status',('done',),(),('reserved','ambiguous'))
_register('rules','rules_related_receipts','chat_id repo parent_number plan_hash recurrence_keys source_keys',json_fields=('recurrence_keys','source_keys'))
_register('rules','rules_analysis_reviews','review_id chat_id source_id receipt_hash receipt',json_fields=('receipt',))
_register('rules','rules_analysis_review_members','chat_id message_id review_id')
_register('transition','rules_legacy_transitions','chat_id message_id source_sha256 attestation source_evidence_sha256 original_state',json_fields=('attestation','original_state'))
_register('transition','rules_legacy_releases','chat_id message_id attestation',json_fields=('attestation',))
_register('accounting','thinking_decisions thinking_decision_events','turn_key version record',json_fields=('record',))

REGISTRY['followup_jobs']['text_fields']=('result',)
REGISTRY['followup_analysis_retries']['text_fields']=('original_result',)
REGISTRY['rules_topics']['enums']={'phase':('NEW','HOLD','WAITING','LEGACY_REVIEW')}
REGISTRY['rules_lifecycle_answers']['enums']={'choice':('evidence','recurrence')}

ACTIONS={
 'RETAIN_LEGACY':'Retain original copy and legacy route; do not reset, replay or remove ledgers.',
 'RECONCILE_EXTERNAL':'Privately read source/Reply, operation reservation, configured repository issue/readback and sent notice identity; record confirmed outcome without replay.',
 'RECONCILE_SCHEMA':'Obtain a complete supported offline schema/history copy; absent reservation evidence cannot prove no effect.',
 'ATTEST_STAGE':'Review exact pristine target and inert context; supply offline digest and explicit stage attestation.',
 'ATTEST_RELEASE':'Retain original/stage receipt; use fresh post-stage digest and explicit release attestation; read back binding/archive.',
 'VERIFY_FUNCTION':'Release only arms HOLD; verify actual rules processing, intended effects and restart/duplicate behavior separately.',
}


def _quoted(name):return '"'+name.replace('"','""')+'"'

def _shape(connection,table):return {r[1] for r in connection.execute('PRAGMA table_info('+_quoted(table)+')')}

def _json(raw):
    if not isinstance(raw,str) or len(raw.encode('utf8'))>MAX_JSON_BYTES:raise ValueError()
    def unique(pairs):
        result={}
        for key,value in pairs:
            if key in result:raise ValueError()
            result[key]=value
        return result
    try:value=json.loads(raw,object_pairs_hook=unique)
    except RecursionError:raise ValueError('nested record') from None
    stack=[(value,0)];nodes=0
    while stack:
        item,depth=stack.pop();nodes+=1
        if depth>32 or nodes>10000:raise ValueError('record structure limit')
        if isinstance(item,dict):stack.extend((child,depth+1) for child in item.values())
        elif isinstance(item,list):stack.extend((child,depth+1) for child in item)
    return value


def _category(connection,name,spec,limit):
    result=dict(family=spec['family'],schema='SUPPORTED',total=None,terminal=None,pending=None,uncertain=None,invalid=None,
                validation_complete=True,truncated=False,codes=[])
    result['total']=connection.execute('SELECT count(*) FROM '+_quoted(name)).fetchone()[0]
    if not spec['required']<=_shape(connection,name):
        result.update(schema='UNKNOWN',validation_complete=False,codes=['MISSING_COLUMNS']);return result
    table=_quoted(name);total=connection.execute('SELECT count(*) FROM '+table).fetchone()[0];result['total']=total
    result.update(terminal=total,pending=0,uncertain=0,invalid=0,uncertain_posts=None)
    columns=_shape(connection,name)
    ids={'chat_id','source_chat_id','message_id','source_id','anchor_id','source_message_id','anchor_message_id','answer_id','parent_number','issue_number','ref','ordinal'} & columns
    id_conditions=[]
    for field in ids:
        nullable=field=='issue_number' or name=='attachments' and field=='chat_id'
        condition=f'typeof({_quoted(field)})!=\"integer\" OR {_quoted(field)}'+('=0' if field.endswith('chat_id') or field=='chat_id' else '<0' if field=='ordinal' else '<=0')
        if nullable:condition=f'{_quoted(field)} IS NOT NULL AND ('+condition+')'
        id_conditions.append('('+condition+')')
    id_where=' OR '.join(id_conditions) or '0'
    invalid_ids=connection.execute('SELECT count(*) FROM '+table+' WHERE '+id_where).fetchone()[0]
    basic_where=id_where;basic_params=()
    if {'github_called_at','issue_number','issue_url'}<=columns:
        result['uncertain_posts']=connection.execute('SELECT count(*) FROM '+table+" WHERE github_called_at IS NOT NULL AND (typeof(issue_number)!='integer' OR issue_number<=0 OR typeof(issue_url)!='text' OR issue_url='')").fetchone()[0]
        if result['uncertain_posts']:result['codes'].append('UNCONFIRMED_POST')
    if invalid_ids:result['codes'].append('INVALID_IDENTIFIER')
    if spec['status']:
        column=_quoted(spec['status']);values=spec['terminal']+spec['pending']+spec['uncertain']
        placeholders=','.join('?' for _ in values)
        known=connection.execute(f'SELECT count(*) FROM {table} WHERE {column} IN ({placeholders})',values).fetchone()[0]
        result['invalid']=total-known
        for key in ('terminal','pending','uncertain'):
            allowed=spec[key]
            result[key]=connection.execute(f'SELECT count(*) FROM {table} WHERE {column} IN ('+','.join('?' for _ in allowed)+')',allowed).fetchone()[0] if allowed else 0
        if result['invalid']:result['codes'].append('UNKNOWN_STATUS')
        basic_where='('+id_where+') OR '+column+' IS NULL OR '+column+' NOT IN ('+placeholders+')'
        basic_params=values
    result['invalid']=connection.execute('SELECT count(*) FROM '+table+' WHERE '+basic_where,basic_params).fetchone()[0]
    for column in spec['text_fields']:
        name=_quoted(column)
        clause=f'{name} IS NOT NULL AND (typeof({name})!=\"text\" OR length(CAST({name} AS BLOB))>{MAX_JSON_BYTES})'
        if connection.execute('SELECT 1 FROM '+table+' WHERE '+clause+' LIMIT 1').fetchone():result['codes'].append('MALFORMED_TEXT_RECORD')
        basic_where='('+basic_where+') OR ('+clause+')'
    for column,allowed in spec['enums'].items():
        clause=_quoted(column)+' IS NULL OR '+_quoted(column)+' NOT IN ('+','.join('?' for _ in allowed)+')'
        if connection.execute('SELECT 1 FROM '+table+' WHERE '+clause+' LIMIT 1',allowed).fetchone():
            result['codes'].append('UNKNOWN_'+column.upper())
        basic_where='('+basic_where+') OR '+clause
        basic_params=(*basic_params,*allowed)
    result['invalid']=connection.execute('SELECT count(*) FROM '+table+' WHERE '+basic_where,basic_params).fetchone()[0]
    if spec['json_fields']:
        fields=','.join(_quoted(field) for field in spec['json_fields'])
        # SQL length guards avoid bringing unbounded/private payloads into Python.
        expressions=','.join(f'CASE WHEN length(CAST({_quoted(f)} AS BLOB))<=? THEN {_quoted(f)} ELSE NULL END,{_quoted(f)} IS NOT NULL' for f in spec['json_fields'])
        cursor=connection.execute('SELECT '+expressions+',CASE WHEN '+basic_where+' THEN 1 ELSE 0 END FROM '+table+' LIMIT ?',(*([MAX_JSON_BYTES]*len(spec['json_fields'])),*basic_params,limit+1))
        nullable={'split_plan','plan','result','original_result'}
        malformed=0;used=0
        for index,row in enumerate(cursor):
            if index>=limit:result['truncated']=True;break
            for field,value,exists in zip(spec['json_fields'],row[:-1:2],row[1:-1:2]):
                try:
                    if value is None and not exists and field in nullable:continue
                    used+=len(value.encode('utf8')) if isinstance(value,str) else 0
                    if used>8*1024*1024:result['truncated']=True;break
                    parsed=_json(value)
                    if not isinstance(parsed,(dict,list)):raise ValueError()
                except (ValueError,TypeError,UnicodeError):
                    malformed+=1
                    if not row[-1]:result['invalid']+=1
                    break
            if result['truncated']:break
        if malformed:result['codes'].append('MALFORMED_RECORD')
        if result['truncated']:result['codes'].append('VALIDATION_TRUNCATED')
    result['validation_complete']=not result['truncated'] and not result['invalid']
    return result


def _attachment_evidence(connection,tables,limit):
    result=dict(items=[],truncated=False,migration_performed=False,schema='UNKNOWN',validation_complete=False)
    if 'attachments' not in tables:return result
    if not {'id','chat_id','message_id','status'}<=_shape(connection,'attachments'):return result
    rows=connection.execute("SELECT id,chat_id,message_id,CASE WHEN status IN ('uploaded','prewrite_error','uploading','ambiguous','error') THEN status ELSE 'UNKNOWN_STATUS' END FROM attachments WHERE status IS NULL OR status!='uploaded' ORDER BY id LIMIT ?",(limit+1,)).fetchall()
    for ident,chat,mid,status in rows[:limit]:
        valid=type(ident) is int and ident>0 and (chat is None or type(chat) is int) and type(mid) is int and mid>0
        item=dict(status=status,evidence='proven_prewrite' if status=='prewrite_error' and valid and chat is not None else 'outcome_unresolved')
        if valid:item.update(attachment_id=ident,chat_id=chat,message_id=mid)
        else:item['code']='INVALID_IDENTIFIER'
        result['items'].append(item)
    result.update(schema='SUPPORTED',truncated=len(rows)>limit,
        validation_complete=len(rows)<=limit and all(row['status']!='UNKNOWN_STATUS' and 'code' not in row for row in result['items']))
    return result


def _conversion(connection,tables,inventory,blockers):
    result=dict(stage='UNKNOWN',release='NOT_STAGED',functional='UNVERIFIED',codes=[],actions=['RETAIN_LEGACY'])
    if 'rules_legacy_transitions' in tables and inventory['rules_legacy_transitions']['total'] and 'original_state' not in _shape(connection,'rules_legacy_transitions'):
        result.update(stage='ALREADY_STAGED',release='HOLD_ARCHIVE_MISSING',codes=['ORIGINAL_ARCHIVE_UNAVAILABLE'],actions=['RETAIN_LEGACY','RECONCILE_EXTERNAL']);return result
    if 'messages' not in tables or inventory['messages']['schema']!='SUPPORTED' or blockers:
        result['codes']=['INCOMPLETE_OR_UNRESOLVED_EVIDENCE'];return result
    if any(row['schema']=='UNKNOWN' or not row['validation_complete'] for row in inventory.values() if row['schema']!='ABSENT_UNKNOWN'):
        result['codes']=['INCOMPLETE_SCHEMA_OR_VALIDATION'];return result
    if 'rules_legacy_transitions' in tables and inventory['rules_legacy_transitions']['total']:
        if inventory['rules_legacy_transitions']['schema']!='SUPPORTED':result['release']='HOLD_ARCHIVE_MISSING';return result
        if 'rules_topics' not in tables:result['release']='HOLD_BINDING_UNKNOWN';return result
        rows=connection.execute('SELECT phase FROM rules_topics LIMIT 2').fetchall()
        if rows==[('HOLD',)]:result.update(stage='ALREADY_STAGED',release='ARMED_NOT_FUNCTIONALLY_VERIFIED',actions=['VERIFY_FUNCTION']);return result
        media=connection.execute('SELECT 1 FROM messages WHERE has_image!=0 OR has_document!=0 LIMIT 1').fetchone()
        result.update(stage='ALREADY_STAGED',release='HOLD_IMMUTABLE_MEDIA_MISSING' if media else 'REQUIRES_EXTERNAL_RECEIPT_ATTESTATION',
                      actions=['RECONCILE_EXTERNAL'] if media else ['ATTEST_RELEASE','VERIFY_FUNCTION']);return result
    allowed={'messages','attachments','triage_topic_hydration'}
    if any(name not in allowed and row['total'] for name,row in inventory.items()):
        result.update(stage='HOLD_HISTORY_REQUIRES_RECONCILIATION',codes=['NONEMPTY_HISTORY'],actions=['RETAIN_LEGACY','RECONCILE_EXTERNAL']);return result
    unknown=tables-set(REGISTRY)-{'sqlite_sequence'}
    if any(connection.execute('SELECT 1 FROM '+_quoted(name)+' LIMIT 1').fetchone() for name in unknown):
        result['codes']=['UNSUPPORTED_HISTORY'];return result
    pending=connection.execute("SELECT chat_id,message_id,decision,github_called_at,case_key,group_key,reply_to_id FROM messages WHERE decision IN ('queued','classifying','in_case','pending','pending_review','error') LIMIT 2").fetchall()
    if len(pending)!=1 or pending[0][2] not in ('queued','pending','pending_review') or any(value is not None for value in pending[0][3:]):
        result.update(stage='HOLD_TARGET_NOT_PRISTINE',codes=['TARGET_NOT_PRISTINE']);return result
    from .legacy_reconciliation import _check_inert_messages,_check_empty_hydration
    try:
        _check_inert_messages(connection,pending[0][:2]);_check_empty_hydration(connection,tables)
        if connection.execute('SELECT 1 FROM messages WHERE github_called_at IS NOT NULL OR issue_number IS NOT NULL OR issue_url IS NOT NULL LIMIT 1').fetchone():raise RulesHalt('history')
    except RulesHalt:result.update(stage='HOLD_HISTORY_REQUIRES_RECONCILIATION',codes=['NON_PRISTINE_HISTORY']);return result
    from .legacy_reconciliation import _check_prewrite_attachments
    flags=connection.execute('SELECT has_image,has_document FROM messages WHERE chat_id=? AND message_id=?',pending[0][:2]).fetchone()
    ids=[row[0] for row in connection.execute('SELECT id FROM attachments ORDER BY id LIMIT 2')] if 'attachments' in tables else []
    try:_check_prewrite_attachments(connection,tables,dict(chat_id=pending[0][0],message_id=pending[0][1],prewrite_attachment_ids=ids),*flags)
    except RulesHalt:
        result.update(stage='HOLD_ATTACHMENT_REQUIRES_RECONCILIATION',codes=['ATTACHMENT_NOT_PRISTINE'],actions=['RETAIN_LEGACY','RECONCILE_EXTERNAL']);return result
    result.update(stage='CANDIDATE_REQUIRES_ATTESTATION',actions=['ATTEST_STAGE','ATTEST_RELEASE','VERIFY_FUNCTION'],codes=['ATTESTATION_AND_TRANSACTIONAL_RECHECK_REQUIRED'])
    return result


def inspect(path,*,include_attachment_evidence=False,limit=MAX_ROWS):
    """One isolated snapshot for inventory, bounded JSON validation and pure recovery gate."""
    if type(limit) is not int or not 1<=limit<=MAX_ROWS:raise ValueError('invalid inspection limit')
    with isolated_snapshot(path) as (connection,digest):
        tables={r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'messages' not in tables:raise RulesHalt('MESSAGE_SCHEMA_UNKNOWN')
        inventory={}
        for name,spec in REGISTRY.items():
            inventory[name]=(_category(connection,name,spec,limit) if name in tables else dict(family=spec['family'],schema='ABSENT_UNKNOWN',total=None,terminal=None,pending=None,uncertain=None,invalid=None,validation_complete=False,truncated=False,codes=['LEDGER_ABSENT']))
        unknown={name for name in tables-set(REGISTRY) if not name.startswith('sqlite_')}
        internal={name for name in tables if name.startswith('sqlite_') and name!='sqlite_sequence'}
        internal_nonempty=sum(bool(connection.execute('SELECT 1 FROM '+_quoted(name)+' LIMIT 1').fetchone()) for name in sorted(internal)[:limit])
        unsupported=0
        for name in sorted(unknown)[:limit]:unsupported+=bool(connection.execute('SELECT 1 FROM '+_quoted(name)+' LIMIT 1').fetchone())
        blockers=local_binding_blockers(connection,tables,limit)
        incomplete=any(row['schema']=='UNKNOWN' or (row['schema']=='SUPPORTED' and not row['validation_complete']) for row in inventory.values())
        if len(unknown)>limit:blockers.append('UNKNOWN_INVENTORY_TRUNCATED');incomplete=True
        if unsupported:blockers.append('UNSUPPORTED_HISTORY')
        if internal_nonempty:blockers.append('SQLITE_INTERNAL_HISTORY')
        if len(internal)>limit:blockers.append('INTERNAL_INVENTORY_TRUNCATED');incomplete=True
        try:
            if incomplete:raise RulesHalt('SCHEMA_OR_VALIDATION_UNKNOWN')
            check_connection_before_recovery(connection)
        except RulesHalt:
            gate='UNKNOWN' if incomplete else 'HOLD';recovery_codes=['SCHEMA_OR_VALIDATION_UNKNOWN'] if incomplete else ['RECOVERY_REQUIRES_RECONCILIATION']
        else:gate='HOLD' if blockers else 'CLEAR';recovery_codes=blockers or ['RECOVERY_CHECKS_CLEAR_ONLY']
        counts={key:(inventory[table]['pending'] or 0)+(inventory[table]['uncertain'] or 0)+(inventory[table]['invalid'] or 0) if inventory[table]['schema']=='SUPPORTED' and inventory[table]['validation_complete'] else None for key,table in
            (('legacy_messages','messages'),('open_cases','cases'),('followup_jobs','followup_jobs'),('followup_operations','followup_operations'),('followup_edits','followup_analysis_retries'),('confirmations','autonomous_confirmations'),('notices','group_notices'),('attachments','attachments'),('rules_comments','rules_comments'),('rules_notices','rules_notices'),('rules_checkpoints','rules_batch_groups'))}
        offers=0
        if 'autonomous_plans' in tables and inventory['autonomous_plans']['schema']=='SUPPORTED':
            for raw, in connection.execute('SELECT CASE WHEN length(CAST(plan AS BLOB))<=? THEN plan ELSE NULL END FROM autonomous_plans LIMIT ?',(MAX_JSON_BYTES,limit)):
                try:
                    plan=_json(raw)
                    if not isinstance(plan,dict):raise ValueError()
                    offers+=plan.get('action')=='ANALYZE' and 'task' not in plan and bool(plan.get('options'))
                except (TypeError,ValueError):offers+=1
        for key,table in (('uncertain_issue_posts','messages'),('uncertain_cases_posts','cases'),('uncertain_groups_posts','groups')):
            counts[key]=inventory[table].get('uncertain_posts')
        counts['legacy_offers_or_unreadable_plans']=offers if 'autonomous_plans' in tables else None
        report=dict(preflight_only=True,migration_performed=False,inspection=dict(status='INCOMPLETE' if incomplete else 'COMPLETE',
            validation_complete=not incomplete and not any(row['schema']=='ABSENT_UNKNOWN' for row in inventory.values()),
            present_ledgers_validated=not incomplete,schema_coverage_complete=not any(row['schema']=='ABSENT_UNKNOWN' for row in inventory.values()),
            absent_ledgers_unknown=True,snapshot_sha256=digest,one_isolated_snapshot=True),
            recovery_gate=gate,recovery_codes=sorted(set(recovery_codes)),counts=counts,inventory=inventory,
            unsupported_history=dict(nonempty_tables=unsupported,truncated=len(unknown)>limit),
            sqlite_internal_history=dict(nonempty_tables=internal_nonempty,truncated=len(internal)>limit,converter_exempt=False),binding_codes=blockers,
            conversion=_conversion(connection,tables,inventory,blockers),operator_actions=ACTIONS)
        if include_attachment_evidence:report['attachment_evidence']=_attachment_evidence(connection,tables,min(limit,100))
        return report


def attachment_evidence(path,limit=100):
    if type(limit) is not int or not 1<=limit<=1000:raise ValueError('invalid evidence limit')
    with isolated_snapshot(path) as (connection,_):
        tables={r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        return _attachment_evidence(connection,tables,limit)

"""Durable rules-intake attempts and cross-chat question bindings.

This helper never sends or retries external effects. Callers reserve first and
must treat an unfinished reservation as consumed, not as permission to resend.
"""
import json
import hashlib
import sqlite3
from datetime import timedelta


class RulesHalt(RuntimeError):
    """An unresolved side effect requires Owner inspection before restart."""


def readonly_plan_id(batch_id, ordinal, fingerprint, plan):
    payload=json.dumps([batch_id,ordinal,fingerprint,plan],sort_keys=True,ensure_ascii=False,separators=(',',':'))
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()


def _unmarked_open_comment(connection,tables):
    if 'rules_comments' not in tables:
        return False
    if not {'rules_sources','rules_topics'}<=tables:
        return bool(connection.execute('SELECT 1 FROM rules_comments LIMIT 1').fetchone())
    marker=("AND NOT EXISTS (SELECT 1 FROM rules_append_completions a WHERE a.chat_id=c.chat_id AND a.message_id=c.message_id AND a.status='done')"
            if 'rules_append_completions' in tables else '')
    return bool(connection.execute("""SELECT 1 FROM rules_comments c
        JOIN rules_sources s ON s.chat_id=c.chat_id AND s.message_id=c.message_id
        JOIN rules_topics t ON t.topic_id=s.topic_id
        WHERE c.status='done' AND t.status='OPEN' """+marker+" LIMIT 1").fetchone())


def _recoverable_batch_groups(connection):
    """Classify only checkpoints whose completed work can be proved from ledgers."""
    rows=connection.execute("SELECT batch_id,ordinal,plan,status FROM rules_batch_groups WHERE status IN ('RESERVED','AMBIGUOUS')").fetchall()
    recover=[]
    for batch_id,ordinal,raw,status in rows:
        if status == 'AMBIGUOUS': raise RulesHalt('unresolved ambiguous operation; Owner inspection required')
        try:
            plan=json.loads(raw); state=plan['state']; keys=plan['source_keys']
            if not keys or not all(isinstance(k,list) and len(k)==2 and all(type(x) is int for x in k) for k in keys): raise ValueError()
            topic=plan.get('pending_topic') or f'{keys[0][0]}:{min(k[1] for k in keys)}'
            if state == 'CHAT':
                current=connection.execute('SELECT status FROM rules_topics WHERE topic_id=?',(topic,)).fetchone()
                if current and current[0]=='CLOSED':
                    complete=all(connection.execute("""SELECT 1 FROM messages m JOIN rules_sources s
                        ON s.chat_id=m.chat_id AND s.message_id=m.message_id
                        WHERE m.chat_id=? AND m.message_id=? AND s.topic_id=? AND m.decision='chat' AND m.reason=?""",(*key,topic,plan['rule'])).fetchone() for key in keys)
                    if not complete: raise ValueError()
                    recover.append((batch_id,ordinal,'DONE')); continue
                if current and current[0]!='OPEN': raise ValueError()
                recover.append((batch_id,ordinal,'PLANNED')); continue
            if state == 'MEDIA_ONLY':
                recover.append((batch_id,ordinal,'PLANNED')); continue
            if state == 'RELATED':
                if not plan.get('targets') or len({p['number'] for p in plan['targets']})!=1: raise ValueError()
                if any(key[0]!=keys[0][0] for key in keys): raise ValueError()
                import sqlite3
                from .store import _to_message
                from .rules_intake import _source_evidence_id
                old_factory=connection.row_factory
                try:
                    connection.row_factory=sqlite3.Row
                    rows=[connection.execute('SELECT * FROM messages WHERE chat_id=? AND message_id=?',key).fetchone() for key in keys]
                    context=[connection.execute('SELECT * FROM messages WHERE chat_id=? AND message_id=?',(keys[0][0],int(mid))).fetchone()
                        for mid in plan.get('lifecycle_context',{})]
                finally:
                    connection.row_factory=old_factory
                if any(row is None for row in [*rows,*context]): raise ValueError()
                members=[_to_message(row) for row in rows]
                lifecycle=plan.get('lifecycle') or {}
                from .lifecycle import partition_source_roles
                history,_,unknown=partition_source_roles(lifecycle,{m.message_id:m.reply_to_id for m in members})
                if unknown: raise ValueError()
                recurrence=sorted([m for m in members if m.message_id not in history],key=lambda m:(m.received_at,m.message_id))
                if not recurrence: raise ValueError()
                anchor=recurrence[0]; authority=[keys[0][0],plan['targets'][0]['reply_message_id']]
                notice_key=[anchor.chat_id,anchor.message_id]
                episode=connection.execute('SELECT status,issue_number,issue_url,notice_body,repo,parent_number FROM rules_related_episodes WHERE chat_id=? AND source_id=? AND answer_id=?',
                    (*notice_key,anchor.message_id)).fetchone()
                answer=connection.execute('SELECT status,choice,answer_id FROM rules_lifecycle_answers WHERE chat_id=? AND source_id=?',authority).fetchone()
                notice=connection.execute("SELECT kind,status,notice_id,ops_chat_id FROM rules_notices WHERE topic_id=? AND kind='other'",(topic,)).fetchone()
                question_notice=connection.execute('SELECT 1 FROM rules_lifecycle_questions WHERE chat_id=? AND source_id=?',authority).fetchone()
                if episode is None and answer is None and notice is None and question_notice is None:
                    recover.append((batch_id,ordinal,'PLANNED')); continue
                if episode is not None:
                    expected_hash=hashlib.sha256(json.dumps(plan,sort_keys=True,ensure_ascii=False,separators=(',',':')).encode('utf8')).hexdigest()
                    receipt=connection.execute('SELECT plan_hash,recurrence_keys,source_keys FROM rules_related_receipts WHERE chat_id=? AND repo=? COLLATE NOCASE AND parent_number=?',
                        (anchor.chat_id,episode[4],episode[5])).fetchone()
                    if (not receipt or receipt[0]!=expected_hash
                            or json.loads(receipt[1])!=[[m.chat_id,m.message_id] for m in recurrence]
                            or json.loads(receipt[2])!=keys
                            or plan.get('lifecycle_sources')!={str(m.message_id):_source_evidence_id(m) for m in members}
                            or plan.get('lifecycle_context')!={str(row['message_id']):_source_evidence_id(_to_message(row)) for row in context}): raise ValueError()
                    for row in rows:
                        historical=row['message_id'] in history
                        target_number=episode[5] if historical else episode[1]
                        target_url=f'https://github.com/{episode[4]}/issues/{target_number}'
                        expected_decision='issue' if row['message_id']==anchor.message_id else 'followup'
                        if (row['decision']!=expected_decision or row['issue_number']!=target_number or row['issue_url']!=target_url
                                or row['reason']!='lifecycle_related_episode' or not historical and not row['github_called_at']): raise ValueError()
                    current=connection.execute('SELECT status FROM rules_topics WHERE topic_id=?',(topic,)).fetchone()
                    from .rules_intake import _ops_notice_identity
                    if (episode[0]=='done' and episode[5]==plan['targets'][0]['number']
                            and episode[2]==f'https://github.com/{episode[4]}/issues/{episode[1]}'
                            and answer and tuple(answer)==('done','recurrence',anchor.message_id)
                            and current and current[0]=='CLOSED' and notice and notice[0]=='other'
                            and notice[1]=='sent' and _ops_notice_identity(connection,notice[3],notice[2])):
                        recover.append((batch_id,ordinal,'DONE')); continue
                    raise ValueError()
                if answer is not None: raise ValueError()
            if state in ('LIFECYCLE_QUESTION','RELATED'):
                if not plan.get('targets') or len({p['number'] for p in plan['targets']})!=1: raise ValueError()
                target=plan['targets'][0]
                question_key=[target['chat_id'],target['reply_message_id']]
                if question_key not in keys: raise ValueError()
                batch=connection.execute('SELECT fingerprint FROM rules_batches WHERE batch_id=?',(batch_id,)).fetchone()
                q=connection.execute('SELECT parent_number,repo,plan_id,question,fingerprint,evidence_answer,recurrence_answer,batch_id,ordinal FROM rules_lifecycle_questions WHERE chat_id=? AND source_id=?',question_key).fetchone()
                notices=connection.execute("SELECT kind,status,notice_id,ops_chat_id FROM rules_notices WHERE topic_id=? AND kind='question'",(topic,)).fetchall()
                notice=notices[0] if len(notices)==1 else None
                if q is None and notice is None:
                    recover.append((batch_id,ordinal,'PLANNED')); continue
                current=connection.execute('SELECT status FROM rules_topics WHERE topic_id=?',(topic,)).fetchone()
                source=connection.execute("SELECT 1 FROM messages WHERE chat_id=? AND message_id=? AND decision='pending_review' AND reason='lifecycle_question_sent'",question_key).fetchone()
                import sqlite3
                old_factory=connection.row_factory
                try:
                    connection.row_factory=sqlite3.Row
                    evidence_row=connection.execute('SELECT * FROM messages WHERE chat_id=? AND message_id=?',question_key).fetchone()
                finally:
                    connection.row_factory=old_factory
                from .store import _to_message
                from .rules_intake import _source_evidence_id
                evidence_ok=bool(evidence_row and (plan.get('lifecycle_sources') or {}).get(str(question_key[1]),plan.get('lifecycle_source_evidence'))==_source_evidence_id(_to_message(evidence_row)))
                parent=target['reply_to_id']; root_id=target.get('target_source_id',parent)
                if parent>=target['reply_message_id']: raise ValueError()
                if root_id!=parent and not (root_id<parent<target['reply_message_id']): raise ValueError()
                if root_id!=parent and not connection.execute('''SELECT 1 FROM group_notices n JOIN rules_related_episodes e
                    ON e.chat_id=n.chat_id AND e.answer_id=n.message_id WHERE n.chat_id=? AND n.notice_message_id=?
                    AND n.message_id=? AND n.kind='LIFECYCLE_EPISODE' AND n.status='sent' AND e.status='done' ''',(target['chat_id'],parent,root_id)).fetchone(): raise ValueError()
                if root_id!=parent and connection.execute("SELECT count(*) FROM group_notices WHERE chat_id=? AND notice_message_id=? AND status='sent'",(target['chat_id'],parent)).fetchone()[0]!=1: raise ValueError()
                if root_id!=parent and connection.execute('SELECT 1 FROM messages WHERE chat_id=? AND message_id=?',(target['chat_id'],parent)).fetchone(): raise ValueError()
                root=connection.execute("SELECT issue_number,issue_url FROM messages WHERE chat_id=? AND message_id=? AND decision IN ('issue','followup')",(target['chat_id'],root_id)).fetchone()
                root_ok=bool(root and q and root[0]==target.get('recurrence_origin_number',q[0])
                    and root[1]==f'https://github.com/{q[1]}/issues/{root[0]}')
                if root_ok and target.get('recurrence_origin_number'):
                    from .rules_intake import _stored_recurrence_lineage
                    chain=_stored_recurrence_lineage(connection,target['chat_id'],q[1],root[0])
                    root_ok=any(edge=={'number':q[0],'source_id':target.get('recurrence_parent_source_id')} for edge in chain)
                from .rules_intake import _ops_notice_identity
                if (q and notice and batch and current and current[0]=='OPEN' and source and evidence_ok and
                        q[0]==target['number'] and q[3]==plan.get('lifecycle_question') and q[4]==batch[0] and
                        q[5]==plan.get('lifecycle_evidence_answer') and q[6]==plan.get('lifecycle_recurrence_answer') and
                        q[7]==batch_id and q[8]==ordinal and
                        root_ok and
                        q[2]==readonly_plan_id(batch_id,ordinal,batch[0],plan) and
                        notice[0]=='question' and notice[1]=='sent' and _ops_notice_identity(connection,notice[3],notice[2])):
                    recover.append((batch_id,ordinal,'DONE')); continue
                raise ValueError()
            if state in ('ANALYZE','STATUS'):
                if len(keys)!=1 and (state=='STATUS' or not plan.get('analysis_review')): raise ValueError()
                reason='semantic_analyze' if state=='ANALYZE' else 'semantic_status'
                finished=all(connection.execute("SELECT 1 FROM messages WHERE chat_id=? AND message_id=? AND decision='chat' AND reason=?",(*key,reason)).fetchone() for key in keys)
                notices=connection.execute("SELECT status,notice_id,kind,ops_chat_id FROM rules_notices WHERE topic_id=? AND kind IN ('other','question')",(topic,)).fetchall()
                notice=notices[0] if len(notices)==1 else None
                proof=connection.execute("SELECT kind,plan_id,body_hash,body FROM rules_readonly_notices WHERE chat_id=? AND message_id=?",keys[0]).fetchone()
                batch=connection.execute('SELECT fingerprint FROM rules_batches WHERE batch_id=?',(batch_id,)).fetchone()
                expected=readonly_plan_id(batch_id,ordinal,batch[0],plan) if batch else None
                content_ok=bool(proof and isinstance(proof[3],str) and hashlib.sha256(proof[3].encode('utf-8')).hexdigest()==proof[2])
                if content_ok and state=='ANALYZE':
                    from .analysis_task import render, render_excerpts, explicit_task
                    source=connection.execute('SELECT text,is_forward FROM messages WHERE chat_id=? AND message_id=?',keys[0]).fetchone()
                    content_ok=bool(source and proof[3]==(render(plan['task']) if plan.get('analysis_review') or explicit_task(plan['task'],source[0],source[1]) else render_excerpts(plan['task'])))
                from .rules_analysis import has_binding
                if state=='ANALYZE' and not plan.get('analysis_review') and has_binding(connection,keys): raise ValueError()
                if plan.get('analysis_review'):
                    from .rules_analysis import load
                    receipt=load(connection,plan,batch[0])
                    from .rules_analysis import source_bindings_valid,member_states_valid
                    content_ok=content_ok and receipt is not None and source_bindings_valid(connection,receipt) and member_states_valid(connection,plan,topic)
                    finished=content_ok
                from .rules_intake import _ops_notice_identity
                if finished and notice and proof and proof[0]==state and proof[1]==expected and content_ok and notice[0]=='sent' and _ops_notice_identity(connection,notice[3],notice[1]):
                    current=connection.execute('SELECT status FROM rules_topics WHERE topic_id=?',(topic,)).fetchone()
                    if not current or current[0] not in ('OPEN','CLOSED'): raise ValueError()
                    recover.append((batch_id,ordinal,'DONE')); continue
                # No reservation proves no notice was sent by this path.
                if not notices and proof is None:
                    recover.append((batch_id,ordinal,'PLANNED')); continue
                raise ValueError()
            if state == 'READY':
                root=connection.execute('SELECT chat_id,message_id FROM rules_sources WHERE topic_id=? AND ref=1',(topic,)).fetchone()
                if not root: raise ValueError()
                issue=connection.execute("SELECT 1 FROM messages WHERE chat_id=? AND message_id=? AND decision='issue' AND github_called_at IS NOT NULL AND issue_number>0 AND issue_url IS NOT NULL",root).fetchone()
                notice=connection.execute("SELECT 1 FROM rules_notices WHERE topic_id=? AND kind='other' AND status='sent' AND notice_id>0",(topic,)).fetchone()
                if issue and notice:
                    recover.append((batch_id,ordinal,'DONE')); continue
            if state in ('APPEND','APPEND_CLOSED'):
                targets=plan['targets']
                if not isinstance(targets,list) or not targets: raise ValueError()
                target=targets[0]
                number=target['number']
                if type(number) is not int or number<=0: raise ValueError()
                comment=connection.execute("SELECT repo FROM rules_comments WHERE chat_id=? AND message_id=? AND issue_number=? AND status='done' AND result>0",(target['chat_id'],target['reply_message_id'],number)).fetchone()
                if not comment: raise ValueError()
                repo=comment[0]
                for proof in targets:
                    chat=proof['chat_id']; reply=proof['reply_message_id']; parent=proof['reply_to_id']
                    if parent>=reply: raise ValueError()
                    expected_state='closed' if state=='APPEND_CLOSED' else 'open'
                    if proof['number']!=number or proof['state']!=expected_state or [chat,reply] not in keys: raise ValueError()
                    source=connection.execute('SELECT reply_to_id FROM messages WHERE chat_id=? AND message_id=?',(chat,reply)).fetchone()
                    root_id=proof.get('target_source_id',parent)
                    if root_id!=parent and not (root_id<parent<reply): raise ValueError()
                    if root_id!=parent and not connection.execute('''SELECT 1 FROM group_notices n JOIN rules_related_episodes e
                        ON e.chat_id=n.chat_id AND e.answer_id=n.message_id WHERE n.chat_id=? AND n.notice_message_id=?
                        AND n.message_id=? AND n.kind='LIFECYCLE_EPISODE' AND n.status='sent' AND e.status='done' ''',(chat,parent,root_id)).fetchone(): raise ValueError()
                    if root_id!=parent and connection.execute("SELECT count(*) FROM group_notices WHERE chat_id=? AND notice_message_id=? AND status='sent'",(chat,parent)).fetchone()[0]!=1: raise ValueError()
                    if root_id!=parent and connection.execute('SELECT 1 FROM messages WHERE chat_id=? AND message_id=?',(chat,parent)).fetchone(): raise ValueError()
                    root=connection.execute("SELECT issue_number,issue_url FROM messages WHERE chat_id=? AND message_id=? AND decision IN ('issue','followup')",(chat,root_id)).fetchone()
                    if not source or source[0]!=parent or not root or root[0]!=number or root[1]!=f'https://github.com/{repo}/issues/{number}': raise ValueError()
                notice=connection.execute("SELECT 1 FROM rules_notices WHERE topic_id=? AND kind='other' AND status='sent' AND notice_id>0",(topic,)).fetchone()
                if comment and notice:
                    recover.append((batch_id,ordinal,'DONE')); continue
        except (ValueError,KeyError,TypeError,json.JSONDecodeError):
            pass
        raise RulesHalt('batch checkpoint requires Owner inspection')
    return recover


def _source_followup_blocker(connection, tables, *, before_recovery=False):
    """Read only: reject unusable committed links, not clean PLANNED recovery.

    A connection has no running RulesSnapshot; fingerprint comparison remains
    in the worker/sweep. No other batch family is interpreted here.
    """
    if 'rules_batch_groups' not in tables: return None
    rows=connection.execute("""SELECT batch_id,ordinal,CASE WHEN length(CAST(plan AS BLOB))<=262144 THEN plan ELSE NULL END
        FROM rules_batch_groups WHERE batch_id LIKE 'source-followup:%' AND status='PLANNED' LIMIT 1001""").fetchall()
    if not rows: return None
    if len(rows)>1000: return 'source-followup validation incomplete; Owner inspection required'
    used=0
    for batch,ordinal,raw in rows:
        def blocked(reason): return f'source-followup checkpoint {batch}: {reason}; Owner inspection required'
        try:
            if not {'rules_batches','rules_topics','rules_sources','rules_inflight_topics','messages'}<=tables:
                return blocked('required ledger missing')
            if not isinstance(raw,str): return blocked('plan cannot be inspected within validation budget')
            used+=len(raw.encode('utf8'))
            if used>8*1024*1024: return blocked('validation incomplete')
            plan=json.loads(raw)
            required={'topic','state','intent','intent_rule','rule','present','questions','asked_fields','sources'}
            if not isinstance(plan,dict) or not required<=plan.keys(): return blocked('invalid replay plan')
            topic=plan['topic']; sources=plan['sources']
            if (ordinal!=0 or not isinstance(topic,str) or not topic or plan['state'] not in ('READY','NEED_INFO','CHAT','MEDIA_ONLY')
                    or not isinstance(sources,list) or not sources
                    or any(not isinstance(s,list) or len(s)!=3 or any(type(v) is not int for v in s) or s[0]<=0 or s[2]<=0 for s in sources)
                    or len({s[0] for s in sources})!=len(sources) or len({tuple(s[1:]) for s in sources})!=len(sources)
                    or batch!=f'source-followup:{sources[-1][1]}:{sources[-1][2]}'):
                return blocked('invalid replay identity or state')
            if (any(not isinstance(plan[k],str) for k in ('intent','intent_rule','rule'))
                    or not isinstance(plan['present'],dict)
                    or any(not isinstance(v,dict) or not isinstance(v.get('value'),str) or not isinstance(v.get('source_ids'),list)
                           or any(type(i) is not int or i not in {s[0] for s in sources} for i in v['source_ids']) for v in plan['present'].values())
                    or any(not isinstance(plan[k],list) or any(not isinstance(v,str) for v in plan[k]) for k in ('questions','asked_fields'))):
                return blocked('invalid replay facts')
            if not connection.execute('SELECT 1 FROM rules_batches WHERE batch_id=?',(batch,)).fetchone(): return blocked('header missing')
            if not connection.execute('SELECT 1 FROM rules_topics WHERE topic_id=?',(topic,)).fetchone(): return blocked('topic missing')
            current=[list(r) for r in connection.execute('SELECT ref,chat_id,message_id FROM rules_sources WHERE topic_id=? ORDER BY ref',(topic,))]
            if current!=sources: return blocked('sources changed')
            members=[]
            for _,chat,mid in sources:
                row=connection.execute('''SELECT chat_id,message_id,decision,attempts,github_called_at,case_key,issue_number,issue_url
                    FROM messages WHERE chat_id=? AND message_id=?''',(chat,mid)).fetchone()
                if row is None: return blocked('source message missing')
                member=dict(zip(('chat','mid','decision','attempts','github','case','issue','url'),row))
                if member['decision']=='error': return blocked(f'member {chat}:{mid} is quarantined')
                if (type(member['attempts']) is not int or member['attempts']<0
                        or member['attempts']>=2 and (member['decision']=='queued' or before_recovery and member['decision']=='classifying')):
                    return blocked(f'member {chat}:{mid} cannot be recovered within the attempt limit')
                members.append(member)
            claim=connection.execute('SELECT batch_id FROM rules_inflight_topics WHERE topic_id=?',(topic,)).fetchone()
            if claim and claim[0]!='single:'+topic: return blocked('effect claimed by another operation')
            root=members[0]
            if plan['state']=='READY' and not (root['decision']=='issue' and root['issue'] is not None):
                local=[m for m in members if m['chat']==root['chat']]
                keys={(m['chat'],m['mid']) for m in local}
                cases={m['case'] for m in local if m['case'] is not None}
                for case in cases:
                    bound=connection.execute('SELECT chat_id,message_id,github_called_at FROM messages WHERE case_key=?',(case,)).fetchall()
                    if {tuple(r)[:2] for r in bound}!=keys or any(r[2] is not None for r in bound):
                        return blocked('case has unresolved reservation or different members')
                    if 'groups' not in tables or connection.execute('SELECT 1 FROM groups WHERE case_key=? AND github_called_at IS NOT NULL',(case,)).fetchone():
                        return blocked('group has unresolved reservation')
                for m in local:
                    if m['case'] is None and (m['github'] is not None or m['decision'] not in ('queued','classifying','pending_review')):
                        return blocked(f"member {m['chat']}:{m['mid']} is no longer available for READY")
        except (ValueError,TypeError,KeyError,sqlite3.Error):
            return blocked('cannot inspect replay plan safely')
    return None


def check_before_recovery(db_path):
    """Inspect existing side-effect ledgers read-only before Store can recover anything."""
    import sqlite3
    from pathlib import Path
    if str(db_path) == ':memory:': return
    path=Path(db_path)
    if not path.exists(): return
    connection=None
    try:
        connection=sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True)
        check_connection_before_recovery(connection)
    except sqlite3.Error:
        raise RulesHalt('cannot inspect operation ledger safely; Owner inspection required') from None
    finally:
        if connection is not None: connection.close()


def check_connection_before_recovery(connection):
    """Pure local gate; caller owns one stable read snapshot. No schema writes or tools."""
    tables={row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if local_binding_blockers(connection,tables):raise RulesHalt('invalid local ownership or receipt binding; Owner inspection required')
    from .legacy_preflight import REGISTRY,_category,MAX_ROWS
    for table,spec in REGISTRY.items():
        if table not in tables:continue
        assessment=_category(connection,table,spec,MAX_ROWS)
        if assessment['schema']!='SUPPORTED' or not assessment['validation_complete']:
            raise RulesHalt('unknown schema, identifier, state or record; Owner inspection required')
    followup_blocker=_source_followup_blocker(connection,tables,before_recovery=True)
    if followup_blocker: raise RulesHalt(followup_blocker)
    for table in ('messages','cases','groups'):
        if table in tables and not {'github_called_at','issue_number','issue_url'} <= {r[1] for r in connection.execute('PRAGMA table_info('+table+')')}:
            raise RulesHalt('reservation schema unknown; Owner inspection required')
        if table in tables and connection.execute('SELECT 1 FROM '+table+" WHERE github_called_at IS NOT NULL AND (typeof(issue_number)!='integer' OR issue_number<=0 OR typeof(issue_url)!='text' OR issue_url='') LIMIT 1").fetchone():
            raise RulesHalt('unconfirmed issue metadata; Owner inspection required')
    checks=(('rules_comments',"SELECT 1 FROM rules_comments WHERE status IN ('reserved','ambiguous') LIMIT 1"),
            ('rules_append_completions',"SELECT 1 FROM rules_append_completions WHERE status!='done' OR status IS NULL LIMIT 1"),
            ('rules_notices',"SELECT 1 FROM rules_notices WHERE status IN ('reserved','ambiguous') LIMIT 1"),
            ('rules_lifecycle_answers',"SELECT 1 FROM rules_lifecycle_answers WHERE status IN ('reserved','ambiguous') LIMIT 1"),
            ('rules_related_episodes',"SELECT 1 FROM rules_related_episodes WHERE status != 'done' OR status IS NULL LIMIT 1"),
            ('rules_batch_groups',"SELECT 1 FROM rules_batch_groups WHERE status='AMBIGUOUS' LIMIT 1"))
    for table,query in checks:
        if table in tables and connection.execute(query).fetchone():
            raise RulesHalt('unresolved ambiguous operation; Owner inspection required')
    if _unmarked_open_comment(connection,tables):
        raise RulesHalt('unmarked open comment requires Owner inspection')
    # Legacy work is not represented by rules_topics. Do not let Store's
    # startup recovery rewrite it or feed it to a different decision path.
    # This is a barrier, not a migration or permission to replay it.
    legacy_checks = (
        ('followup_jobs', "SELECT 1 FROM followup_jobs WHERE status NOT IN ('done','blocked') OR status IS NULL LIMIT 1"),
        ('followup_operations', "SELECT 1 FROM followup_operations WHERE status != 'done' OR status IS NULL LIMIT 1"),
        ('followup_analysis_retries', "SELECT 1 FROM followup_analysis_retries WHERE edit_status != 'done' OR edit_status IS NULL LIMIT 1"),
        ('autonomous_confirmations', "SELECT 1 FROM autonomous_confirmations WHERE status != 'sent' OR status IS NULL LIMIT 1"),
        ('group_notices', "SELECT 1 FROM group_notices WHERE status != 'sent' OR status IS NULL LIMIT 1"),
        ('attachments', "SELECT 1 FROM attachments WHERE status NOT IN ('uploaded','prewrite_error') OR status IS NULL LIMIT 1"),
    )
    for table, query in legacy_checks:
        if table in tables and connection.execute(query).fetchone():
            raise RulesHalt('unsupported legacy pending or uncertain operation; Owner inspection required')
    if 'autonomous_plans' in tables:
        # Legacy numbered offers remain replyable indefinitely. Until the
        # rules route supports their selection ledger, never silently
        # switch them to a classifier that cannot honor those replies.
        rows=connection.execute('SELECT CASE WHEN length(CAST(plan AS BLOB))<=262144 THEN plan ELSE NULL END FROM autonomous_plans LIMIT 1001')
        used=0
        for index,(raw,) in enumerate(rows):
            used+=len(raw.encode('utf8')) if isinstance(raw,str) else 0
            if index>=1000 or used>8*1024*1024:raise RulesHalt('legacy plan validation truncated; Owner inspection required')
            try:
                plan=json.loads(raw)
                if not isinstance(plan,dict): raise ValueError()
                if plan.get('action')=='ANALYZE' and 'task' not in plan and plan.get('options'):
                    raise RulesHalt('legacy analysis offer needs reconciliation before rules mode')
            except (TypeError,ValueError):
                raise RulesHalt('cannot inspect legacy plan safely; Owner inspection required') from None
    if 'messages' in tables:
        bound = ("AND NOT EXISTS (SELECT 1 FROM rules_sources s JOIN rules_topics t ON t.topic_id=s.topic_id WHERE s.chat_id=m.chat_id AND s.message_id=m.message_id AND t.status='OPEN' AND t.phase IN ('NEW','HOLD','WAITING','LEGACY_REVIEW'))"
                 if 'rules_sources' in tables else '')
        if 'rules_deferred_ops' in tables:
            bound += " AND NOT EXISTS (SELECT 1 FROM rules_deferred_ops d JOIN rules_topics t ON t.topic_id=d.topic_id WHERE d.chat_id=m.chat_id AND d.message_id=m.message_id AND t.status='OPEN')"
        if 'rules_sources' in tables:
            # Live 2026-11-12: expiry is terminal per states.md but leaves NEED_INFO
            # sources pending_review, including their completed CLOSED batch binding.
            # No notice is required for expiry. Binding and uncertain-effect checks
            # above still apply, and another ESCALATED binding must retain its sent
            # escalation proof. Only no-GitHub/no-issue pending_review is settled.
            unnotified = ("NOT EXISTS (SELECT 1 FROM rules_notices n WHERE n.topic_id=t.topic_id AND n.kind='escalation'"
                          " AND n.status='sent' AND n.notice_id>0)" if 'rules_notices' in tables else '1')
            bound += (" AND NOT (m.decision='pending_review' AND m.github_called_at IS NULL AND m.issue_number IS NULL"
                      " AND m.issue_url IS NULL AND EXISTS (SELECT 1 FROM rules_sources s JOIN rules_topics t ON t.topic_id=s.topic_id"
                      " WHERE s.chat_id=m.chat_id AND s.message_id=m.message_id AND t.status='EXPIRED')"
                      " AND NOT EXISTS (SELECT 1 FROM rules_sources s JOIN rules_topics t ON t.topic_id=s.topic_id"
                      " WHERE s.chat_id=m.chat_id AND s.message_id=m.message_id AND t.status='ESCALATED' AND " + unnotified + "))")
        if {'rules_sources','rules_notices'} <= tables:
            # Live 2026-11-05: an escalated HOLD (terminal per states.md) left its no-ticket
            # `pending_review` source here and halted every later restart. Settled only when the
            # Owner was provably notified (a sent escalation notice), with no GitHub call and no
            # issue. An escalation without a sent notice (e.g. lifecycle HOLD) still halts.
            bound += (" AND NOT (m.decision='pending_review' AND m.github_called_at IS NULL AND m.issue_number IS NULL"
                      " AND m.issue_url IS NULL AND EXISTS (SELECT 1 FROM rules_sources s JOIN rules_topics t ON t.topic_id=s.topic_id"
                      " WHERE s.chat_id=m.chat_id AND s.message_id=m.message_id AND t.status='ESCALATED'"
                      " AND EXISTS (SELECT 1 FROM rules_notices n WHERE n.topic_id=t.topic_id AND n.kind='escalation'"
                      " AND n.status='sent' AND n.notice_id>0)))")
        unowned=connection.execute("SELECT m.chat_id,m.message_id FROM messages m WHERE m.decision IN ('queued','classifying','in_case','pending','pending_review') " + bound + " LIMIT 1001").fetchall()
        if unowned:
            settled=_proven_closed_chat_sources(connection)
            if len(unowned)>1000 or any(tuple(row) not in settled for row in unowned):
                raise RulesHalt('unsupported legacy pending messages; Owner inspection required')
        columns={row[1] for row in connection.execute('PRAGMA table_info(messages)')}
        # Older stores may have errors from before POST reservations existed.
        # Their outcome cannot be inferred from a missing reservation column.
        uncertain = ("m.github_called_at IS NOT NULL AND m.decision IN ('pending','error','queued','classifying','in_case','pending_review')"
                     if 'github_called_at' in columns else "m.decision='error'")
        if connection.execute('SELECT 1 FROM messages m WHERE ' + uncertain + ' LIMIT 1').fetchone():
            raise RulesHalt('unresolved legacy POST outcome; Owner inspection required')
    if 'cases' in tables:
        bound = ("AND (NOT EXISTS (SELECT 1 FROM messages m WHERE m.case_key=c.case_key) OR EXISTS (SELECT 1 FROM messages m WHERE m.case_key=c.case_key AND NOT EXISTS (SELECT 1 FROM rules_sources s JOIN rules_topics t ON t.topic_id=s.topic_id WHERE s.chat_id=m.chat_id AND s.message_id=m.message_id AND t.status='OPEN' AND t.phase IN ('NEW','HOLD','WAITING','LEGACY_REVIEW'))))"
                 if {'messages','rules_sources'} <= tables else '')
        if connection.execute("SELECT 1 FROM cases c WHERE c.status IN ('open','analyzing') " + bound + " LIMIT 1").fetchone():
            raise RulesHalt('unsupported legacy pending case; Owner inspection required')
    for table in ('cases','groups'):
        if table not in tables: continue
        columns={row[1] for row in connection.execute('PRAGMA table_info('+table+')')}
        if 'github_called_at' not in columns:
            condition="status='error'"
        elif 'issue_number' not in columns:
            condition='github_called_at IS NOT NULL'
        else:
            condition='github_called_at IS NOT NULL AND (issue_number IS NULL OR issue_number<=0)'
        if connection.execute('SELECT 1 FROM '+table+' WHERE '+condition+' LIMIT 1').fetchone():
            raise RulesHalt('unresolved legacy issue reservation; Owner inspection required')
    if {'rules_sources','attachments'} <= tables:
        columns={row[1] for row in connection.execute('PRAGMA table_info(attachments)')}
        join=("(a.chat_id=s.chat_id AND a.message_id=s.message_id) OR (a.chat_id IS NULL AND a.case_key=s.topic_id AND a.message_id=s.message_id)"
              if 'chat_id' in columns else "a.case_key=s.topic_id AND a.message_id=s.message_id")
        if connection.execute("SELECT 1 FROM attachments a JOIN rules_sources s ON " + join + " WHERE a.status IN ('uploading','ambiguous') LIMIT 1").fetchone():
            raise RulesHalt('unresolved ambiguous operation; Owner inspection required')
    if 'rules_batch_groups' in tables:
        rows=connection.execute("SELECT length(CAST(plan AS BLOB)) FROM rules_batch_groups WHERE status IN ('RESERVED','AMBIGUOUS') LIMIT 1001").fetchall()
        if len(rows)>1000 or sum(row[0] or 0 for row in rows)>8*1024*1024 or any(row[0] is None or row[0]>262144 for row in rows):raise RulesHalt('checkpoint validation incomplete; Owner inspection required')
        _recoverable_batch_groups(connection)
    if {'rules_sources','messages'} <= tables and connection.execute("""SELECT 1 FROM rules_sources s
        JOIN messages m ON m.chat_id=s.chat_id AND m.message_id=s.message_id
        WHERE m.github_called_at IS NOT NULL AND m.decision IN ('pending','error') LIMIT 1""").fetchone():
        raise RulesHalt('unresolved ambiguous operation; Owner inspection required')


class RulesPendingStore:
    def __init__(self, store):
        self.store = store
        with store._lock, store.connection:
            store.connection.execute('''CREATE TABLE IF NOT EXISTS rules_topics (
                topic_id TEXT PRIMARY KEY, source_chat_id INTEGER NOT NULL,
                last_at TEXT NOT NULL, status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                attempt_reserved INTEGER NOT NULL DEFAULT 0, phase TEXT NOT NULL DEFAULT 'NEW', asked TEXT NOT NULL DEFAULT '{}',
                fallback_asked INTEGER NOT NULL DEFAULT 0)''')
            store.connection.execute('''CREATE TABLE IF NOT EXISTS rules_sources (
                topic_id TEXT NOT NULL, ref INTEGER NOT NULL, chat_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL, PRIMARY KEY(topic_id,ref),
                UNIQUE(topic_id,chat_id,message_id))''')
            store.connection.execute('''CREATE TABLE IF NOT EXISTS rules_ops_replies (
                chat_id INTEGER NOT NULL, message_id INTEGER NOT NULL, topic_id TEXT NOT NULL,
                ref INTEGER NOT NULL, PRIMARY KEY(chat_id,message_id))''')
            store.connection.execute('''CREATE TABLE IF NOT EXISTS rules_inflight_topics (
                topic_id TEXT PRIMARY KEY, batch_id TEXT NOT NULL, ordinal INTEGER NOT NULL)''')
            store.connection.execute('''CREATE TABLE IF NOT EXISTS rules_deferred_ops (
                chat_id INTEGER NOT NULL, message_id INTEGER NOT NULL, topic_id TEXT NOT NULL,
                PRIMARY KEY(chat_id,message_id))''')
            store.connection.execute('''CREATE TABLE IF NOT EXISTS rules_notices (
                topic_id TEXT NOT NULL, sequence INTEGER NOT NULL, ops_chat_id INTEGER NOT NULL,
                kind TEXT NOT NULL, status TEXT NOT NULL, notice_id INTEGER,
                PRIMARY KEY(topic_id,sequence), UNIQUE(ops_chat_id,notice_id))''')
            store.connection.execute('''CREATE TABLE IF NOT EXISTS rules_decisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT, topic_id TEXT NOT NULL, recorded_at TEXT NOT NULL,
                state TEXT NOT NULL, intent_rule TEXT NOT NULL, decision_rule TEXT NOT NULL,
                fingerprint TEXT NOT NULL, present_sources TEXT NOT NULL, confidence REAL)''')
            store.connection.execute('''CREATE TABLE IF NOT EXISTS rules_batches (
                batch_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, recorded_at TEXT NOT NULL)''')
            store.connection.execute('''CREATE TABLE IF NOT EXISTS rules_batch_groups (
                batch_id TEXT NOT NULL, ordinal INTEGER NOT NULL, plan TEXT NOT NULL,
                status TEXT NOT NULL, PRIMARY KEY(batch_id,ordinal))''')
            store.connection.execute('''CREATE TABLE IF NOT EXISTS rules_comments (
                chat_id INTEGER NOT NULL, message_id INTEGER NOT NULL, repo TEXT NOT NULL,
                issue_number INTEGER NOT NULL, status TEXT NOT NULL, result INTEGER,
                PRIMARY KEY(chat_id,message_id))''')
            store.connection.execute('''CREATE TABLE IF NOT EXISTS rules_append_completions (
                chat_id INTEGER NOT NULL,message_id INTEGER NOT NULL,topic_id TEXT NOT NULL,status TEXT NOT NULL,
                PRIMARY KEY(chat_id,message_id))''')
            from .rules_analysis import init_store as init_analysis_store
            init_analysis_store(store.connection)
            store.connection.execute('''CREATE TABLE IF NOT EXISTS rules_readonly_notices (
                chat_id INTEGER NOT NULL, message_id INTEGER NOT NULL, kind TEXT NOT NULL,
                plan_id TEXT NOT NULL, body_hash TEXT NOT NULL, body TEXT NOT NULL,
                PRIMARY KEY(chat_id,message_id))''')
            store.connection.execute('''CREATE TABLE IF NOT EXISTS rules_lifecycle_questions (
                chat_id INTEGER NOT NULL,source_id INTEGER NOT NULL,topic_id TEXT NOT NULL,
                parent_number INTEGER NOT NULL,repo TEXT NOT NULL,plan_id TEXT NOT NULL,question TEXT NOT NULL,
                fingerprint TEXT,evidence_answer TEXT,recurrence_answer TEXT,batch_id TEXT,ordinal INTEGER,
                PRIMARY KEY(chat_id,source_id))''')
            question_columns={row[1] for row in store.connection.execute('PRAGMA table_info(rules_lifecycle_questions)')}
            for column in ('fingerprint','evidence_answer','recurrence_answer','batch_id','ordinal'):
                if column not in question_columns:
                    store.connection.execute('ALTER TABLE rules_lifecycle_questions ADD COLUMN '+column+' TEXT')
            store.connection.execute('''CREATE TABLE IF NOT EXISTS rules_lifecycle_answers (
                chat_id INTEGER NOT NULL,source_id INTEGER NOT NULL,answer_id INTEGER NOT NULL,
                choice TEXT NOT NULL,status TEXT NOT NULL,PRIMARY KEY(chat_id,source_id),UNIQUE(chat_id,answer_id))''')
            store.connection.execute('''CREATE TABLE IF NOT EXISTS rules_related_episodes (
                chat_id INTEGER NOT NULL,repo TEXT NOT NULL,parent_number INTEGER NOT NULL,
                source_id INTEGER NOT NULL,answer_id INTEGER NOT NULL,status TEXT NOT NULL,
                issue_number INTEGER,issue_url TEXT,notice_body TEXT,
                PRIMARY KEY(chat_id,repo,parent_number),UNIQUE(chat_id,answer_id))''')
            store.connection.execute("""CREATE TABLE IF NOT EXISTS rules_related_receipts (
                chat_id INTEGER NOT NULL,repo TEXT NOT NULL,parent_number INTEGER NOT NULL,
                plan_hash TEXT NOT NULL,recurrence_keys TEXT NOT NULL,source_keys TEXT NOT NULL,PRIMARY KEY(chat_id,repo,parent_number))""")
            store.connection.execute('CREATE UNIQUE INDEX IF NOT EXISTS rules_related_parent_once ON rules_related_episodes(repo COLLATE NOCASE,parent_number)')
            # An interrupted model call consumed its attempt; no operation may be replayed.
            store.connection.execute('UPDATE rules_topics SET attempt_reserved=0 WHERE attempt_reserved=1')
            store.connection.execute("UPDATE rules_comments SET status='ambiguous' WHERE status='reserved'")
            store.connection.execute("UPDATE rules_notices SET status='ambiguous' WHERE status='reserved'")
            try:
                recoverable=_recoverable_batch_groups(store.connection)
            except RulesHalt:
                recoverable=None
            if recoverable is not None:
                for batch_id,ordinal,status in recoverable:
                    if status=='DONE':
                        row=store.connection.execute('SELECT plan FROM rules_batch_groups WHERE batch_id=? AND ordinal=?',(batch_id,ordinal)).fetchone()
                        plan=json.loads(row[0])
                        if plan.get('state') in ('ANALYZE','STATUS'):
                            key=plan['source_keys'][0]
                            topic=plan.get('pending_topic') or f'{key[0]}:{key[1]}'
                            if plan.get('analysis_review'):
                                from .rules_analysis import finish_members
                                from .models import Decision
                                finish_members(store,plan,Decision('chat','rule',case_key=topic,reason='semantic_analyze'))
                            question=store.connection.execute("SELECT 1 FROM rules_notices WHERE topic_id=? AND kind='question' AND status='sent'",(topic,)).fetchone()
                            if plan.get('state')!='STATUS' or not question:
                                store.connection.execute("UPDATE rules_topics SET status='CLOSED' WHERE topic_id=? AND status='OPEN'",(topic,))
                    store.connection.execute("UPDATE rules_batch_groups SET status=? WHERE batch_id=? AND ordinal=? AND status='RESERVED'",(status,batch_id,ordinal))
            # Internal claims have no external effect of their own.  Reconcile
            # only after proving that every actual write reservation is resolved.
            if not self.unresolved_ambiguity():
                store.connection.execute("""UPDATE rules_topics SET status='CLOSED'
                    WHERE status='OPEN' AND topic_id IN (
                        SELECT i.topic_id FROM rules_inflight_topics i
                        JOIN rules_sources s ON s.topic_id=i.topic_id AND s.ref=1
                        JOIN messages m ON m.chat_id=s.chat_id AND m.message_id=s.message_id
                        WHERE m.decision='issue' AND m.github_called_at IS NOT NULL
                          AND m.issue_number > 0 AND m.issue_url IS NOT NULL)""")
                store.connection.execute('DELETE FROM rules_inflight_topics')

    def unresolved_ambiguity(self):
        with self.store._lock:
            tables={row[0] for row in self.store.connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if _unmarked_open_comment(self.store.connection,tables): return True
            for query in ("SELECT 1 FROM rules_comments WHERE status IN ('reserved','ambiguous') LIMIT 1",
                          "SELECT 1 FROM rules_append_completions WHERE status!='done' OR status IS NULL LIMIT 1",
                          "SELECT 1 FROM rules_notices WHERE status IN ('reserved','ambiguous') LIMIT 1",
                          "SELECT 1 FROM rules_lifecycle_answers WHERE status IN ('reserved','ambiguous') LIMIT 1",
                          "SELECT 1 FROM rules_related_episodes WHERE status != 'done' OR status IS NULL LIMIT 1",
                          "SELECT 1 FROM group_notices WHERE status IN ('reserved','failed') LIMIT 1",
                          "SELECT 1 FROM rules_batch_groups WHERE status IN ('RESERVED','AMBIGUOUS') LIMIT 1",
                          """SELECT 1 FROM attachments a JOIN rules_sources s
                             ON (a.chat_id=s.chat_id AND a.message_id=s.message_id) OR (a.chat_id IS NULL AND a.case_key=s.topic_id AND a.message_id=s.message_id)
                             WHERE a.status IN ('uploading','ambiguous') LIMIT 1""",
                          """SELECT 1 FROM rules_sources s JOIN messages m ON m.chat_id=s.chat_id AND m.message_id=s.message_id
                             WHERE m.github_called_at IS NOT NULL AND m.decision IN ('pending','error') LIMIT 1"""):
                if self.store.connection.execute(query).fetchone(): return True
            return _source_followup_blocker(self.store.connection,tables) is not None

    def reserve_comment(self, chat_id, message_id, repo, issue_number, topic=None):
        with self.store._lock,self.store.connection:
            inserted=self.store.connection.execute("INSERT OR IGNORE INTO rules_comments VALUES(?,?,?,?,'reserved',NULL)", (chat_id,message_id,repo,issue_number)).rowcount == 1
            if inserted and topic is not None:
                changed=self.store.connection.execute("INSERT INTO rules_append_completions VALUES(?,?,?,'reserved')",(chat_id,message_id,topic)).rowcount
                row=self.store.connection.execute('SELECT topic_id,status FROM rules_append_completions WHERE chat_id=? AND message_id=?',(chat_id,message_id)).fetchone()
                if changed!=1 or row is None or tuple(row)!=(topic,'reserved'):
                    raise RulesHalt('append reservation unresolved; Owner inspection required')
            return inserted

    def finish_append(self,chat_id,message_id,topic):
        with self.store._lock,self.store.connection:
            changed=self.store.connection.execute("UPDATE rules_append_completions SET status='done' WHERE chat_id=? AND message_id=? AND topic_id=? AND status='reserved'",(chat_id,message_id,topic)).rowcount
            row=self.store.connection.execute('SELECT topic_id,status FROM rules_append_completions WHERE chat_id=? AND message_id=?',(chat_id,message_id)).fetchone()
            if changed!=1 or row is None or tuple(row)!=(topic,'done'):
                raise RulesHalt('append completion unresolved; Owner inspection required')

    def finish_comment(self, chat_id, message_id, comment_id):
        status='done' if type(comment_id) is int and comment_id > 0 else 'ambiguous'
        with self.store._lock,self.store.connection:
            return self.store.connection.execute("UPDATE rules_comments SET status=?,result=? WHERE chat_id=? AND message_id=? AND status='reserved'", (status,comment_id if status=='done' else None,chat_id,message_id)).rowcount == 1

    def comment_status(self, chat_id, message_id):
        with self.store._lock:
            row=self.store.connection.execute('SELECT status FROM rules_comments WHERE chat_id=? AND message_id=?',(chat_id,message_id)).fetchone()
            return row['status'] if row else None

    def create(self, topic_id, chat_id, message_id):
        with self.store._lock, self.store.connection:
            now = self.store.clock().isoformat()
            added = self.store.connection.execute('INSERT OR IGNORE INTO rules_topics(topic_id,source_chat_id,last_at,status) VALUES(?,?,?,?)',
                                                  (topic_id, chat_id, now, 'OPEN')).rowcount
            if added:
                self.store.connection.execute('INSERT INTO rules_sources VALUES(?,?,?,?)', (topic_id, 1, chat_id, message_id))
            return bool(added)

    def add_source(self, topic_id, chat_id, message_id):
        with self.store._lock, self.store.connection:
            row = self.store.connection.execute('SELECT status FROM rules_topics WHERE topic_id=?', (topic_id,)).fetchone()
            if row is None or row['status'] != 'OPEN': return None
            found = self.store.connection.execute('SELECT ref FROM rules_sources WHERE topic_id=? AND chat_id=? AND message_id=?', (topic_id,chat_id,message_id)).fetchone()
            if found: return found['ref']
            ref = self.store.connection.execute('SELECT COALESCE(MAX(ref),0)+1 FROM rules_sources WHERE topic_id=?', (topic_id,)).fetchone()[0]
            self.store.connection.execute('INSERT INTO rules_sources VALUES(?,?,?,?)', (topic_id,ref,chat_id,message_id))
            self.store.connection.execute('UPDATE rules_topics SET last_at=? WHERE topic_id=?', (self.store.clock().isoformat(), topic_id))
            return ref

    def sources(self, topic_id):
        with self.store._lock:
            return {r['ref']: (r['chat_id'],r['message_id']) for r in self.store.connection.execute('SELECT ref,chat_id,message_id FROM rules_sources WHERE topic_id=?', (topic_id,))}

    def topic_snapshot(self, topic_id):
        with self.store._lock:
            row = self.store.connection.execute("SELECT status,last_at,asked,fallback_asked FROM rules_topics WHERE topic_id=?", (topic_id,)).fetchone()
            if row is None: return None
            sources = [[r['ref'],r['chat_id'],r['message_id']] for r in self.store.connection.execute(
                "SELECT ref,chat_id,message_id FROM rules_sources WHERE topic_id=? ORDER BY ref", (topic_id,))]
            return dict(status=row['status'],last_at=row['last_at'],asked=row['asked'],fallback_asked=row['fallback_asked'],sources=sources)

    def is_bound_ops_reply(self, chat_id, message_id):
        with self.store._lock:
            return self.store.connection.execute('SELECT 1 FROM rules_ops_replies WHERE chat_id=? AND message_id=? UNION ALL SELECT 1 FROM rules_deferred_ops WHERE chat_id=? AND message_id=?', (chat_id,message_id,chat_id,message_id)).fetchone() is not None

    def is_deferred_ops_reply(self, chat_id, message_id):
        with self.store._lock:
            return self.store.connection.execute('SELECT 1 FROM rules_deferred_ops WHERE chat_id=? AND message_id=?', (chat_id,message_id)).fetchone() is not None

    def topic_for_ops_reply(self, chat_id, message_id):
        with self.store._lock:
            row = self.store.connection.execute('''SELECT t.topic_id,t.status,t.asked FROM rules_ops_replies r
                JOIN rules_topics t ON t.topic_id=r.topic_id WHERE r.chat_id=? AND r.message_id=?''', (chat_id,message_id)).fetchone()
            return (row['topic_id'],json.loads(row['asked'])) if row and row['status'] == 'OPEN' else None

    def topic_for_source_reply(self, chat_id, reply_to_id, expiry_days):
        self.expire_due(expiry_days)
        with self.store._lock:
            rows=self.store.connection.execute('''SELECT t.topic_id,t.asked FROM rules_sources s JOIN rules_topics t ON t.topic_id=s.topic_id
                WHERE s.chat_id=? AND s.message_id=? AND t.source_chat_id=? AND t.status='OPEN' AND t.topic_id NOT LIKE 'batch:%' ''',
                (chat_id,reply_to_id,chat_id)).fetchall()
            return (rows[0]['topic_id'],json.loads(rows[0]['asked'])) if len(rows)==1 else None

    def topic_for_source_followup(self, chat_id, sender_id, window_minutes, received_at=None):
        """questions.source_followup SF1: read only; ambiguity never picks a winner."""
        from datetime import datetime
        from .store import _aware
        if sender_id is None: return None
        if type(window_minutes) is not int or window_minutes <= 0: raise ValueError('invalid followup window')
        with self.store._lock:
            rows = self.store.connection.execute('''SELECT t.topic_id,t.last_at FROM rules_topics t
                WHERE t.source_chat_id=? AND t.status='OPEN' AND t.topic_id NOT LIKE 'batch:%'
                AND t.phase NOT IN ('LEGACY_REVIEW','HOLD')
                AND NOT EXISTS (SELECT 1 FROM rules_inflight_topics i WHERE i.topic_id=t.topic_id)
                AND (SELECT d.state FROM rules_decisions d WHERE d.topic_id=t.topic_id ORDER BY d.id DESC LIMIT 1) IN ('NEED_INFO','MEDIA_ONLY')
                AND NOT EXISTS (SELECT 1 FROM rules_sources s JOIN messages m
                    ON m.chat_id=s.chat_id AND m.message_id=s.message_id
                    WHERE s.topic_id=t.topic_id AND m.has_document=1)
                AND EXISTS (SELECT 1 FROM rules_sources s JOIN messages m
                    ON m.chat_id=s.chat_id AND m.message_id=s.message_id
                    WHERE s.topic_id=t.topic_id AND s.chat_id=? AND m.sender_id=?)''',
                (chat_id,chat_id,sender_id)).fetchall()
            now = _aware(received_at or self.store.clock())
            matches = []
            for row in rows:
                sources = self.store.connection.execute('''SELECT m.* FROM rules_sources s JOIN messages m
                    ON m.chat_id=s.chat_id AND m.message_id=s.message_id WHERE s.topic_id=?''', (row['topic_id'],)).fetchall()
                latest = max(_aware(datetime.fromisoformat(m['received_at'])) for m in sources)
                if not timedelta(0) <= now - latest < timedelta(minutes=window_minutes): continue
                state = self.store.connection.execute('SELECT state FROM rules_decisions WHERE topic_id=? ORDER BY id DESC LIMIT 1', (row['topic_id'],)).fetchone()[0]
                if state == 'MEDIA_ONLY':
                    if any(m['chat_id'] != chat_id or m['sender_id'] != sender_id or m['text'].strip()
                           or not m['has_image'] or m['is_forward'] for m in sources): continue
                matches.append(row['topic_id'])
            return matches[0] if len(matches) == 1 else None

    def prepare_source_followup(self, message, max_retries, fingerprint):
        """Own HOLD checkpoint before speculative IO; never touches the candidate."""
        topic = f'{message.chat_id}:{message.message_id}'
        with self.store._lock, self.store.connection:
            now = self.store.clock().isoformat()
            self.store.connection.execute("INSERT OR IGNORE INTO rules_topics(topic_id,source_chat_id,last_at,status) VALUES(?,?,?,'OPEN')", (topic,message.chat_id,now))
            self.store.connection.execute('INSERT OR IGNORE INTO rules_sources VALUES(?,1,?,?)', (topic,message.chat_id,message.message_id))
            row = self.store.connection.execute('SELECT attempts,attempt_reserved FROM rules_topics WHERE topic_id=?', (topic,)).fetchone()
            if row['attempt_reserved'] or row['attempts'] >= max_retries + 1: return None
            attempt = row['attempts'] + 1
            self.store.connection.execute("UPDATE rules_topics SET phase='HOLD',attempts=?,attempt_reserved=1 WHERE topic_id=?", (attempt,topic))
            self.store.connection.execute("UPDATE messages SET decision='pending_review',reason='rules_extract_pending' WHERE chat_id=? AND message_id=?", (message.chat_id,message.message_id))
            batch = 'source-followup-confirmation:' + topic
            self.store.connection.execute('INSERT OR IGNORE INTO rules_batches VALUES(?,?,?)', (batch,fingerprint,now))
            self.store.connection.execute("INSERT OR IGNORE INTO rules_batch_groups VALUES(?,0,?,'PLANNED')", (batch,json.dumps(dict(topic=topic))))
            return attempt

    def release_source_confirmation(self, message):
        """Keep a replayable HOLD until the old path durably records its outcome."""
        with self.store._lock, self.store.connection:
            topic = f'{message.chat_id}:{message.message_id}'
            batch = 'source-followup-confirmation:' + topic
            row = self.store.connection.execute("SELECT plan FROM rules_batch_groups WHERE batch_id=? AND ordinal=0 AND status='PLANNED'", (batch,)).fetchone()
            if row is None: return
            plan = json.loads(row['plan'])
            if plan.get('released'): return
            self.store.connection.execute("UPDATE rules_topics SET phase='HOLD',attempts=0,attempt_reserved=0 WHERE topic_id=? AND status='OPEN'", (topic,))
            plan['released'] = True
            self.store.connection.execute('UPDATE rules_batch_groups SET plan=? WHERE batch_id=? AND ordinal=0', (json.dumps(plan),batch))

    def finish_source_confirmations(self):
        """Reconcile only this feature's own checkpoints, including after restart."""
        with self.store._lock, self.store.connection:
            rows = self.store.connection.execute("SELECT batch_id,plan FROM rules_batch_groups WHERE batch_id LIKE 'source-followup-confirmation:%' AND ordinal=0 AND status='PLANNED'").fetchall()
            for row in rows:
                topic = json.loads(row['plan'])['topic']
                # The extension path owns a different topic. Retire the scratch
                # only after its replacement has durably reserved an attempt.
                state = self.store.connection.execute('SELECT state FROM rules_decisions WHERE topic_id=? ORDER BY id DESC LIMIT 1', (topic,)).fetchone()
                replacement = self.store.connection.execute('SELECT attempts,phase,status FROM rules_topics WHERE topic_id=?', ('batch:'+topic,)).fetchone()
                if replacement and (replacement['attempts'] or replacement['phase'] == 'WAITING' or replacement['status'] != 'OPEN'):
                    if state is None:
                        self.store.connection.execute('DELETE FROM rules_sources WHERE topic_id=?', (topic,))
                        self.store.connection.execute('DELETE FROM rules_topics WHERE topic_id=?', (topic,))
                    else:
                        # A successful batch can reuse the scratch id for its
                        # real single-source group. Restore that group's NEW phase.
                        self.store.connection.execute("UPDATE rules_topics SET phase='NEW',attempts=0,attempt_reserved=0 WHERE topic_id=?", (topic,))
                    self.store.connection.execute("UPDATE rules_batch_groups SET status='DONE' WHERE batch_id=? AND ordinal=0", (row['batch_id'],))
                    continue
                if state is None: continue
                # Same-topic old-path HOLD owns its retry counter; never reset it.
                if state[0] == 'MEDIA_ONLY':
                    self.store.connection.execute("UPDATE rules_topics SET phase='NEW',attempts=0,attempt_reserved=0 WHERE topic_id=?", (topic,))
                self.store.connection.execute("UPDATE rules_batch_groups SET status='DONE' WHERE batch_id=? AND ordinal=0", (row['batch_id'],))

    def source_followup_for_member(self, message):
        """A restarted worker may dequeue any member before the anchor."""
        with self.store._lock:
            for row in self.store.connection.execute('''SELECT g.plan,g.status,b.fingerprint FROM rules_batch_groups g
                    JOIN rules_batches b ON b.batch_id=g.batch_id
                    WHERE g.batch_id LIKE 'source-followup:%' AND g.ordinal=0 AND g.status='PLANNED' '''):
                plan = json.loads(row['plan'])
                if any((chat,mid) == (message.chat_id,message.message_id) for _,chat,mid in plan['sources']):
                    return plan,row['status'],row['fingerprint']
            return None

    def source_followup_checkpoint(self, message):
        with self.store._lock:
            row = self.store.connection.execute('''SELECT g.plan,g.status,b.fingerprint FROM rules_batch_groups g
                JOIN rules_batches b ON b.batch_id=g.batch_id WHERE g.batch_id=? AND g.ordinal=0''',
                (f'source-followup:{message.chat_id}:{message.message_id}',)).fetchone()
            return (json.loads(row['plan']),row['status'],row['fingerprint']) if row else None

    def commit_source_followup(self, message, topic, snapshot, originals, decision, window_minutes):
        """One SQLite transaction: recheck, claim, add source, journal and replay plan."""
        batch = f'source-followup:{message.chat_id}:{message.message_id}'
        own = f'{message.chat_id}:{message.message_id}'
        with self.store._lock:
            connection = self.store.connection
            connection.execute('BEGIN IMMEDIATE')
            try:
                if (self.topic_for_source_followup(message.chat_id,message.sender_id,window_minutes,message.received_at) != topic
                        or self.topic_snapshot(topic) != snapshot):
                    connection.rollback(); return False
                if any((chat,mid) == (message.chat_id,message.message_id) for _,chat,mid in snapshot['sources']):
                    connection.rollback(); return False
                now = self.store.clock().isoformat()
                ref = originals[-1][0]
                connection.execute('INSERT INTO rules_inflight_topics VALUES(?,?,0)', (topic,'single:'+topic))
                connection.execute('INSERT INTO rules_sources VALUES(?,?,?,?)', (topic,ref,message.chat_id,message.message_id))
                connection.execute('UPDATE rules_topics SET last_at=? WHERE topic_id=?', (now,topic))
                lookup = {i:(m.chat_id,m.message_id) for i,m in originals}
                provenance = {k:[dict(chat_id=lookup[i][0],message_id=lookup[i][1]) for i in v.source_ids] for k,v in decision.present.items()}
                connection.execute('''INSERT INTO rules_decisions(topic_id,recorded_at,state,intent_rule,decision_rule,fingerprint,present_sources)
                    VALUES(?,?,?,?,?,?,?)''', (topic,now,decision.state,decision.intent_rule,decision.rule,decision.fingerprint,json.dumps(provenance,separators=(',',':'))))
                plan = dict(topic=topic,state=decision.state,intent=decision.intent,intent_rule=decision.intent_rule,rule=decision.rule,
                            present={k:dict(value=v.value,source_ids=list(v.source_ids)) for k,v in decision.present.items()},
                            questions=list(decision.questions),asked_fields=list(decision.asked_fields),
                            sources=[[i,m.chat_id,m.message_id] for i,m in originals])
                connection.execute('INSERT INTO rules_batches VALUES(?,?,?)', (batch,decision.fingerprint,now))
                connection.execute("INSERT INTO rules_batch_groups VALUES(?,0,?,'PLANNED')", (batch,json.dumps(plan,ensure_ascii=False,separators=(',',':'))))
                connection.execute("UPDATE rules_topics SET status='CLOSED',phase='WAITING',attempts=0,attempt_reserved=0 WHERE topic_id=?", (own,))
                connection.execute("UPDATE rules_batch_groups SET status='DONE' WHERE batch_id=? AND ordinal=0", ('source-followup-confirmation:'+own,))
                connection.commit()
                return True
            except BaseException:
                connection.rollback()
                raise

    def source_followup_replays(self):
        with self.store._lock:
            return [json.loads(row[0])['sources'][-1][1:] for row in self.store.connection.execute(
                "SELECT plan FROM rules_batch_groups WHERE batch_id LIKE 'source-followup:%' AND ordinal=0 AND status='PLANNED'")]

    def finish_source_followup(self, message, topic, result):
        from dataclasses import asdict
        with self.store._lock, self.store.connection:
            batch = f'source-followup:{message.chat_id}:{message.message_id}'
            plan = json.loads(self.store.connection.execute('SELECT plan FROM rules_batch_groups WHERE batch_id=? AND ordinal=0',(batch,)).fetchone()[0])
            plan['result'] = {name:getattr(result,name) for name in ('decision','tier','case_key','issue_number','issue_url','reason')}
            if result.classification is not None: plan['result']['classification'] = asdict(result.classification)
            self.store.connection.execute("UPDATE rules_batch_groups SET status='DONE',plan=? WHERE batch_id=? AND ordinal=0", (json.dumps(plan,ensure_ascii=False,separators=(',',':')),batch))
            self.store.connection.execute("DELETE FROM rules_inflight_topics WHERE topic_id=? AND batch_id=?", (topic,'single:'+topic))

    def record_decision(self, topic_id, decision, confidence=None):
        import math
        if confidence is not None and (type(confidence) not in (int,float) or not math.isfinite(confidence) or not 0 <= confidence <= 1): raise ValueError('invalid confidence')
        with self.store._lock, self.store.connection:
            refs = self.sources(topic_id)
            provenance = {}
            for name, field in decision.present.items():
                if not field.source_ids or any(ref not in refs for ref in field.source_ids): raise ValueError('unknown source ref')
                provenance[name] = [{'chat_id': refs[ref][0], 'message_id': refs[ref][1]} for ref in field.source_ids]
            self.store.connection.execute('''INSERT INTO rules_decisions(topic_id,recorded_at,state,intent_rule,decision_rule,fingerprint,present_sources,confidence)
                VALUES(?,?,?,?,?,?,?,?)''', (topic_id,self.store.clock().isoformat(),decision.state,decision.intent_rule,decision.rule,
                                            decision.fingerprint,json.dumps(provenance,separators=(',',':')),confidence))

    def close(self, topic_id, status):
        if status not in ('CLOSED','EXPIRED','ESCALATED'): raise ValueError('invalid terminal state')
        with self.store._lock, self.store.connection:
            return self.store.connection.execute("UPDATE rules_topics SET status=? WHERE topic_id=? AND status='OPEN'", (status,topic_id)).rowcount == 1

    def save_batch(self, batch_id, fingerprint, plans, pending_updates=()):
        with self.store._lock, self.store.connection:
            if self.store.connection.execute('SELECT 1 FROM rules_batches WHERE batch_id=?', (batch_id,)).fetchone(): return False
            for ordinal,topic,expected,additions in pending_updates:
                if self.topic_snapshot(topic) != expected: return False
            for ordinal,topic,expected,additions in pending_updates:
                for ref,chat_id,message_id in additions:
                    self.store.connection.execute('INSERT INTO rules_sources VALUES(?,?,?,?)', (topic,ref,chat_id,message_id))
                if additions:
                    self.store.connection.execute('UPDATE rules_topics SET last_at=? WHERE topic_id=?', (self.store.clock().isoformat(),topic))
                plans[ordinal]['pending_snapshot'] = self.topic_snapshot(topic)
            inserted = self.store.connection.execute('INSERT OR IGNORE INTO rules_batches VALUES(?,?,?)', (batch_id,fingerprint,self.store.clock().isoformat())).rowcount
            if not inserted: return False
            for ordinal,plan in enumerate(plans):
                self.store.connection.execute("INSERT INTO rules_batch_groups VALUES(?,?,?,'PLANNED')", (batch_id,ordinal,json.dumps(plan,ensure_ascii=False,separators=(',',':'))))
            return True

    def batch(self, batch_id):
        with self.store._lock:
            header=self.store.connection.execute('SELECT fingerprint FROM rules_batches WHERE batch_id=?',(batch_id,)).fetchone()
            if header is None: return None
            rows=self.store.connection.execute('SELECT ordinal,plan,status FROM rules_batch_groups WHERE batch_id=? ORDER BY ordinal',(batch_id,)).fetchall()
            return header['fingerprint'], [(r['ordinal'],json.loads(r['plan']),r['status']) for r in rows]

    def claim_topic(self, topic_id, snapshot):
        with self.store._lock, self.store.connection:
            if self.topic_snapshot(topic_id) != snapshot: return False
            return self.store.connection.execute('INSERT OR IGNORE INTO rules_inflight_topics VALUES(?,?,?)',
                                                 (topic_id,'single:'+topic_id,0)).rowcount == 1

    def release_topic(self, topic_id):
        with self.store._lock, self.store.connection:
            return self.store.connection.execute('DELETE FROM rules_inflight_topics WHERE topic_id=? AND batch_id=?',
                                                 (topic_id,'single:'+topic_id)).rowcount == 1

    def transition_group(self, batch_id, ordinal, old, new, *, topic=None, snapshot=None):
        if (old,new) not in {('PLANNED','RESERVED'),('RESERVED','DONE'),('RESERVED','AMBIGUOUS'),('RESERVED','BLOCKED')}:
            raise ValueError('invalid batch transition')
        with self.store._lock,self.store.connection:
            if topic is not None and self.topic_snapshot(topic) != snapshot: return False
            changed = self.store.connection.execute('UPDATE rules_batch_groups SET status=? WHERE batch_id=? AND ordinal=? AND status=?',(new,batch_id,ordinal,old)).rowcount == 1
            if changed and topic is not None:
                self.store.connection.execute('INSERT INTO rules_inflight_topics VALUES(?,?,?)', (topic,batch_id,ordinal))
            if changed and new in ('DONE','BLOCKED'):
                self.store.connection.execute('DELETE FROM rules_inflight_topics WHERE batch_id=? AND ordinal=?', (batch_id,ordinal))
            return changed

    def reserve_attempt(self, topic_id, max_retries):
        if type(max_retries) is not int or max_retries < 0: raise ValueError('invalid retry limit')
        with self.store._lock, self.store.connection:
            row = self.store.connection.execute('SELECT attempts,status,attempt_reserved FROM rules_topics WHERE topic_id=?', (topic_id,)).fetchone()
            if row is None or row['status'] != 'OPEN' or row['attempt_reserved'] or row['attempts'] >= max_retries + 1: return None
            attempt = row['attempts'] + 1
            self.store.connection.execute("UPDATE rules_topics SET attempts=?,attempt_reserved=1,phase='HOLD' WHERE topic_id=?", (attempt,topic_id))
            return attempt

    def finish_attempt(self, topic_id, attempt, *, success=False):
        if type(success) is not bool: raise ValueError('invalid attempt result')
        with self.store._lock, self.store.connection:
            return self.store.connection.execute("UPDATE rules_topics SET attempt_reserved=0,attempts=CASE WHEN ? THEN 0 ELSE attempts END,phase=CASE WHEN ? THEN 'WAITING' ELSE 'HOLD' END WHERE topic_id=? AND attempts=? AND attempt_reserved=1", (success,success,topic_id,attempt)).rowcount == 1

    def hold_topics(self):
        with self.store._lock:
            return [row['topic_id'] for row in self.store.connection.execute("SELECT topic_id FROM rules_topics WHERE status='OPEN' AND phase='HOLD' AND attempt_reserved=0 ORDER BY last_at")]

    def expire_due(self, expiry_days):
        from datetime import datetime
        from .store import _aware
        with self.store._lock, self.store.connection:
            rows = self.store.connection.execute("SELECT topic_id,last_at FROM rules_topics WHERE status='OPEN' AND phase!='LEGACY_REVIEW'").fetchall()
            expired = [r['topic_id'] for r in rows if self.store.clock() - _aware(datetime.fromisoformat(r['last_at'])) >= timedelta(days=expiry_days)]
            for topic in expired:
                self.store.connection.execute("UPDATE rules_topics SET status='EXPIRED' WHERE topic_id=? AND status='OPEN'", (topic,))
            return len(expired)

    def reserve_notice(self, topic_id, ops_chat_id, kind, fields=(), *, max_asks_per_field=1, once=False):
        if kind not in ('question','escalation','other','error'): raise ValueError('invalid notice kind')
        if type(max_asks_per_field) is not int or max_asks_per_field <= 0: raise ValueError('invalid ask limit')
        fields = tuple(fields)
        if any(not isinstance(f,str) or not f for f in fields) or len(fields) != len(set(fields)): raise ValueError('invalid question fields')
        with self.store._lock, self.store.connection:
            row = self.store.connection.execute('SELECT * FROM rules_topics WHERE topic_id=?', (topic_id,)).fetchone()
            if row is None or row['status'] != 'OPEN': return None
            if once and self.store.connection.execute('SELECT 1 FROM rules_notices WHERE topic_id=? AND kind=?',(topic_id,kind)).fetchone(): return None
            asked = json.loads(row['asked'])
            if kind == 'question' and ((not fields and row['fallback_asked']) or any(asked.get(f,0) >= max_asks_per_field for f in fields)): return None
            if kind == 'escalation' and self.store.connection.execute("SELECT 1 FROM rules_notices WHERE topic_id=? AND kind='escalation'", (topic_id,)).fetchone(): return None
            if kind == 'error' and self.store.connection.execute("SELECT 1 FROM rules_notices WHERE topic_id=? AND kind='error'", (topic_id,)).fetchone(): return None
            sequence = self.store.connection.execute('SELECT COALESCE(MAX(sequence),0)+1 FROM rules_notices WHERE topic_id=?', (topic_id,)).fetchone()[0]
            self.store.connection.execute("INSERT INTO rules_notices VALUES(?,?,?,?,'reserved',NULL)", (topic_id,sequence,ops_chat_id,kind))
            if kind == 'question':
                for field in fields: asked[field] = asked.get(field, 0) + 1
                self.store.connection.execute('UPDATE rules_topics SET asked=?,fallback_asked=? WHERE topic_id=?', (json.dumps(asked), int(not fields or row['fallback_asked']), topic_id))
            return sequence

    def finish_notice(self, topic_id, sequence, notice_id):
        status = 'sent' if type(notice_id) is int and notice_id > 0 else 'ambiguous'
        with self.store._lock, self.store.connection:
            row=self.store.connection.execute('SELECT ops_chat_id FROM rules_notices WHERE topic_id=? AND sequence=? AND status=?',(topic_id,sequence,'reserved')).fetchone()
            if row is None: return False
            if status=='sent':
                chat=row[0]
                collision=(self.store.connection.execute('SELECT 1 FROM messages WHERE chat_id=? AND message_id=?',(chat,notice_id)).fetchone()
                    or self.store.connection.execute('SELECT 1 FROM rules_notices WHERE ops_chat_id=? AND notice_id=?',(chat,notice_id)).fetchone()
                    or self.store.connection.execute('SELECT 1 FROM group_notices WHERE chat_id=? AND notice_message_id=?',(chat,notice_id)).fetchone())
                if not collision and self.store.connection.execute("SELECT 1 FROM sqlite_master WHERE name='autonomous_confirmations'").fetchone():
                    collision=self.store.connection.execute('SELECT 1 FROM autonomous_confirmations WHERE chat_id=? AND notice_message_id=?',(chat,notice_id)).fetchone()
                if collision: status='ambiguous'
            return self.store.connection.execute('UPDATE rules_notices SET status=?,notice_id=? WHERE topic_id=? AND sequence=? AND status=?', (status,notice_id if status == 'sent' else None,topic_id,sequence,'reserved')).rowcount == 1

    def bind_reply(self, ops_chat_id, reply_to, message_id, expiry_days):
        if type(reply_to) is not int or type(message_id) is not int or reply_to <= 0 or message_id <= 0: return None
        if type(expiry_days) is not int or expiry_days <= 0: raise ValueError('invalid expiry')
        with self.store._lock, self.store.connection:
            return self._bind_reply(ops_chat_id, reply_to, message_id, expiry_days)

    def accept_ops_message(self, message, expiry_days):
        """Atomically persist an actual question reply and its original Telegram identity."""
        if type(message.reply_to_id) is not int or message.reply_to_id <= 0 or type(message.message_id) is not int or message.message_id <= 0: return False
        if type(expiry_days) is not int or expiry_days <= 0: raise ValueError('invalid expiry')
        with self.store._lock, self.store.connection:
            if self.store.get(message.chat_id, message.message_id) is not None: return False
            binding = self._bind_reply(message.chat_id, message.reply_to_id, message.message_id, expiry_days)
            return bool(binding and self.store._enqueue_sql(message))

    def _bind_reply(self, ops_chat_id, reply_to, message_id, expiry_days):
            row = self.store.connection.execute('''SELECT t.topic_id,t.last_at,t.status FROM rules_notices n
                JOIN rules_topics t ON t.topic_id=n.topic_id
                WHERE n.ops_chat_id=? AND n.notice_id=? AND n.kind='question' AND n.status='sent' ''', (ops_chat_id,reply_to)).fetchone()
            if row is None: return None
            if self.store.connection.execute('SELECT 1 FROM rules_inflight_topics WHERE topic_id=?', (row['topic_id'],)).fetchone():
                self.store.connection.execute('INSERT OR IGNORE INTO rules_deferred_ops VALUES(?,?,?)', (ops_chat_id,message_id,row['topic_id']))
                return row['topic_id'],0
            if row['status'] != 'OPEN': return None
            from .store import _aware
            from datetime import datetime
            if self.store.clock() - _aware(datetime.fromisoformat(row['last_at'])) >= timedelta(days=expiry_days):
                self.store.connection.execute("UPDATE rules_topics SET status='EXPIRED' WHERE topic_id=?", (row['topic_id'],))
                return None
            prior = self.store.connection.execute('SELECT topic_id,ref FROM rules_ops_replies WHERE chat_id=? AND message_id=?', (ops_chat_id,message_id)).fetchone()
            if prior:
                return (prior['topic_id'],prior['ref']) if prior['topic_id'] == row['topic_id'] else None
            ref = self.store.connection.execute('SELECT COALESCE(MAX(ref),0)+1 FROM rules_sources WHERE topic_id=?', (row['topic_id'],)).fetchone()[0]
            self.store.connection.execute('INSERT INTO rules_sources VALUES(?,?,?,?)', (row['topic_id'],ref,ops_chat_id,message_id))
            self.store.connection.execute('INSERT INTO rules_ops_replies VALUES(?,?,?,?)', (ops_chat_id,message_id,row['topic_id'],ref))
            self.store.connection.execute('UPDATE rules_topics SET last_at=? WHERE topic_id=?', (self.store.clock().isoformat(),row['topic_id']))
            return row['topic_id'],ref

def local_binding_blockers(connection,tables=None,limit=1000):
    """Bounded local ownership/receipt checks, usable on a read-only old-schema snapshot."""
    tables=tables or {r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    blockers=[]
    def check(code,sql):
        try:
            if connection.execute(sql).fetchone():blockers.append(code)
        except sqlite3.Error:blockers.append('BINDING_SCHEMA_UNKNOWN')
    if 'rules_sources' in tables:
        cross=("AND NOT EXISTS (SELECT 1 FROM rules_ops_replies o WHERE o.topic_id=s.topic_id AND o.ref=s.ref AND o.chat_id=s.chat_id AND o.message_id=s.message_id)" if 'rules_ops_replies' in tables else '')
        check('ORPHAN_RULES_SOURCE',"""SELECT 1 FROM rules_sources s LEFT JOIN rules_topics t ON t.topic_id=s.topic_id
            LEFT JOIN messages m ON m.chat_id=s.chat_id AND m.message_id=s.message_id
            WHERE t.topic_id IS NULL OR m.message_id IS NULL OR typeof(s.ref)!='integer' OR s.ref<=0
            OR (t.source_chat_id!=s.chat_id """+cross+") LIMIT 1")
    for table in ('rules_deferred_ops','rules_ops_replies'):
        if table not in tables:continue
        check('ORPHAN_OPS_BINDING',f'''SELECT 1 FROM {table} d LEFT JOIN rules_topics t ON t.topic_id=d.topic_id
            LEFT JOIN messages m ON m.chat_id=d.chat_id AND m.message_id=d.message_id
            WHERE t.topic_id IS NULL OR m.message_id IS NULL LIMIT 1''')
    if 'rules_deferred_ops' in tables:
        check('MISMATCH_DEFERRED_NOTICE',"""SELECT 1 FROM rules_deferred_ops d JOIN messages m ON m.chat_id=d.chat_id AND m.message_id=d.message_id
            WHERE NOT EXISTS (SELECT 1 FROM rules_notices n WHERE n.topic_id=d.topic_id AND n.ops_chat_id=d.chat_id AND n.notice_id=m.reply_to_id AND n.kind='question' AND n.status='sent') OR
            (SELECT count(*) FROM rules_notices n WHERE n.ops_chat_id=d.chat_id AND n.notice_id=m.reply_to_id AND n.status='sent')!=1 LIMIT 1""")
    if 'rules_notices' in tables:
        check('ORPHAN_RULES_NOTICE',"""SELECT 1 FROM rules_notices n WHERE NOT EXISTS
            (SELECT 1 FROM rules_topics t WHERE t.topic_id=n.topic_id) OR typeof(n.sequence)!='integer' OR n.sequence<=0
            OR typeof(n.ops_chat_id)!='integer' OR n.ops_chat_id=0 OR (n.status='sent' AND (typeof(n.notice_id)!='integer' OR n.notice_id<=0)) LIMIT 1""")
    if 'rules_ops_replies' in tables:
        check('MISMATCH_OPS_SOURCE','''SELECT 1 FROM rules_ops_replies o WHERE NOT EXISTS
            (SELECT 1 FROM rules_sources s WHERE s.topic_id=o.topic_id AND s.ref=o.ref AND s.chat_id=o.chat_id AND s.message_id=o.message_id) LIMIT 1''')
    if 'rules_inflight_topics' in tables:
        check('ORPHAN_INFLIGHT','''SELECT 1 FROM rules_inflight_topics i LEFT JOIN rules_topics t ON t.topic_id=i.topic_id
            WHERE t.topic_id IS NULL OR (i.batch_id='single:'||i.topic_id AND i.ordinal!=0) OR (i.batch_id!='single:'||i.topic_id AND NOT EXISTS
            (SELECT 1 FROM rules_batch_groups g WHERE g.batch_id=i.batch_id AND g.ordinal=i.ordinal)) LIMIT 1''')
    if 'rules_batch_groups' in tables:
        check('ORPHAN_BATCH_GROUP','''SELECT 1 FROM rules_batch_groups g WHERE NOT EXISTS
            (SELECT 1 FROM rules_batches b WHERE b.batch_id=g.batch_id) LIMIT 1''')
    for table,keys in (('rules_readonly_notices',('chat_id','message_id')),('rules_comments',('chat_id','message_id')),
                       ('rules_lifecycle_questions',('chat_id','source_id')),('rules_lifecycle_answers',('chat_id','source_id'))):
        if table in tables:
            check('ORPHAN_SOURCE_LEDGER',f'''SELECT 1 FROM {table} l WHERE NOT EXISTS
                (SELECT 1 FROM messages m WHERE m.chat_id=l.{keys[0]} AND m.message_id=l.{keys[1]}) LIMIT 1''')
    if 'rules_append_completions' in tables:
        check('ORPHAN_APPEND_COMPLETION','''SELECT 1 FROM rules_append_completions a WHERE NOT EXISTS
            (SELECT 1 FROM rules_comments c WHERE c.chat_id=a.chat_id AND c.message_id=a.message_id AND c.status='done')
            OR NOT EXISTS (SELECT 1 FROM rules_topics t WHERE t.topic_id=a.topic_id) LIMIT 1''')
    if 'rules_lifecycle_questions' in tables:
        check('ORPHAN_LIFECYCLE_QUESTION','''SELECT 1 FROM rules_lifecycle_questions q WHERE NOT EXISTS
            (SELECT 1 FROM rules_topics t WHERE t.topic_id=q.topic_id) OR NOT EXISTS
            (SELECT 1 FROM rules_batch_groups g WHERE g.batch_id=q.batch_id AND g.ordinal=q.ordinal) LIMIT 1''')
    if 'rules_lifecycle_answers' in tables:
        q=("EXISTS (SELECT 1 FROM rules_lifecycle_questions q WHERE q.chat_id=a.chat_id AND q.source_id=a.source_id)" if 'rules_lifecycle_questions' in tables else '0')
        e=("EXISTS (SELECT 1 FROM rules_related_episodes e WHERE e.chat_id=a.chat_id AND e.answer_id=a.answer_id)" if 'rules_related_episodes' in tables else '0')
        check('ORPHAN_LIFECYCLE_ANSWER',f'SELECT 1 FROM rules_lifecycle_answers a WHERE NOT ({q} OR {e}) LIMIT 1')
    if 'rules_related_episodes' in tables:
        check('ORPHAN_RELATED_EPISODE','''SELECT 1 FROM rules_related_episodes e WHERE NOT EXISTS
            (SELECT 1 FROM messages m WHERE m.chat_id=e.chat_id AND m.message_id=e.source_id) OR NOT EXISTS
            (SELECT 1 FROM rules_lifecycle_answers a WHERE a.chat_id=e.chat_id AND a.answer_id=e.answer_id) LIMIT 1''')
    for table in ('rules_legacy_transitions','rules_legacy_releases'):
        if table in tables:
            check('ORPHAN_TRANSITION_RELEASE',f'''SELECT 1 FROM {table} l WHERE NOT EXISTS
                (SELECT 1 FROM messages m WHERE m.chat_id=l.chat_id AND m.message_id=l.message_id) OR NOT EXISTS
                (SELECT 1 FROM rules_sources s JOIN rules_topics t ON t.topic_id=s.topic_id
                 WHERE s.chat_id=l.chat_id AND s.message_id=l.message_id) LIMIT 1''')
    if 'rules_legacy_releases' in tables:
        check('ORPHAN_RELEASE','''SELECT 1 FROM rules_legacy_releases r WHERE NOT EXISTS
            (SELECT 1 FROM rules_legacy_transitions t WHERE t.chat_id=r.chat_id AND t.message_id=r.message_id) LIMIT 1''')
    if 'rules_analysis_review_members' in tables:
        check('ORPHAN_ANALYSIS_MEMBER','''SELECT 1 FROM rules_analysis_review_members m WHERE NOT EXISTS
            (SELECT 1 FROM rules_analysis_reviews r WHERE r.review_id=m.review_id) OR NOT EXISTS
            (SELECT 1 FROM messages s WHERE s.chat_id=m.chat_id AND s.message_id=m.message_id) LIMIT 1''')
    def rows(table,columns):
        try:
            json_columns={'plan','receipt','recurrence_keys','source_keys'}
            selected=','.join(f'CASE WHEN length(CAST({column} AS BLOB))<=262144 THEN {column} ELSE NULL END' if column in json_columns else column for column in columns.split(','))
            result=[];used=0
            for index,row in enumerate(connection.execute('SELECT '+selected+' FROM '+table+' LIMIT ?',(limit+1,))):
                used+=sum(len(v.encode('utf8')) for v in row if isinstance(v,str))
                if index>=limit or used>8*1024*1024:blockers.append('BINDING_VALIDATION_TRUNCATED');break
                result.append(row)
            return result
        except sqlite3.Error:
            blockers.append('BINDING_SCHEMA_UNKNOWN');return []
    def decode(raw):
        from .legacy_preflight import _json
        return _json(raw)
    if 'rules_analysis_reviews' in tables:
        from .rules_analysis import load,source_bindings_valid
        for ident,raw in rows('rules_analysis_reviews','review_id,receipt'):
            try:
                value=decode(raw)
                found=connection.execute('SELECT g.plan,b.fingerprint FROM rules_batch_groups g JOIN rules_batches b ON b.batch_id=g.batch_id WHERE g.batch_id=? AND g.ordinal=?',
                    (value['batch_id'],value['ordinal'])).fetchone()
                if not found or load(connection,decode(found[0]),found[1])!=value or not source_bindings_valid(connection,value):raise ValueError()
            except (ValueError,TypeError,KeyError,IndexError,UnicodeError,sqlite3.Error):blockers.append('ANALYSIS_RECEIPT_MISMATCH')
    if 'rules_related_receipts' in tables:
        plan_hashes={}
        if 'rules_batch_groups' in tables:
            for raw, in rows('rules_batch_groups','plan'):
                try:
                    value=decode(raw);sha=hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()
                    plan_hashes[sha]=value
                except (ValueError,TypeError):blockers.append('MALFORMED_BINDING_RECORD')
        for chat,repo,parent,sha,recurrence,source in rows('rules_related_receipts','chat_id,repo,parent_number,plan_hash,recurrence_keys,source_keys'):
            try:
                plan=plan_hashes.get(sha);keys=decode(source);recurring=decode(recurrence)
                episode=connection.execute('SELECT source_id,answer_id FROM rules_related_episodes WHERE chat_id=? AND repo=? COLLATE NOCASE AND parent_number=?',(chat,repo,parent)).fetchone()
                if not plan or keys!=plan['source_keys'] or not recurring or not episode or any(k not in keys for k in recurring):raise ValueError()
            except (ValueError,TypeError,KeyError,sqlite3.Error):blockers.append('RELATED_RECEIPT_MISMATCH')
    blockers.extend(_lifecycle_relation_blockers(connection,tables))
    return sorted(set(blockers))

def _proven_closed_chat_sources(connection):
    """Recognize the existing completed CHAT checkpoint, never arbitrary closed-topic bindings."""
    from .legacy_preflight import _json
    tables={row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not {'rules_batch_groups','rules_batches','rules_topics','rules_sources','rules_decisions'}<=tables:return set()
    recoverable={(batch,ordinal) for batch,ordinal,status in _recoverable_batch_groups(connection) if status=='DONE'}
    proven=set()
    rows=connection.execute("""SELECT g.batch_id,g.ordinal,g.plan,g.status,b.fingerprint FROM rules_batch_groups g
        JOIN rules_batches b ON b.batch_id=g.batch_id WHERE g.status IN ('RESERVED','DONE') LIMIT 1001""").fetchall()
    if len(rows)>1000:raise RulesHalt('CHAT ownership validation incomplete; Owner inspection required')
    for batch,ordinal,raw,status,fingerprint in rows:
        try:
            plan=_json(raw);snapshot=plan.get('pending_snapshot');topic=plan.get('pending_topic')
            if plan.get('state')!='CHAT' or not topic or not isinstance(snapshot,dict):continue
            if status=='RESERVED' and (batch,ordinal) not in recoverable:continue
            current=connection.execute('SELECT status,last_at,asked,fallback_asked FROM rules_topics WHERE topic_id=?',(topic,)).fetchone()
            sources=[list(row) for row in connection.execute('SELECT ref,chat_id,message_id FROM rules_sources WHERE topic_id=? ORDER BY ref',(topic,))]
            if (not current or current[0]!='CLOSED' or snapshot.get('status')!='OPEN'
                    or sources!=snapshot.get('sources') or tuple(current[1:])!=(snapshot.get('last_at'),snapshot.get('asked'),snapshot.get('fallback_asked'))):continue
            if not connection.execute("SELECT 1 FROM rules_decisions WHERE topic_id=? AND state='CHAT' AND fingerprint=? AND decision_rule=?",(topic,fingerprint,plan['rule'])).fetchone():continue
            keys=plan['source_keys'];members={(chat,mid) for _,chat,mid in sources}
            if not keys or any(tuple(key) not in members or not connection.execute("SELECT 1 FROM messages WHERE chat_id=? AND message_id=? AND decision='chat' AND reason=?",(*key,plan['rule'])).fetchone() for key in keys):continue
            for _,chat,mid in sources:
                row=connection.execute('SELECT decision,github_called_at,issue_number,issue_url FROM messages WHERE chat_id=? AND message_id=?',(chat,mid)).fetchone()
                if row and row[0]=='pending_review' and all(value is None for value in row[1:]):proven.add((chat,mid))
        except (ValueError,TypeError,KeyError,sqlite3.Error):continue
    return proven


def _lifecycle_relation_blockers(connection,tables):
    """Question-backed A/B and direct related episodes have different durable authority proofs."""
    from .legacy_preflight import _json
    blockers=[];questions={}
    if 'rules_lifecycle_questions' in tables:
        try:
            rows=connection.execute('''SELECT chat_id,source_id,
                CASE WHEN length(topic_id)<=1000 THEN topic_id END,parent_number,
                CASE WHEN length(repo)<=200 THEN repo END,plan_id,fingerprint,batch_id,ordinal,
                CASE WHEN length(evidence_answer)<=2000 THEN evidence_answer END,
                CASE WHEN length(recurrence_answer)<=2000 THEN recurrence_answer END,
                CASE WHEN length(question)<=2000 THEN question END
                FROM rules_lifecycle_questions LIMIT 1001''').fetchall()
            if len(rows)>1000:blockers.append('LIFECYCLE_VALIDATION_TRUNCATED')
            for row in rows[:1000]:
                chat,source,topic,parent,repo,planid,fp,batch,ordinal,evidence,recurrence,question_text=row
                try:
                    saved=connection.execute('SELECT g.plan,b.fingerprint FROM rules_batch_groups g JOIN rules_batches b ON b.batch_id=g.batch_id WHERE g.batch_id=? AND g.ordinal=?',(batch,ordinal)).fetchone()
                    if not saved:raise ValueError()
                    plan=_json(saved[0]);targets=[p for p in plan.get('targets',[]) if p.get('chat_id')==chat and p.get('reply_message_id')==source]
                    expected_topic=plan.get('pending_topic') or f"{plan['source_keys'][0][0]}:{min(k[1] for k in plan['source_keys'])}"
                    if (plan.get('state') not in ('LIFECYCLE_QUESTION','RELATED') or fp!=saved[1]
                            or planid!=readonly_plan_id(batch,ordinal,fp,plan) or topic!=expected_topic
                            or len(targets)!=1 or targets[0]['number']!=parent
                            or question_text!=plan.get('lifecycle_question') or not isinstance(question_text,str) or not question_text
                            or evidence!=plan.get('lifecycle_evidence_answer') or recurrence!=plan.get('lifecycle_recurrence_answer')
                            or not isinstance(repo,str) or not isinstance(evidence,str) or not evidence or not isinstance(recurrence,str) or not recurrence):raise ValueError()
                    # The plan retains the original Reply target even when the question binds a later episode.
                    # Episode-notice aliases resolve to their durable source, not a synthetic Telegram message.
                    target=targets[0];origin=target.get('recurrence_origin_number',parent)
                    target_source=target.get('target_source_id',target.get('reply_to_id'))
                    original=connection.execute('SELECT issue_number,issue_url FROM messages WHERE chat_id=? AND message_id=?',
                        (chat,target_source)).fetchone()
                    if (not original or original[0]!=origin
                            or original[1]!=f'https://github.com/{repo}/issues/{origin}'):raise ValueError()
                    notice=connection.execute("SELECT kind,status,notice_id,ops_chat_id FROM rules_notices WHERE topic_id=? AND kind='question' AND status='sent'",(topic,)).fetchone()
                    from .ticket_status import sent_notice_identity
                    if notice:
                        from .rules_intake import _ops_notice_identity
                        if not _ops_notice_identity(connection,notice[3],notice[2]):raise ValueError()
                        notice_chat=notice[3]
                    else:
                        notice=connection.execute("SELECT kind,status,notice_message_id FROM group_notices WHERE chat_id=? AND message_id=?",(chat,source)).fetchone()
                        if not notice or notice[0]!='LIFECYCLE_QUESTION' or notice[1]!='sent' or not sent_notice_identity(connection,chat,notice[2]):raise ValueError()
                        notice_chat=chat
                    questions[(chat,source)]=(evidence,recurrence,notice[2],notice_chat)
                except (ValueError,TypeError,KeyError,sqlite3.Error):blockers.append('LIFECYCLE_QUESTION_MISMATCH')
        except sqlite3.Error:blockers.append('BINDING_SCHEMA_UNKNOWN')
    if 'rules_lifecycle_answers' in tables:
        try:
            rows=connection.execute('SELECT chat_id,source_id,answer_id,choice FROM rules_lifecycle_answers LIMIT 1001').fetchall()
            if len(rows)>1000:blockers.append('LIFECYCLE_VALIDATION_TRUNCATED')
            for chat,source,answer,choice in rows[:1000]:
                try:
                    if choice not in ('evidence','recurrence'):raise ValueError()
                    question=questions.get((chat,source))
                    answer_chat=question[3] if question else chat
                    message=connection.execute('''SELECT reply_to_id,is_forward,
                        CASE WHEN length(text)<=2000 THEN text END FROM messages WHERE chat_id=? AND message_id=?''',(answer_chat,answer)).fetchone()
                    if not message:raise ValueError()
                    if question:
                        expected=question[0] if choice=='evidence' else question[1]
                        if message[0]!=question[2] or message[1] or not isinstance(message[2],str) or message[2].strip()!=expected:raise ValueError()
                    else:
                        # No question is valid only for host-grounded direct recurrence, bound by its related receipt.
                        if connection.execute('SELECT 1 FROM rules_lifecycle_questions WHERE chat_id=? AND source_id=?',(chat,source)).fetchone():raise ValueError()
                        episode=connection.execute('''SELECT e.parent_number,r.source_keys,r.recurrence_keys FROM rules_related_episodes e
                            JOIN rules_related_receipts r ON r.chat_id=e.chat_id AND r.repo=e.repo COLLATE NOCASE AND r.parent_number=e.parent_number
                            WHERE e.chat_id=? AND e.source_id=? AND e.answer_id=?''',(chat,answer,answer)).fetchone()
                        if choice!='recurrence' or not episode:raise ValueError()
                        keys=_json(episode[1]);recurring=_json(episode[2])
                        if [chat,source] not in keys or [chat,answer] not in recurring:raise ValueError()
                except (ValueError,TypeError,KeyError,sqlite3.Error):blockers.append('LIFECYCLE_ANSWER_MISMATCH')
        except sqlite3.Error:blockers.append('BINDING_SCHEMA_UNKNOWN')
    return blockers

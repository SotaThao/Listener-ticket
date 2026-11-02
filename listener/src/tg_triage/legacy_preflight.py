"""Read-only, content-free inventory for an operator inspecting an offline copy."""
import sqlite3
import json
from pathlib import Path

from .rules_pending_store import RulesHalt, check_before_recovery


def inspect(path):
    """Count outstanding categories without changing, migrating, or replaying any row."""
    path = Path(path)
    if not path.is_file():
        raise RulesHalt('legacy preflight database does not exist')
    queries = {
        'legacy_messages': ('messages', "SELECT count(*) FROM messages m WHERE m.decision IN ('queued','classifying','in_case','pending','pending_review')"),
        'open_cases': ('cases', "SELECT count(*) FROM cases WHERE status IN ('open','analyzing')"),
        'followup_jobs': ('followup_jobs', "SELECT count(*) FROM followup_jobs WHERE status NOT IN ('done','blocked') OR status IS NULL"),
        'followup_operations': ('followup_operations', "SELECT count(*) FROM followup_operations WHERE status != 'done' OR status IS NULL"),
        'followup_edits': ('followup_analysis_retries', "SELECT count(*) FROM followup_analysis_retries WHERE edit_status != 'done' OR edit_status IS NULL"),
        'confirmations': ('autonomous_confirmations', "SELECT count(*) FROM autonomous_confirmations WHERE status != 'sent' OR status IS NULL"),
        'notices': ('group_notices', "SELECT count(*) FROM group_notices WHERE status != 'sent' OR status IS NULL"),
        'attachments': ('attachments', "SELECT count(*) FROM attachments WHERE status NOT IN ('uploaded','prewrite_error') OR status IS NULL"),
        'rules_comments': ('rules_comments', "SELECT count(*) FROM rules_comments WHERE status IN ('reserved','ambiguous')"),
        'rules_notices': ('rules_notices', "SELECT count(*) FROM rules_notices WHERE status IN ('reserved','ambiguous')"),
        'rules_checkpoints': ('rules_batch_groups', "SELECT count(*) FROM rules_batch_groups WHERE status IN ('RESERVED','AMBIGUOUS')"),
    }
    try:
        connection = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)
        try:
            connection.execute('PRAGMA query_only=ON')
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if 'messages' not in tables:
                raise RulesHalt('legacy preflight cannot recognize the message ledger')
            counts = {}
            for name, (table, query) in queries.items():
                if table not in tables:
                    continue
                if name == 'legacy_messages':
                    if 'rules_sources' in tables:
                        query += ' AND NOT EXISTS (SELECT 1 FROM rules_sources s WHERE s.chat_id=m.chat_id AND s.message_id=m.message_id)'
                    if 'rules_deferred_ops' in tables:
                        query += ' AND NOT EXISTS (SELECT 1 FROM rules_deferred_ops d WHERE d.chat_id=m.chat_id AND d.message_id=m.message_id)'
                counts[name] = connection.execute(query).fetchone()[0]
            message_columns = {row[1] for row in connection.execute('PRAGMA table_info(messages)')}
            uncertain = ("github_called_at IS NOT NULL AND decision IN ('pending','error','queued','classifying','in_case','pending_review')"
                         if 'github_called_at' in message_columns else "decision='error'")
            counts['uncertain_issue_posts'] = connection.execute('SELECT count(*) FROM messages WHERE ' + uncertain).fetchone()[0]
            for table in ('cases','groups'):
                if table not in tables: continue
                columns={row[1] for row in connection.execute('PRAGMA table_info('+table+')')}
                condition=("status='error'" if 'github_called_at' not in columns else
                           'github_called_at IS NOT NULL' if 'issue_number' not in columns else
                           'github_called_at IS NOT NULL AND (issue_number IS NULL OR issue_number<=0)')
                counts['uncertain_'+table+'_posts'] = connection.execute('SELECT count(*) FROM '+table+' WHERE '+condition).fetchone()[0]
            if 'autonomous_plans' in tables:
                count=0
                for (raw,) in connection.execute('SELECT plan FROM autonomous_plans'):
                    try:
                        plan=json.loads(raw)
                        if not isinstance(plan,dict): raise ValueError()
                        if plan.get('action')=='ANALYZE' and 'task' not in plan and plan.get('options'): count+=1
                    except (TypeError,ValueError):
                        count+=1
                counts['legacy_offers_or_unreadable_plans']=count
        finally:
            connection.close()
    except sqlite3.Error:
        raise RulesHalt('cannot inspect legacy inventory safely') from None
    try:
        check_before_recovery(path)
    except RulesHalt:
        gate = 'HOLD'
    else:
        gate = 'CLEAR'
    return {'preflight_only': True, 'migration_performed': False, 'recovery_gate': gate,
            'counts': counts}


def attachment_evidence(path, limit=100):
    """List bounded local attachment evidence; never infer permission to replay."""
    if type(limit) is not int or not 1<=limit<=1000:
        raise ValueError('invalid evidence limit')
    path=Path(path)
    if not path.is_file():
        raise RulesHalt('legacy preflight database does not exist')
    try:
        connection=sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True)
        try:
            connection.execute('PRAGMA query_only=ON')
            tables={r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if 'attachments' not in tables:
                return {'items': [], 'truncated': False, 'migration_performed': False}
            columns={row[1] for row in connection.execute('PRAGMA table_info(attachments)')}
            if not {'id','message_id','status'} <= columns:
                raise RulesHalt('cannot inspect attachment evidence safely')
            chat='chat_id' if 'chat_id' in columns else 'NULL'
            rows=connection.execute('SELECT id,'+chat+',message_id,status FROM attachments '
                "WHERE status != 'uploaded' OR status IS NULL ORDER BY id LIMIT ?",(limit+1,)).fetchall()
            items=[{'attachment_id':ident,'chat_id':chat_id,'message_id':message_id,'status':status,
                    'evidence':'proven_prewrite' if status=='prewrite_error' else 'outcome_unresolved'}
                   for ident,chat_id,message_id,status in rows[:limit]]
            return {'items':items,'truncated':len(rows)>limit,'migration_performed':False}
        finally:
            connection.close()
    except sqlite3.Error:
        raise RulesHalt('cannot inspect attachment evidence safely') from None

"""Read-only ticket status from exact local source proofs, never model claims."""
from datetime import datetime
import sqlite3
from urllib.parse import urlsplit
from .store import _aware


def resolve(store,sources,repo):
    result=dict(state='unavailable',source_ids=[],issues=[])
    if len(sources)!=1:
        return result
    message=sources[0]
    if message.reply_to_id is None:
        return dict(result,state='missing_target')
    try:
        with store._lock:
            notices=store.connection.execute('SELECT status,notice_message_id FROM group_notices WHERE chat_id=? AND notice_message_id=?',
                (message.chat_id,message.reply_to_id)).fetchall()
            if store.connection.execute("SELECT 1 FROM sqlite_master WHERE name='autonomous_confirmations'").fetchone():
                notices += store.connection.execute('SELECT status,notice_message_id FROM autonomous_confirmations WHERE chat_id=? AND notice_message_id=?',(message.chat_id,message.reply_to_id)).fetchall()
            if notices and (len(notices)!=1 or notices[0]['status']!='sent'
                    or store.get(message.chat_id,message.reply_to_id) is not None):return result
            target=store.group_notice_reply_target(message.chat_id,message.reply_to_id)
            row=store.get(message.chat_id,target)
            if row is None or row['message_id']>=message.message_id or _aware(datetime.fromisoformat(row['received_at']))>_aware(message.received_at):
                return result
            if row['group_key']:
                rows=store.connection.execute('SELECT * FROM messages WHERE chat_id=? AND group_key=? ORDER BY message_id',
                    (message.chat_id,row['group_key'])).fetchall()
                group=store.connection.execute('SELECT * FROM groups WHERE chat_id=? AND group_key=?',
                    (message.chat_id,row['group_key'])).fetchone()
            else:
                rows=[row]; group=None
                if row['case_key']:
                    count=store.connection.execute('SELECT COUNT(*) FROM groups WHERE chat_id=? AND case_key=?',
                        (message.chat_id,row['case_key'])).fetchone()[0]
                    if count>1:
                        return result
                    rows=store.connection.execute('SELECT * FROM messages WHERE chat_id=? AND case_key=? AND group_key IS NULL ORDER BY message_id',
                        (message.chat_id,row['case_key'])).fetchall()
            result['source_ids']=[r['message_id'] for r in rows]
            if not rows or any(r['message_id']>=message.message_id for r in rows):
                return result
            records=list(rows)+([group] if group is not None else [])
            plans=[store.autonomous_plan(message.chat_id,old['message_id']) for old in rows]
            plan_repos={p.get('host_repo').casefold() for p in plans if p is not None and isinstance(p.get('host_repo'),str)}
            observed_repo=next(iter(plan_repos)) if len(plan_repos)==1 else repo if not plan_repos else None
            # An incomplete reservation takes precedence over malformed
            # metadata elsewhere: STATUS must surface the uncertain POST. A
            # confirmed old-repository issue is not an uncertain POST merely
            # because the configured repository has changed.
            operation_repos=[p.get('host_repo') if p is not None else observed_repo for p in plans]
            if group is not None: operation_repos.append(observed_repo)
            if any(old['github_called_at'] and not (isinstance(op_repo,str) and type(old['issue_number']) is int
                    and old['issue_number']>0 and _issue_url(old['issue_url'],op_repo,old['issue_number']))
                    for old,op_repo in zip(records,operation_repos)):
                return dict(result,state='ambiguous')
            # Repo binding of a durable plan also applies when it has no ticket yet.
            for plan in plans:
                if plan is not None and (not isinstance(plan.get('host_repo'),str) or plan['host_repo'].casefold()!=repo.casefold() or plan.get('action')=='STATUS'):
                    return result
            issues={}
            for old in records:
                number=old['issue_number']; url=old['issue_url']
                if number is not None or url:
                    if type(number) is not int or number<=0 or not _issue_url(url,repo,number):
                        return result
                    issues[number]=dict(number=number,url=f'https://github.com/{repo}/issues/{number}')
            if len(issues)>1:
                return result
            if issues:
                return dict(result,state='created',issues=list(issues.values()))
            if any(old['github_called_at'] for old in records):
                return dict(result,state='ambiguous')
            if any(r['decision'] in ('queued','classifying','in_case') for r in rows) or group is not None and group['status'] in ('planned','analyzing'):
                return dict(result,state='in_progress')
            return dict(result,state='not_recorded')
    except (ValueError,TypeError,KeyError,sqlite3.Error):
        return result


def render(snapshot):
    state=snapshot['state']
    if state=='created':
        issue=snapshot['issues'][0]
        return f"Em đã ghi nhận ticket #{issue['number']}: {issue['url']}"
    return {
        'missing_target':'Anh/Chị reply tin yêu cầu gốc hoặc tin xác nhận ticket để Em kiểm tra trạng thái nhé.',
        'not_recorded':'Em chưa có ticket được ghi nhận trong sổ theo dõi của bot cho yêu cầu này.',
        'in_progress':'Bot đang xử lý yêu cầu này; hiện chưa có ticket được xác nhận trong sổ theo dõi.',
        'ambiguous':'Bot đã bắt đầu bước ghi ticket nhưng chưa xác nhận được kết quả; cần đối chiếu trước khi tạo lại.',
        'unavailable':'Em chưa xác minh được trạng thái ticket từ nguồn được reply trong sổ theo dõi của bot.'}[state]


def _issue_url(value,repo,number):
    if not isinstance(value,str) or not isinstance(repo,str):return False
    try:
        parsed=urlsplit(value)
        return (parsed.scheme=='https' and parsed.netloc.casefold()=='github.com'
            and not parsed.query and not parsed.fragment
            and parsed.path.rstrip('/').casefold()==f'/{repo}/issues/{number}'.casefold())
    except ValueError:return False


def sent_notice_identity(connection,chat_id,notice_id):
    """One sent host identity across both delivery ledgers, no source-row collision."""
    rows=connection.execute('SELECT status FROM group_notices WHERE chat_id=? AND notice_message_id=?',(chat_id,notice_id)).fetchall()
    if connection.execute("SELECT 1 FROM sqlite_master WHERE name='autonomous_confirmations'").fetchone():
        rows+=connection.execute('SELECT status FROM autonomous_confirmations WHERE chat_id=? AND notice_message_id=?',(chat_id,notice_id)).fetchall()
    return (type(notice_id) is int and notice_id>0 and len(rows)==1 and rows[0][0]=='sent'
        and not connection.execute('SELECT 1 FROM messages WHERE chat_id=? AND message_id=?',(chat_id,notice_id)).fetchone())

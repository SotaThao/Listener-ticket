"""Read-only analysis alternatives and exact-reply selection; never ticket authority."""
import json
import re
from .models import CaseAnalysis, utf8_valid


MISSING=object()


def clean_option(value):
    from .group_notices import clean_analysis
    return ' '.join(clean_analysis(value).split())[:180].strip()


def valid_options(options, action='ANALYZE', needs_choice=MISSING):
    if not isinstance(options, list) or len(options) not in (0, 2, 3):
        return False
    if options and action != 'ANALYZE':
        return False
    if needs_choice is not MISSING and (type(needs_choice) is not bool
            or needs_choice != bool(options) or needs_choice and action!='ANALYZE'):
        return False
    normalized = []
    for value in options:
        if not utf8_valid(value) or not value.strip() or len(value) > 180:
            return False
        clean=clean_option(value)
        if not clean:
            return False
        normalized.append(' '.join(clean.casefold().split()))
    return len(normalized) == len(set(normalized))


def render(answer, options):
    from . import group_notices
    prose = group_notices.analysis_text(answer)
    if not options:
        return prose
    clean = []
    for option in options:
        # Report sanitization without its trusted scope prefix; each choice stays complete.
        text = clean_option(option)
        clean.append(text or 'Giả thuyết chưa rõ')
    suffix = '\nCác khả năng chưa xác nhận:\n' + '\n'.join(f'{i}. {text}' for i, text in enumerate(clean, 1))
    suffix += '\nAnh/Chị reply số (có thể chọn nhiều, ví dụ 1,2); 0 = không lựa chọn nào.'
    return prose[:1500-len(suffix)].rstrip() + suffix


def offer(store, message, repo, *, origin=None):
    """Only a sent notice or its proven source IDs can bind an offer in this chat."""
    if message.reply_to_id is None:
        return None
    with store._lock:
        rows = store.connection.execute('''SELECT DISTINCT p.anchor_id,p.source_ids,p.plan,n.notice_message_id
            FROM autonomous_plans p JOIN group_notices n
            ON n.chat_id=p.chat_id AND n.message_id=p.anchor_id
            WHERE p.chat_id=? AND n.status='sent' AND n.kind='chat'
            AND (n.notice_message_id=? OR EXISTS(SELECT 1 FROM json_each(p.source_ids) WHERE value=?))
            ORDER BY p.anchor_id DESC LIMIT 2''',
            (message.chat_id,message.reply_to_id,message.reply_to_id)).fetchall()
    if len(rows) != 1:
        return None
    row=rows[0]
    try:
        plan=json.loads(row['plan'])
        if (plan.get('action')!='ANALYZE' or 'task' in plan or plan.get('candidate_id') is not None
                or plan.get('host_repo')!=repo or not plan.get('options')
                or not valid_options(plan['options'],needs_choice=plan.get('needs_choice',MISSING)) or row['anchor_id']>=message.message_id
                or type(row['notice_message_id']) is not int or row['notice_message_id']<=0
                or origin is not None and row['anchor_id']!=origin):
            return None
        return dict(anchor=row['anchor_id'],options=plan['options'])
    except (ValueError,TypeError):
        return None


def selection_group(store, message, repo):
    prior=offer(store,message,repo)
    if prior is None:
        return None
    text=' '.join(message.text[:1000].casefold().strip().split())
    if len(message.text)>1000:
        return None
    text=re.sub(r'^(?:chọn|chon|lựa chọn|lua chon|option)\s*:?\s*','',text)
    all_selected=text in ('all','tất cả','tat ca','cả hai','cả 2','cả ba','cả 3')
    count={'cả hai':2,'cả 2':2,'cả ba':3,'cả 3':3}.get(text)
    none_selected=text in ('0','none','không có','khong co','không lựa chọn nào','không có lựa chọn nào')
    numeric=re.fullmatch(r'\d+(?:(?:\s*[,;/+&]\s*|\s+(?:và|va|and)\s+|\s+)\d+)*[.!]?',text)
    if not (all_selected or none_selected or numeric):
        return None  # Ordinary independent work still belongs to semantic intake.
    options=prior['options']
    numbers=list(range(1,len(options)+1)) if all_selected else [int(n) for n in re.findall(r'\d+',text)]
    valid=not none_selected and (count is None or count==len(options)) and bool(numbers) and all(1<=n<=len(options) for n in numbers)
    if valid:
        chosen=sorted(set(numbers))
        answer='Em ghi nhận Anh/Chị chọn cách hiểu: ' + '; '.join(f'{n}. {clean_option(options[n-1])}' for n in chosen)
        answer+='\nĐây là xác nhận cách hiểu; nguyên nhân vẫn cần kiểm chứng.'
        offered=[]
    else:
        answer=('Em ghi nhận chưa có khả năng nào phù hợp; các mục dưới đây vẫn chỉ là giả thuyết để chọn lại.'
                if none_selected else 'Lựa chọn chưa hợp lệ. Anh/Chị chọn số trong các khả năng dưới đây nhé.')
        offered=list(options)
    return dict(message_ids=[message.message_id],action='ANALYZE',candidate_id=None,candidate=None,
        confidence=1,question='',answer=answer,options=offered,needs_choice=bool(offered),
        analysis=CaseAnalysis('needs_human',None,(message.message_id,),category='chat',actionable=False),
        host_selection_origin=prior['anchor'],host_selection_offer=list(options))

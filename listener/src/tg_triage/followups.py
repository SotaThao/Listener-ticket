"""Conversational corrections with committed plans and at-most-once GitHub POSTs."""
import json
import logging
import re
import hashlib
import copy
from .followup_model import validate_followup, MAX_SPLIT
from .issue_format import Issue, format_issue, redact, sanitize, telegram_link
from .models import Classification
from .group_notices import clean_question, address_question
from .ticket_templates import active

LOG=logging.getLogger(__name__)
SCOPE_QUESTION='Em chưa xác định chắc mỗi yêu cầu gồm những tin/ảnh nào. Anh/Chị chỉ rõ tin hoặc ảnh thuộc yêu cầu thứ nhất và thứ hai giúp Em nhé?'
TARGET_QUESTION='Em đang theo dõi nhiều ticket cho nội dung này. Bạn muốn bổ sung cho ticket thứ mấy trong danh sách dưới đây?'


def _safe(text):
    return redact(sanitize(text[:4000]))


def _link(repo, number):
    return f'https://github.com/{repo}/issues/{number}'


def _recurrence_body(body, url):
    return body+'\n\n'+active().render('comments.md','append.recurrence',{'url':url})


def _source_body(body, url):
    return body+'\n\n'+active().render('comments.md','append.source',{'url':url})


def _enrichment_comment(summary, reply, link):
    return active().render('comments.md','enrichment',{'summary':_safe(summary),
        'reply_quote':'> '+_safe(reply).replace('\n','\n> '),'reply_link':link or ''})


def _scope_confirmation(source, ordinal, link, images, documents):
    return active().render('comments.md','scope_confirmation',{'source':_safe(_describe(source)),
        'ordinal':str(ordinal+1),'source_link':link or '', 'image_urls':[url for _,url in images],
        'document_urls':[url for _,url in documents]})


def _scope_correction(first, pending, pending_images, pending_documents):
    return active().render('comments.md','scope_correction',{'title':_safe(first['title']),
        'summary':_safe(first['summary']), 'message_ids':', '.join(str(i) for i in first['message_ids']),
        'questions':[_safe(q) for q in first['missing_info']], 'has_unresolved':'yes' if pending is not None else '',
        'unresolved':[_safe(_describe(m)) for m in pending or []],
        'unresolved_image_urls':[url for _,url in pending_images],
        'unresolved_document_urls':[url for _,url in pending_documents]})


def _completion_text(plan):
    name = 'followup.recurrence' if plan.get('related_episode') else 'followup.split' if plan['action']=='split' else 'followup.updated'
    return active().render('group-replies.md',name)


def _tickets(root):
    return ([root['issue_number']] if type(root['issue_number']) is int and root['issue_number']>0 else []) + json.loads(root['siblings'])


def _edit_retry_notice(pipeline,job,text,retry):
    editor=getattr(pipeline,'notice_editor',None)
    if editor is None or pipeline.config.mode=='dry-run':
        return
    store=pipeline.store
    if not store.reserve_followup_notice_edit(job['id']):
        return
    success=False
    try:
        success=editor(job['chat_id'],retry['notice_message_id'],text[:3500]) is True
    except Exception as error:
        LOG.warning('Followup notice edit unknown exception=%s',type(error).__name__)
    store.finish_followup_notice_edit(job['id'],success)


def _notify(pipeline, job, reply, text):
    if pipeline.config.mode=='dry-run':
        return
    store=pipeline.store
    retry=store.followup_analysis_retry(job['id'])
    if retry is not None:
        _edit_retry_notice(pipeline,job,text,retry)
        return
    if pipeline.notice_sender is None:
        return
    try:
        if not store.reserve_group_notice(reply.chat_id,reply.message_id,'followup'):
            return
        status,notice_id='failed',None
        try:
            sent=pipeline.notice_sender(reply.chat_id,reply.message_id,text[:3500])
            if type(sent) is int and sent>0:
                status,notice_id='sent',sent
        except Exception as error:
            LOG.warning('Followup notice failed exception=%s',type(error).__name__)
        store.finish_group_notice(reply.chat_id,reply.message_id,status,notice_id)
    except Exception as error:
        LOG.warning('Followup notice state failed exception=%s',type(error).__name__)


def _result(pipeline,job,root,reply,status,text,extra=(), *, include_links=True):
    numbers=list(dict.fromkeys(_tickets(root)+list(extra))) if include_links else []
    if numbers:
        text+='\n'+'\n'.join(f'{i+1}. {_link(pipeline.config.gh_repo,n)}' for i,n in enumerate(numbers))
    pipeline.store.finish_followup(job,status,text)
    _notify(pipeline,job,reply,text)


def _clarify(questions):
    questions=[address_question(q) for q in questions[:3]]
    questions=[q for q in questions if q]
    return 'Em cần Anh/Chị xác nhận thêm để xử lý đúng nhé:\n'+'\n'.join('• '+q for q in (questions or [SCOPE_QUESTION]))


def _explicit_label(text):
    text=' '.join((text or '')[:300].lower().split()).rstrip('.!😊 ')
    match=re.fullmatch(r'(?:(?:đây|đó|này) là )?(?:ticket|yêu cầu) ([1-5])(?: nhé)?',text)
    return int(match[1])-1 if match else None


def _explicit_count(text):
    text=' '.join((text or '')[:500].lower().split())
    if re.search(r'(^|\s)(?:không|đừng|chưa)(?:\s|$)',text) or any(ch in text for ch in '"“”\''):
        return None
    counts={int(m[1]) for m in re.finditer(r'(?:cần lên|cần tạo|tạo|lên|thành) ([2-5]) ticket\b',text)}
    return counts.pop() if len(counts)==1 else None


def _apply_context(state, entry, originals):
    """Only an explicit human label on an original reply edge binds a source; never infers tasks."""
    lookup={m.message_id:m for m in originals}
    sender=entry.get('sender_id'); anchor=entry.get('reply_to_id')
    ordinal=_explicit_label(entry.get('text'))
    count=_explicit_count(entry.get('text'))
    owner=state.get('author_id')
    first=originals[0] if originals else None
    if type(sender) is not int or (owner is not None and sender!=owner):
        return False, bool(ordinal is not None or count is not None)
    if count is not None and first is not None and first.sender_id==sender:
        if state.get('desired_count') not in (None,count) or any(v>=count for v in state['bindings'].values()):
            state['conflicts']['count']=entry.get('message_id')
            return False,True
        state['conflicts'].pop('count',None)
        state['desired_count']=count; state['author_id']=sender; state['count_evidence']=entry.get('message_id')
        state['conflicts'].pop('invalid_count',None)
    if ordinal is None:
        return False,False
    source=lookup.get(anchor)
    if source is None or source.sender_id!=sender:
        return False,True
    if state.get('desired_count') is not None and ordinal>=state['desired_count']:
        state['conflicts']['count']=entry.get('message_id')
        return False,True
    key=str(anchor)
    if key in state['bindings'] and state['bindings'][key]!=ordinal:
        state['conflicts'][key]=entry.get('message_id')
        return False,True
    state['conflicts'].pop(key,None)
    state['conflicts'].pop('invalid:'+key,None)
    state['author_id']=sender
    state['bindings'][key]=ordinal
    state['evidence'][key]=entry.get('message_id')
    return True,False


def _context(pipeline,job,root,originals,reply):
    state=json.loads(root.get('context_state') or '{}')
    state.setdefault('bindings',{}); state.setdefault('evidence',{}); state.setdefault('conflicts',{})
    lookup={m.message_id:m for m in originals}
    for key,ordinal in list(state['bindings'].items()):
        source=lookup.get(int(key)) if isinstance(key,str) and key.isdigit() else None
        evidence=pipeline.store.get(root['chat_id'],state['evidence'].get(key))
        valid=(type(ordinal) is int and 0<=ordinal<MAX_SPLIT and source is not None and evidence is not None
               and evidence['decision']=='followup' and evidence['case_key']==root['case_key']
               and evidence['reply_to_id']==source.message_id and evidence['sender_id']==source.sender_id
               and type(source.sender_id) is int and evidence['sender_id']==state.get('author_id')
               and _explicit_label(evidence['text'])==ordinal)
        if not valid:
            state['bindings'].pop(key,None)
            state['conflicts']['invalid:'+key]=True
    if state.get('desired_count') is not None:
        evidence=pipeline.store.get(root['chat_id'],state.get('count_evidence'))
        if (evidence is None or evidence['decision']!='followup' or evidence['case_key']!=root['case_key']
                or evidence['sender_id']!=state.get('author_id') or not originals
                or evidence['sender_id']!=originals[0].sender_id
                or _explicit_count(evidence['text'])!=state['desired_count']):
            state.pop('desired_count',None)
            state['conflicts']['invalid_count']=True
    # Old completed clarification jobs retain their real reply edges, even after the
    # five-entry model history rolls off. Reconstruct at most 100, then persist the result.
    for entry in pipeline.store.followup_binding_history(job):
        if entry['message_id']>state.get('processed_until',0):
            _apply_context(state,entry,originals)
            state['processed_until']=entry['message_id']
    current=dict(message_id=reply.message_id,reply_to_id=reply.reply_to_id,
                 sender_id=reply.sender_id,text=reply.text)
    accepted,conflict=_apply_context(state,current,originals)
    conflict=conflict or bool(state['conflicts'])
    state['processed_until']=max(reply.message_id,state.get('processed_until',0))
    state['unresolved_message_ids']=[m.message_id for m in originals if str(m.message_id) not in state['bindings']]
    pipeline.store.save_followup_context_state(job['root_key'],state)
    root['context_state']=json.dumps(state)
    return state,accepted,conflict


def _describe(item):
    text=clean_question(item.text).replace('“',"'").replace('”',"'")
    if text:
        return '“'+text[:85]+'”'+(' (kèm ảnh)' if item.has_image else '')
    when=item.received_at.strftime('%H:%M')
    sender='“'+clean_question(item.sender_name).replace('“',"'").replace('”',"'")[:40]+'”'
    return f'ảnh không caption của {sender} lúc {when}' if item.has_image else f'tin không có chữ của {sender} lúc {when}'


def _context_ack(state, originals):
    known=[]
    for item in originals:
        ordinal=state['bindings'].get(str(item.message_id))
        if ordinal is not None:
            known.append(f'{_describe(item)} thuộc phạm vi ticket {ordinal+1}')
    return 'Em đã ghi nhận '+ '; '.join(known)+'.' if known else ''


def _context_questions(state, originals):
    remaining=[m for m in originals if str(m.message_id) not in state['bindings']]
    if not state.get('desired_count'):
        return ['Bạn muốn tách thành bao nhiêu ticket tất cả?']
    count=state['desired_count']
    return [f'Tin/ảnh chưa được gán: {_describe(m)} — thuộc ticket nào trong {count} yêu cầu bạn đã xác nhận, hay cần giữ riêng để làm rõ?' for m in remaining[:3]]


def _anchor_split(state, originals):
    """Two or more explicitly confirmed request anchors; retain ALL unassigned content separately."""
    count=state.get('desired_count')
    if not count or any(state.get('author_id')!=m.sender_id for m in originals):
        return None
    groups=[]
    for ordinal in range(count):
        selected=[m for m in originals if state['bindings'].get(str(m.message_id))==ordinal]
        if not selected or not any(m.text.strip() for m in selected):
            return None
        text=next(m.text for m in selected if m.text.strip())
        groups.append(dict(message_ids=[m.message_id for m in selected],title=text[:200],summary=text[:1500],
                           type='task',priority='medium',missing_info=['Nhờ xác nhận chi tiết và kết quả mong muốn của yêu cầu này.']))
    unassigned=[m.message_id for m in originals if str(m.message_id) not in state['bindings']]
    return dict(action='split',confidence=1.0,target_ordinal=None,summary='',questions=[],groups=groups,
                desired_count=count,bindings=[],unresolved_message_ids=unassigned,context_reason='confirmed_anchors')



def _enrichment_sources(pipeline,root,originals):
    """Extend only with durable evidence proven by this trusted conversation, never model IDs."""
    from .store import _to_message
    state=json.loads(root.get('context_state') or '{}')
    snapshot=state.get('managed_snapshot',{})
    ids=list(snapshot.get('source_ids',[]))+list(state.get('lifecycle_evidence_ids',[]))+[int(i) for i in state.get('media_evidence',{}) if str(i).isdigit()]
    ids += [row[0] for row in pipeline.store.connection.execute('''SELECT s.message_id FROM followup_job_sources s
        JOIN followup_jobs j ON j.id=s.job_id WHERE j.root_key=? ORDER BY j.id DESC,s.message_id DESC LIMIT 30''',(root['root_key'],))]
    result=list(originals); present={m.message_id for m in result}
    for mid in ids:
        if type(mid) is not int or mid in present: continue
        episode=pipeline.store.connection.execute('SELECT root_key FROM followup_episode_sources WHERE chat_id=? AND message_id=?',
                                                  (root['chat_id'],mid)).fetchone()
        if episode is not None and episode[0]!=root['root_key']:
            continue  # New recurrence media is not evidence for the old incident.
        row=pipeline.store.get(root['chat_id'],mid)
        if (row is None or row['decision']!='followup' or row['case_key']!=root['case_key']
                or row['group_key']!=root['group_key'] or row['issue_number']!=root['issue_number']):
            raise ValueError('Unproven enrichment source')
        result.append(_to_message(row)); present.add(mid)
    if len(result)>39:
        raise ValueError('Enrichment context exceeds source bound')
    return sorted(result,key=lambda m:(m.received_at,m.message_id))


def _bounded_attachment_processor(pipeline):
    """Clone only this call's uploader runner; each production subprocess uses remaining time."""
    from .thinking import CURRENT, BudgetExhausted
    turn=CURRENT.get()
    if turn and turn.is_set(): raise BudgetExhausted('decision_deadline_exhausted')
    processor=copy.copy(pipeline.attachments)
    uploader=processor.uploader
    if turn and uploader is not None and hasattr(uploader,'_runner'):
        processor.uploader=copy.copy(uploader)
        original_runner=uploader._runner
        def bounded_runner(*args,**kwargs):
            if turn.is_set(): raise OSError('Artifact deadline exhausted')
            kwargs['timeout']=min(kwargs.get('timeout',turn.remaining()),turn.remaining())
            return original_runner(*args,**kwargs)
        processor.uploader._runner=bounded_runner
    return processor


def _materialize_update(pipeline,job,root,originals,reply,op,payload):
    """Semantic plan is already durable; recover proven assets or perform one ledgered upload."""
    store=pipeline.store
    images,documents=[],[]
    note=None
    for incoming in store.followup_job_messages(job):
        if op['kind']=='related_create' and incoming.message_id not in payload['source_ids']:
            continue  # Previous-success evidence remains context, not new-episode attachments.
        if not incoming.has_attachment:
            continue
        current_images,current_documents=[],[]
        rows=store.followup_attachment_rows(root,incoming.message_id)
        if rows:
            current_images,current_documents=store.followup_media(root,[incoming],pipeline.config.gh_repo)
        else:
            found=_bounded_attachment_processor(pipeline).process(root['group_key'] or root['case_key'],[incoming],live=True,
                                               prepared=getattr(pipeline,'_followup_prepared_images',None),
                                               document_reads=payload.get('host_document_reads',()))
            current_images,current_documents=found.images,found.documents
        if not current_images and not current_documents:
            note='Em chưa xác nhận được ảnh/tài liệu đã tải lên; nhờ Anh/Chị giữ lại bản gốc và hỗ trợ kiểm tra.'
        images.extend(current_images);documents.extend(current_documents)
        store.record_followup_evidence(job['root_key'],incoming.message_id,current_images,current_documents,bool(current_images or current_documents))
    payload['materialized']=True
    payload['media_note']=note
    if op['kind']=='comment':
        payload['body']+=''.join('\nẢnh bổ sung: '+url for _,url in images)
        payload['body']+=''.join('\nTài liệu bổ sung: '+url for _,url in documents)
        if note: payload['body']+='\n'+note
    elif op['kind']=='related_create':
        from .store import _to_message
        sources=[_to_message(store.get(reply.chat_id,mid)) for mid in payload['source_ids']]
        analysis=payload['analysis']
        old_images,old_documents=store.followup_media(root,sources,pipeline.config.gh_repo)
        all_images=list(dict.fromkeys(tuple(x) for x in old_images+list(images)))
        all_documents=list(dict.fromkeys(tuple(x) for x in old_documents+list(documents)))
        c=Classification(analysis['type'],analysis['confidence'],analysis['title'],analysis['summary'],analysis['priority'])
        issue=format_issue(sources[0],c,'model',False,pipeline.config.ollama.model,case_messages=sources,
            max_quoted=len(sources),key_message_ids=analysis['key_message_ids'],details=analysis['details'],
            images=all_images,documents=all_documents,clarify=analysis['missing_info'],notes=[note] if note else [])
        body=_recurrence_body(issue.body,_link(pipeline.config.gh_repo,payload['parent_number']))
        snapshot=dict(repo=pipeline.config.gh_repo,title=issue.title,body_sha=hashlib.sha256(body.encode()).hexdigest(),
            source_ids=payload['source_ids'],analysis=analysis,images=all_images,documents=all_documents,owned_labels=list(issue.labels))
        payload.update(title=issue.title,body=body,labels=list(issue.labels),new_snapshot=snapshot)
    else:
        state=json.loads(store.connection.execute('SELECT context_state FROM followup_roots WHERE root_key=?',
                                                (root['root_key'],)).fetchone()[0])
        snapshot=state.get('managed_snapshot')
        if (snapshot is None or snapshot.get('body_sha')!=payload['expected_sha']
                or snapshot.get('title')!=payload['expected_title'] or snapshot.get('issue_number')!=payload['number']
                or payload.get('expected_labels') is None or json.loads(root['siblings']) or root.get('split_plan')):
            raise ValueError('Managed snapshot changed before refresh')
        sources=_enrichment_sources(pipeline,root,originals)
        if reply.message_id not in {m.message_id for m in sources}: sources.append(reply)
        analysis=payload['analysis']
        old_details=(snapshot.get('analysis') or {}).get('details',{})
        analysis['details']={**old_details,**analysis.get('details',{})}
        all_images=list(snapshot.get('images',[])); all_documents=list(snapshot.get('documents',[]))
        proven_ids={m.message_id for m in sources}
        for mid,evidence in state.get('media_evidence',{}).items():
            if str(mid).isdigit() and int(mid) in proven_ids and evidence.get('available'):
                for entry in evidence.get('images',[]):
                    if entry not in all_images: all_images.append(entry)
                for entry in evidence.get('documents',[]):
                    if entry not in all_documents: all_documents.append(entry)
        for entry in images:
            if entry not in all_images and list(entry) not in all_images: all_images.append(entry)
        for entry in documents:
            if entry not in all_documents and list(entry) not in all_documents: all_documents.append(entry)
        c=Classification(analysis['type'],analysis['confidence'],analysis['title'],analysis['summary'],analysis['priority'])
        issue=format_issue(sources[0],c,'model',False,pipeline.config.ollama.model,case_messages=sources,
                           max_quoted=len(sources),key_message_ids=analysis['key_message_ids'],details=analysis['details'],
                           images=all_images,documents=all_documents,notes=[note] if note else [],clarify=analysis['missing_info'])
        new_snapshot=dict(issue_number=payload['number'],repo=pipeline.config.gh_repo,title=issue.title,
                          body_sha=hashlib.sha256(issue.body.encode()).hexdigest(),analysis=analysis,
                          source_ids=[m.message_id for m in sources],images=all_images,documents=all_documents,owned_labels=list(issue.labels))
        payload.update(title=issue.title,body=issue.body,labels=list(issue.labels),new_snapshot=new_snapshot,
                       marker=f'<!-- tg:{sources[0].chat_id}:{sources[0].message_id} -->')
    store.materialize_followup_operation(job,op,payload)
    return payload


def _plan(pipeline,job,root,originals,reply,history):
    tickets=_tickets(root)
    incoming=pipeline.store.followup_job_messages(job)
    from . import document_reader
    current_documents=[m for m in incoming if m.has_document]
    document_contents=[]
    if current_documents:
        cached=getattr(pipeline,'_current_document_context',None)
        available={r['message_id']:r for r in cached[1]} if cached and cached[0]==reply.chat_id else {}
        missing=[m for m in current_documents if m.message_id not in available]
        available.update({r['message_id']:r for r in document_reader.read_documents(missing,pipeline.attachments.downloader)})
        document_contents=[available[m.message_id] for m in current_documents]
        from .document_processing import prepare
        document_contents=prepare(document_contents,pipeline.classifier)
        pipeline._followup_document_reads=document_reader.proof(document_contents)
        if any(document_reader.text_intent(m) for m in current_documents) and not document_reader.valid_proof(pipeline._followup_document_reads,incoming):
            return dict(action='clarify',questions=[],technical_notice=document_reader.failure_text(document_contents),context_reason='document_read_failed'),[]
    if not tickets:
        return dict(action='clarify',questions=['Em chưa xác nhận được ticket gốc. Nhờ bạn hoặc Owner kiểm tra kết quả trên GitHub trước khi tạo lại nhé.']),[]
    lifecycle_state=None
    if getattr(pipeline.config,'autonomous_enabled',False):
        try:
            lifecycle_state=pipeline.github.lifecycle_snapshot(tickets[0])
            if (lifecycle_state.get('number')!=tickets[0] or lifecycle_state.get('repo','').casefold()!=pipeline.config.gh_repo.casefold()
                    or lifecycle_state.get('state') not in ('open','closed') or not lifecycle_state.get('observed_at')):
                raise ValueError('Invalid trusted lifecycle snapshot')
        except Exception:
            return dict(action='clarify',questions=['Em chưa kiểm tra được trạng thái ticket gốc. Anh/Chị giữ lại thông tin để Em đối chiếu nhé.'],context_reason='lifecycle_lookup_failed'),[]
    state,accepted,conflict=_context(pipeline,job,root,originals,reply)
    ack=_context_ack(state,originals)
    if conflict:
        return dict(action='clarify',ack=ack,context_reason='conflicting_binding',
                    questions=['Em đang giữ các phạm vi đã xác nhận. Bạn xác nhận lại ticket muốn gán cho tin vừa reply, hoặc số ticket cần tách giúp Em nhé?']),[]
    if accepted and len(tickets)>1:
        ordinal=state['bindings'][str(reply.reply_to_id)]
        if ordinal>=len(tickets):
            return dict(action='clarify',ack=ack,questions=[TARGET_QUESTION]),[]
        if lifecycle_state and ordinal!=0:
            try:
                target_state=pipeline.github.lifecycle_snapshot(tickets[ordinal])
                if (target_state.get('number')!=tickets[ordinal] or target_state.get('repo','').casefold()!=pipeline.config.gh_repo.casefold()
                        or target_state.get('state') not in ('open','closed') or not target_state.get('observed_at')):
                    raise ValueError('Invalid bound lifecycle target')
            except Exception:
                return dict(action='clarify',questions=[TARGET_QUESTION],context_reason='lifecycle_target_unobserved'),[]
        selected=[m for m in originals if m.message_id==reply.reply_to_id]
        images,documents=pipeline.store.followup_media(root,selected,pipeline.config.gh_repo)
        body=_scope_confirmation(selected[0],ordinal,telegram_link(selected[0]),images,documents)
        return dict(action='update',summary='Xác nhận phạm vi',questions=[],groups=[],context_reason='confirmed_binding'),[('comment',dict(number=tickets[ordinal],body=body))]
    anchor_plan=_anchor_split(state,originals) if (accepted or _explicit_count(reply.text) is not None) else None
    if accepted and anchor_plan is None:
        return dict(action='clarify',ack=ack,context_reason='partial_binding',
                    questions=_context_questions(state,originals)),[]
    unassigned_text=[m for m in originals if m.message_id in (anchor_plan or {}).get('unresolved_message_ids',[]) and m.text.strip()]
    ask=getattr(pipeline.classifier,'analyze_followup',None)
    raw=None
    analysis_sources=originals
    try:
        # Scope corrections use the original text and media provenance. No pixel re-read:
        # this keeps the configured provider/consent path and never reuploads attachments.
        scopes=json.loads(root['split_plan']).get('groups',[]) if root.get('split_plan') and len(tickets)>1 else []
        mapping=[dict(ordinal=i,issue_number=n,
                      message_ids=scopes[i]['message_ids'] if i<len(scopes) else [m.message_id for m in originals],
                      scope=scopes[i]['summary'] if i<len(scopes) else '') for i,n in enumerate(tickets)]
        if mapping:
            mapping[0]['confirmed_context']=state
            if lifecycle_state:
                mapping[0]['lifecycle_state']=lifecycle_state
        if anchor_plan is not None:
            if not unassigned_text:
                raw=anchor_plan
            else:
                role_ask=getattr(pipeline.classifier,'analyze_source_roles',None)
                anchors=[dict(ordinal=ordinal,message_id=m.message_id,text=m.text)
                         for m in originals for ordinal in [state['bindings'].get(str(m.message_id))]
                         if ordinal is not None and m.text.strip()]
                response=role_ask(anchors,unassigned_text) if role_ask else None
                raw=(dict(action='clarify',confidence=1.0,target_ordinal=None,summary='',questions=[],groups=[],
                          source_roles=response.get('source_roles')) if isinstance(response,dict) else None)
        else:
            analysis_sources=_enrichment_sources(pipeline,root,originals)
            if lifecycle_state and lifecycle_state['state']=='closed':
                from .store import _to_message
                present={m.message_id for m in analysis_sources}
                for item in history:
                    row=pipeline.store.get(reply.chat_id,item['message_id'])
                    if row is not None and row['message_id'] not in present and row['issue_number']==tickets[0]:
                        analysis_sources.append(_to_message(row)); present.add(row['message_id'])
            options={}
            if document_contents:options['document_contents']=document_contents
            pixel_sources=[m for m in incoming if m.has_image]
            if pixel_sources:
                if not getattr(pipeline.classifier,'supports_vision',False):
                    return dict(action='clarify',questions=['Em chưa đọc được ảnh mới theo quyền/công cụ hiện có. Anh/Chị ghi lại thông báo lỗi trong ảnh giúp Em nhé.'],context_reason='vision_unavailable'),[]
                prepared=pipeline.attachments.prepare_images(root['group_key'] or root['case_key'],pixel_sources)
                pipeline._followup_prepared_images=prepared
                options['images']=prepared.for_messages(pixel_sources)
                if any(m.message_id not in options['images'] for m in pixel_sources):
                    return dict(action='clarify',questions=['Em chưa tải/đọc được ảnh mới. Anh/Chị gửi lại hoặc ghi thông báo lỗi giúp Em nhé.'],context_reason='vision_unavailable'),[]
            raw=ask(analysis_sources,reply,mapping,history,**options) if ask else None
        value=validate_followup(raw)
    except Exception as error:
        LOG.warning('Followup classifier failed exception=%s',type(error).__name__)
        value=None
    if (document_contents and value is not None and value.get('case_analysis') is not None
            and not document_reader.valid_task_details(value['case_analysis'])):
        value=None
    status=(getattr(pipeline.classifier,'last_followup_outcome','provider_unavailable') if raw is None else 'invalid_output') if value is None else ('uncertain' if value['confidence']<pipeline.config.ollama.confidence_threshold else value['action'])
    LOG.info('Followup analysis job_id=%s status=%s confirmed_bindings=%s',job['id'],status,len(state['bindings']))
    if value is None and (document_contents or status in ('context_budget_exhausted','document_context_budget_exhausted',
            'document_processing_failed','decision_deadline_exhausted','model_call_budget_exhausted')):
        return dict(action='clarify',ack=ack,questions=[],context_reason=status,
                    technical_notice='Em đã lưu nội dung và các xác nhận trước. Lượt phân tích đang lỗi nên chưa cập nhật được ticket.'),[]
    expected_ids={m.message_id for m in originals}
    roles={r['message_id']:r for r in (value or {}).get('source_roles',[])}
    role_ids=expected_ids
    if value is not None and value['action']=='update':
        role_ids={m.message_id for m in analysis_sources}|{reply.message_id}
    valid_roles=(len(roles)==len((value or {}).get('source_roles',[])) and set(roles)<=role_ids
                 and (anchor_plan is None or not unassigned_text or set(roles)=={m.message_id for m in unassigned_text}))
    if value is not None and not valid_roles:
        return dict(action='clarify',ack=ack,questions=_context_questions(state,originals) or [SCOPE_QUESTION],
                    context_reason='invalid_source_roles'),[]
    role_clear=valid_roles and all(m.message_id in roles and roles[m.message_id]['role']=='ancillary'
        and roles[m.message_id]['confidence']>=pipeline.config.ollama.confidence_threshold for m in unassigned_text)
    if anchor_plan is not None and any(roles.get(m.message_id,{}).get('role') in ('request','unknown') for m in unassigned_text):
        return dict(action='clarify',ack=ack,questions=_context_questions(state,originals),context_reason='contradictory_source_role'),[]
    if (anchor_plan is not None and len(tickets)==1 and value is not None
            and value['confidence']>=pipeline.config.ollama.confidence_threshold
            and (not unassigned_text or role_clear)
            and not any(roles.get(m.message_id,{}).get('role')=='request' for m in unassigned_text)):
        # Additional text needs a semantic ancillary verdict in this same model request.
        # Never suppress a third request or use sender/adjacency as a role decision.
        value=anchor_plan
        value['source_roles']=list(roles.values())
    elif value is None or value['confidence']<pipeline.config.ollama.confidence_threshold:
        questions=(value.get('questions') if value else None) or _context_questions(state,originals) or [SCOPE_QUESTION]
        if value is None:
            questions=['Em chưa nhận được phân tích hợp lệ lúc này; các xác nhận trước vẫn được giữ.']+questions[:2]
        return dict(action='clarify',ack=ack,questions=questions,context_reason=status),[]
    if anchor_plan is not None and unassigned_text and value.get('context_reason')!='confirmed_anchors':
        model_full=(value['action']=='split' and not value.get('unresolved_message_ids') and
                    {i for g in value['groups'] for i in g['message_ids']}==expected_ids)
        if not model_full:
            return dict(action='clarify',ack=ack,questions=_context_questions(state,originals),context_reason='unresolved_role'),[]
    if value.get('desired_count') is not None and value['desired_count']!=state.get('desired_count'):
        return dict(action='clarify',ack=ack,questions=['Bạn xác nhận tổng số ticket muốn tách giúp Em nhé?'],context_reason='unproven_count'),[]
    for proposed in value.get('bindings',[]):
        if (not accepted or proposed['ordinal']!=_explicit_label(reply.text)
                or proposed['message_ids']!=[reply.reply_to_id]):
            return dict(action='clarify',ack=ack,questions=['Bạn reply trực tiếp vào tin yêu cầu và xác nhận ticket tương ứng giúp Em nhé?'],context_reason='unproven_binding'),[]
    action=value['action']
    if action=='split' and len(incoming)>1:
        return dict(action='clarify',questions=[SCOPE_QUESTION],context_reason='batch_scope_correction_unclear'),[]
    if action in ('chat','clarify'):
        value['ack']=ack
        if action=='clarify' and state['bindings']:
            value['questions']=_context_questions(state,originals) or value['questions']
        return value,[]
    if action=='update':
        target=value['target_ordinal']
        if target is None and len(tickets)==1:
            target=0
        if target is None or target>=len(tickets):
            return dict(action='clarify',questions=[TARGET_QUESTION]),[]
        if lifecycle_state and target!=0:
            return dict(action='clarify',questions=[TARGET_QUESTION],context_reason='lifecycle_target_unobserved'),[]
        if lifecycle_state and lifecycle_state['state']=='closed':
            from .lifecycle import closed_decision,QUESTION
            disposition=closed_decision(value,analysis_sources,reply,{m.message_id for m in incoming})
            value['lifecycle_state']=lifecycle_state
            if disposition=='unclear':
                return dict(action='clarify',questions=[QUESTION],context_reason='closed_episode_unclear'),[]
            if disposition=='recurrence':
                analysis=value['case_analysis']
                if analysis['confidence']<pipeline.config.ollama.confidence_threshold or len(tickets)!=1:
                    return dict(action='clarify',questions=[QUESTION],context_reason='recurrence_unproven'),[]
                latest=pipeline.store.latest_related_episode(root['root_key'])
                working=value['lifecycle']['knownworking_source_ids']
                if latest is not None and min(working)<=latest['anchor_message_id']:
                    return dict(action='clarify',questions=[f'Em đã ghi nhận lần lỗi tái diễn ở ticket #{latest["issue_number"]}. Anh/Chị đang bổ sung cho lần đó, hay đã xác nhận chạy tốt rồi mới lỗi thêm lần nữa?'],
                        existing_related=latest['issue_number'],context_reason='existing_recurrence_episode'),[]
                value['related_episode']=True
                return value,[('related_create',dict(parent_number=tickets[0],analysis=analysis,
                    source_ids=sorted(value['lifecycle']['recurrence_source_ids']),knownworking_source_ids=working,
                    lifecycle_state=lifecycle_state,materialized=False))]
            known=value['lifecycle']['knownworking_source_ids']
            allowed={m.message_id for m in analysis_sources+[reply]}
            if not set(known)<=allowed:
                return dict(action='clarify',questions=[QUESTION],context_reason='lifecycle_foreign_sources'),[]
            # Persist source evidence beyond the short conversational history, not a model fact.
            state['lifecycle_evidence_ids']=list(dict.fromkeys(state.get('lifecycle_evidence_ids',[])+known+[reply.message_id]))[-30:]
            pipeline.store.save_followup_context_state(job['root_key'],state)
            root['context_state']=json.dumps(state)
        body=_enrichment_comment(value['summary'],reply.text,telegram_link(reply))
        analysis=value.get('case_analysis')
        sources=_enrichment_sources(pipeline,root,originals)
        if reply.message_id not in {m.message_id for m in sources}:sources.append(reply)
        allowed={m.message_id for m in sources}
        if analysis is not None and (set(analysis['key_message_ids'])-allowed or analysis['confidence']<pipeline.config.ollama.confidence_threshold):
            return dict(action='clarify',questions=['Anh/Chị xác nhận lại phạm vi và thông tin cần bổ sung giúp Em nhé.'],context_reason='invalid_enrichment_sources'),[]
        snapshot=state.get('managed_snapshot')
        if analysis is not None and snapshot is not None and not (lifecycle_state and lifecycle_state['state']=='closed'):
            if len(tickets)!=1 or root.get('split_plan') is not None or snapshot.get('issue_number')!=tickets[0] or str(snapshot.get('repo','')).casefold()!=pipeline.config.gh_repo.casefold():
                return dict(action='clarify',questions=[TARGET_QUESTION],context_reason='ambiguous_enrichment'),[]
            return value,[('refresh',dict(number=tickets[0],analysis=analysis,reply_id=reply.message_id,
                   expected_sha=snapshot['body_sha'],expected_title=snapshot['title'],expected_labels=snapshot.get('owned_labels'),materialized=False))]
        if any(m.has_attachment for m in incoming):
            if len(tickets)!=1:
                return dict(action='clarify',questions=[TARGET_QUESTION],context_reason='ambiguous_enrichment'),[]
            return value,[('comment',dict(number=tickets[target],body=body,reply_id=reply.message_id,materialized=False))]
        return value,[('comment',dict(number=tickets[target],body=body))]
    ids=[m.message_id for m in originals]
    assigned=[i for g in value['groups'] for i in g['message_ids']]
    unresolved=value.get('unresolved_message_ids',[])
    accounted=assigned+unresolved
    positions={i:ordinal for ordinal,g in enumerate(value['groups']) for i in g['message_ids']}
    binding_mismatch=any(positions.get(int(i))!=ordinal for i,ordinal in state['bindings'].items())
    count_mismatch=state.get('desired_count') is not None and len(value['groups'])!=state['desired_count']
    unproven_bucket=bool(unresolved) and (not state.get('desired_count') or any(
        not any(state['bindings'].get(str(i))==ordinal for i in group['message_ids'])
        for ordinal,group in enumerate(value['groups'])))
    unresolved_text=[m for m in originals if m.message_id in unresolved and m.text.strip()]
    invalid_bucket_roles=bool(unresolved_text) and (not valid_roles or any(
        m.message_id not in roles or roles[m.message_id]['role']!='ancillary' or
        roles[m.message_id]['confidence']<pipeline.config.ollama.confidence_threshold for m in unresolved_text))
    lookup={m.message_id:m for m in originals}
    unproven_media=any(m.has_image and not m.text.strip() and str(m.message_id) not in state['bindings']
        and m.message_id not in unresolved and not (m.reply_to_id in positions
            and positions.get(m.message_id)==positions[m.reply_to_id]
            and lookup[m.reply_to_id].sender_id==m.sender_id)
        for m in originals) if state['bindings'] else False
    if (not ids or len(accounted)!=len(ids) or set(accounted)!=set(ids)
            or len(set(accounted))!=len(accounted) or binding_mismatch or count_mismatch or unproven_bucket or invalid_bucket_roles or unproven_media or ids[0] not in value['groups'][0]['message_ids']
            or len(value['groups'])>MAX_SPLIT):
        return dict(action='clarify',ack=ack,questions=_context_questions(state,originals) or [SCOPE_QUESTION],context_reason='invalid_accounting'),[]
    if len(tickets)>1 or pipeline.store.followup_split_attempted(job['root_key'],job['id']):
        return dict(action='clarify',questions=['Em đã ghi nhận lần tách trước. Bạn muốn chỉnh phạm vi của ticket thứ mấy? Em cần xác nhận trước khi tạo thêm.']),[]
    operations=[]
    for group in value['groups'][1:]:
        selected=[m for m in originals if m.message_id in group['message_ids']]
        c=Classification(group['type'],value['confidence'],group['title'],group['summary'],group['priority'])
        images,documents=pipeline.store.followup_media(root,selected,pipeline.config.gh_repo)
        issue=format_issue(selected[0],None if value.get('context_reason')=='confirmed_anchors' else c,'model',bool(group['missing_info']),pipeline.config.ollama.model,
                           case_messages=selected,max_quoted=len(selected),images=images,documents=documents,
                           clarify=group['missing_info'],text=(c.title,c.summary),details={})
        body=_source_body(issue.body,_link(pipeline.config.gh_repo,tickets[0]))
        operations.append(('create',dict(title=issue.title,body=body,labels=list(issue.labels))))
    first=value['groups'][0]
    pending=None; pending_images=[]; pending_documents=[]
    if unresolved:
        pending=[m for m in originals if m.message_id in unresolved]
        pending_images,pending_documents=pipeline.store.followup_media(root,pending,pipeline.config.gh_repo)
        value['questions']=_context_questions(state,originals)
    scope=_scope_correction(first,pending,pending_images,pending_documents)
    operations.append(('correction',dict(number=tickets[0],body=scope)))
    return value,operations


def _run(pipeline,job):
    from .thinking import decision_turn
    with decision_turn(pipeline,'followup:'+str(job['id']),job['root_key'],job['message_id']) as turn:
        try:
            _run_inner(pipeline,job)
        finally:
            prepared=getattr(pipeline,'_followup_prepared_images',None)
            if prepared is not None:
                prepared.close()
                pipeline._followup_prepared_images=None
        if turn:
            with pipeline.store._lock:
                current = pipeline.store.connection.execute('SELECT status FROM followup_jobs WHERE id=?',(job['id'],)).fetchone()
                root = pipeline.store.connection.execute('SELECT context_state FROM followup_roots WHERE root_key=?',(job['root_key'],)).fetchone()
                operations = pipeline.store.followup_operations(job)
            state = json.loads(root[0]) if root else {}
            turn.record('followup_result',_confirmed_evidence(state),state.get('unresolved_message_ids',[]),
                        state.get('conflicts'),operations=[dict(ordinal=o['ordinal'],kind=o['kind'],status=o['status'],result=o['result']) for o in operations],
                        status=current[0] if current else 'missing')


def _confirmed_evidence(state):
    return {k:state[k] for k in ('bindings','evidence','desired_count','count_evidence','author_id') if k in state}


def _run_inner(pipeline,job):
    from .thinking import CURRENT
    turn = CURRENT.get()
    if turn:
        turn.lookup()
    store=pipeline.store
    root,originals,reply,history=store.followup_context(job)
    # Recovery is not permission: old persisted autonomous jobs may have been
    # selected without a Telegram reply. Read original stored edges, never the
    # synthetic reply target in followup_context or this job's assigned membership.
    members=store.followup_job_messages(job)
    member_ids={m.message_id for m in members}
    authorized=False
    for actual in members:
        if actual.reply_to_id is None or actual.reply_to_id in member_ids:
            continue
        target=store.followup_source(actual)
        if target is not None and target['root_key']==job['root_key']:
            authorized=True
            break
    if not authorized:
        if turn:
            turn.reason='reply_authority_required'
        _result(pipeline,job,root,reply,'blocked',
                'Để bổ sung nội dung cũ, Anh/Chị reply đúng tin gốc hoặc tin xác nhận ticket giúp Em nhé.',
                include_links=False)
        return
    # A stored URL is only proof that the original issue belongs to the CURRENT configured
    # repo. It never selects an API destination. Check before model calls and planned resumes.
    source=store.get(root['chat_id'],root['source_message_id'])
    number=root['issue_number']
    expected=_link(pipeline.config.gh_repo,number)
    source_url=source['issue_url'] if source is not None else None
    if (type(number) is int and number>0
            and (source is None or source['issue_number']!=number or not isinstance(source_url,str)
                 or source_url.casefold()!=expected.casefold())):
        _result(pipeline,job,root,reply,'blocked',
                'Em cần Owner kiểm tra/xác nhận lại repo và ticket gốc trước khi xử lý phản hồi này nhé. Em chưa chỉnh hoặc tạo thêm ticket.',
                include_links=False)
        return
    if job['status']=='ambiguous':
        _result(pipeline,job,root,reply,'blocked','Em chưa xác nhận được một thao tác GitHub đã hoàn tất hay chưa. Nhờ Owner kiểm tra trước khi thử lại; Em giữ các ticket đã xác nhận bên dưới nhé.')
        return
    with store._lock:
        earlier=store.connection.execute("SELECT 1 FROM followup_jobs WHERE root_key=? AND id<? AND status IN ('queued','planned') LIMIT 1", (job['root_key'],job['id'])).fetchone()
    if earlier:
        return
    # A previous interrupted/unknown POST blocks further writes for this conversation.
    with store._lock:
        prior=store.connection.execute("""SELECT 1 FROM followup_jobs j JOIN followup_operations o ON o.job_id=j.id
        WHERE j.root_key=? AND j.id<? AND o.status IN ('reserved','ambiguous') LIMIT 1""",(job['root_key'],job['id'])).fetchone()
    if prior:
        plan,ops=dict(action='clarify',questions=['Em cần Owner kiểm tra thao tác GitHub chưa rõ kết quả trước đó trước khi chỉnh tiếp nhé.']),[]
    elif job['plan'] is None:
        pipeline._followup_document_reads=[]
        plan,ops=_plan(pipeline,job,root,originals,reply,history)
    else:
        plan,ops=json.loads(job['plan']),None
    if job['plan'] is None:
        if any(m.has_document for m in members):
            reads=getattr(pipeline,'_followup_document_reads',[])
            plan=dict(plan,host_document_reads=reads)
            ops=[(kind,dict(payload,host_document_reads=reads)) for kind,payload in ops]
        if turn:
            state=json.loads(root.get('context_state') or '{}')
            turn.reason=turn.reason or plan.get('context_reason')
            turn.record(plan['action'],_confirmed_evidence(state),state.get('unresolved_message_ids',[]),state.get('conflicts'),
                        hypotheses=[dict(message_id=r['message_id'],role=r['role'],confidence=r['confidence']) for r in plan.get('source_roles',[])],
                        operations=[dict(ordinal=i,kind=k) for i,(k,_) in enumerate(ops)])
        store.save_followup_plan(job,plan,ops)
    if plan['action'] not in ('clarify','chat'):
        from . import document_reader
        if (any(document_reader.text_intent(m) for m in members) and plan.get('case_analysis') is not None
                and not document_reader.valid_task_details(plan['case_analysis'])):
            _result(pipeline,job,root,reply,'blocked','Em đã lưu tài liệu nhưng phân tích chưa có công việc và tiêu chí hợp lệ nên chưa cập nhật ticket.',include_links=False)
            return
        if any(document_reader.text_intent(m) for m in members) and not document_reader.valid_proof(plan.get('host_document_reads'),members):
            _result(pipeline,job,root,reply,'blocked','Em chưa có bằng chứng đã đọc đủ tài liệu của lượt này nên chưa cập nhật ticket.',include_links=False)
            return
    if pipeline.config.mode=='dry-run':
        store.finish_followup(job,'dryrun','Simulated '+plan['action'])
        return
    if plan['action'] in ('clarify','chat'):
        text=(plan.get('technical_notice') or _clarify(plan.get('questions',[]))) if plan['action']=='clarify' else 'Em đã nhận phản hồi của Anh/Chị rồi nhé 😊'
        if plan.get('ack'):
            text=plan['ack']+'\n'+text
        _result(pipeline,job,root,reply,'done',text,(plan['existing_related'],) if plan.get('existing_related') else ())
        return
    completed=[]
    for op in store.followup_operations(job):
        if op['status']=='done':
            if op['kind'] in ('create','related_create'):
                completed.append(op['result'])
            continue
        if op['status']!='planned':
            _result(pipeline,job,root,reply,'blocked','Em chưa xác nhận được kết quả thao tác GitHub. Nhờ Owner kiểm tra trước khi thử lại nhé.',completed)
            return
        payload=json.loads(op['payload'])
        if op['kind']=='related_create':
            latest=store.latest_related_episode(root['root_key'])
            if latest is not None and (not payload.get('knownworking_source_ids') or min(payload['knownworking_source_ids'])<=latest['anchor_message_id']):
                _result(pipeline,job,root,reply,'blocked',
                    'Em đã có ticket cho lần lỗi này. Anh/Chị xác nhận giúp Em đây là bổ sung cho lần đó hay một lần mới sau khi đã chạy tốt nhé.',(latest['issue_number'],))
                return
            # Recheck even after a delayed planned resume; never create using stale closure.
            try:
                observed=pipeline.github.lifecycle_snapshot(payload['parent_number'])
                expected=payload['lifecycle_state']
                if (observed.get('number')!=payload['parent_number'] or observed.get('repo','').casefold()!=pipeline.config.gh_repo.casefold()
                        or observed.get('state')!='closed' or not observed.get('observed_at')
                        or observed.get('updated_at')!=expected.get('updated_at')):
                    raise ValueError('Parent lifecycle changed')
            except Exception:
                _result(pipeline,job,root,reply,'blocked','Em cần đối chiếu lại trạng thái ticket cũ trước khi tạo ticket cho lần tái diễn. Anh/Chị xác nhận giúp Em nhé.')
                return
        if payload.get('materialized') is False:
            try:
                payload=_materialize_update(pipeline,job,root,originals,reply,op,payload)
                op['payload']=json.dumps(payload,ensure_ascii=False)
            except Exception as error:
                LOG.warning('Followup enrichment preparation failed exception=%s',type(error).__name__)
                _result(pipeline,job,root,reply,'blocked','Em chưa hoàn tất việc bổ sung ảnh/thông tin. Anh/Chị hỗ trợ kiểm tra trước khi thử lại nhé.')
                return
        if not store.reserve_followup_operation(job,op['ordinal'],pipeline.config.max_issues_per_hour):
            _result(pipeline,job,root,reply,'blocked','Em đã chạm giới hạn tạo ticket nên chưa hoàn tất việc tách. Các ticket đã xác nhận được giữ lại bên dưới; nhờ Owner hỗ trợ phần còn lại nhé.',completed)
            return
        payload=json.loads(op['payload'])
        number=None
        try:
            if op['kind']=='refresh':
                number=pipeline.github.refresh_issue(payload['number'],payload['expected_sha'],payload['expected_title'],
                                                     payload['marker'],Issue(payload['title'],payload['body'],tuple(payload['labels'])),
                                                     expected_labels=payload['expected_labels'])
                if type(number) is not int or number!=payload['number']:
                    raise ValueError('Unverified managed refresh result')
                store.finish_followup_operation(job,op,number)
            elif op['kind'] in ('create','related_create'):
                rendered=Issue(payload['title'],payload['body'],tuple(payload['labels']))
                number,_=pipeline.github.create_issue(rendered)
                if type(number) is not int or number<=0:
                    raise ValueError('Invalid created issue number')
                completed.append(number)
                if op['kind']=='related_create':
                    verified=pipeline.github.verify_created_issue(number,rendered)
                    if type(verified) is not int or verified!=number:
                        raise ValueError('Unverified related issue result')
                    op['readback_verified']=True
                store.finish_followup_operation(job,op,number)
            else:
                body=payload['body']
                if op['kind']=='correction':
                    body+='\n\nCác yêu cầu còn lại được theo dõi riêng:\n'+'\n'.join(_link(pipeline.config.gh_repo,n) for n in completed)
                number=pipeline.github.add_comment(payload['number'],body)
                if type(number) is not int or number<=0:
                    raise ValueError('Invalid comment number')
                store.finish_followup_operation(job,op,number)
        except Exception as error:
            from .github_issues import GitHubConflict,GitHubNotSent
            if isinstance(error,(GitHubConflict,GitHubNotSent)):
                with store._lock,store.connection:
                    store.connection.execute("UPDATE followup_operations SET status=? WHERE job_id=? AND ordinal=? AND status='reserved'",
                                             ('not_sent' if isinstance(error,GitHubNotSent) else 'conflict',job['id'],op['ordinal']))
                _result(pipeline,job,root,reply,'blocked',
                        'Em chưa kiểm tra được phiên bản hiện tại của ticket nên chưa gửi thay đổi; nội dung được giữ nguyên. Nhờ Anh/Chị hỗ trợ kiểm tra nhé.' if isinstance(error,GitHubNotSent) else
                        'Em giữ nguyên nội dung ticket vì có chỉnh sửa ngoài bản Em đang quản lý. Nhờ Anh/Chị xác nhận cách gộp thông tin mới nhé.')
                return
            LOG.warning('Followup operation outcome unknown exception=%s',type(error).__name__)
            if type(number) is int and number>0:
                store.remember_followup_result(job,op,number)
            with store._lock, store.connection:
                store.connection.execute("UPDATE followup_operations SET status='ambiguous' WHERE job_id=? AND ordinal=? AND status='reserved'",(job['id'],op['ordinal']))
            _result(pipeline,job,root,reply,'blocked','Em chưa xác nhận được một thao tác GitHub đã hoàn tất hay chưa. Nhờ Owner kiểm tra trước khi thử lại; các ticket đã xác nhận được giữ lại bên dưới nhé.',completed)
            return
    text=_completion_text(plan)
    questions=plan.get('questions',[])+[q for g in plan.get('groups',[]) for q in g['missing_info']]
    questions += [json.loads(o['payload'])['media_note'] for o in store.followup_operations(job) if json.loads(o['payload']).get('media_note')]
    if questions:
        text+='\n'+_clarify(questions)
    _result(pipeline,job,root,reply,'done',text,completed)


def sweep(pipeline,exclude_chat_ids=()):
    """Called by intake and periodic case sweep, including after restarts with no new messages."""
    from .pacing import BUSY_CHATS
    exclude_chat_ids=set(exclude_chat_ids)|set(BUSY_CHATS.get())
    # All pipeline instances sharing this Store serialize roots, quota and HTTP reservations.
    with pipeline.store._followup_execution_lock:
        for job in pipeline.store.pending_followups():
            if job['chat_id'] in exclude_chat_ids:
                continue
            try:
                _run(pipeline,job)
            except Exception as error:
                LOG.error('Followup processing failed job_id=%s exception=%s',job['id'],type(error).__name__)
        for job in pipeline.store.pending_followup_notice_edits():
            if job['chat_id'] in exclude_chat_ids:
                continue
            try:
                _edit_retry_notice(pipeline,job,job['result'],pipeline.store.followup_analysis_retry(job['id']))
            except Exception as error:
                LOG.warning('Followup pending edit failed exception=%s',type(error).__name__)

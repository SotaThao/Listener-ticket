"""Event-driven semantic intake. SQL proofs and canonical renderer own all side effects."""
from dataclasses import replace
from . import group_notices, analysis_choices, analysis_task, ticket_status, document_reader
from .models import CaseAnalysis, Decision, TopicGroup
from .thinking import CURRENT, decision_turn

def _has_reply_authority(group,sources,candidate):
    """Only this group's actual reply may authorize this historical target."""
    if not candidate or not candidate.get('exact_reply'):
        return False
    bound=set(candidate.get('reply_source_ids',()))
    own=set(group.get('message_ids',()))
    return any(m.message_id in own and m.message_id in bound and m.reply_to_id is not None for m in sources)


def _validate(groups,sources,candidates,threshold,*,read_context=None):
    if not isinstance(groups,list) or not groups:
        return None
    from .store import _aware
    read_context=list(read_context or [])
    if not sources or any(m.chat_id!=sources[0].chat_id or m.message_id>=min(s.message_id for s in sources)
            or _aware(m.received_at)>min(_aware(s.received_at) for s in sources) for m in read_context):
        return None
    expected={m.message_id for m in sources}; seen=set(); chosen={c['candidate_id']:c for c in candidates}
    for group in groups:
        if not isinstance(group,dict):
            return None
        ids=group.get('message_ids',[])
        if not ids or any(type(i) is not int or i not in expected or i in seen for i in ids):
            return None
        seen.update(ids)
        action=group.get('action')
        if not analysis_task.valid_payload(group):
            return None
        candidate=chosen.get(group.get('candidate_id'))
        if group.get('candidate_id') is not None and candidate is None:
            return None
        if action=='CONTINUE' and (candidate is None or group['confidence']<threshold):
            return None
        if candidate is not None and not _has_reply_authority(group,sources,candidate):
            return None
        if action=='NEW' and candidate is not None:
            return None
        analysis=group.get('analysis')
        allowed=set(ids)|({m.message_id for m in candidate['messages']} if candidate else set())
        if action=='ANALYZE':
            allowed|={m.message_id for m in read_context}
            for historical in candidates:
                if _has_reply_authority(group,sources,historical):
                    allowed|={m.message_id for m in historical['messages']}
        if analysis is None or any(i not in allowed for i in analysis.key_message_ids):
            return None
        if action=='NEW' and (analysis.verdict!='ticket' or analysis.actionable is not True
                or analysis.classification is None or analysis.classification.confidence<threshold
                or group['confidence']<threshold or analysis.missing_info):
            group['action']='CLARIFY'
        if action not in ('NEW','CONTINUE','CLARIFY','CHAT','ANALYZE','STATUS'):
            return None
        group['candidate']=candidate
    return groups if seen==expected else None


def _pending(pipeline,messages,candidate,question,reason,*,notice=True,technical=False):
    key,group,all_sources=pipeline.store.autonomy_case(messages,candidate if candidate and candidate['pending'] else None)
    first=min(all_sources,key=lambda m:m.message_id)
    result=Decision('pending_review','model',case_key=key,reason=reason)
    pipeline.store.finish(first,result)
    pipeline.store.close_case(key,result,0)
    if not question and candidate and candidate['pending']:
        prior=pipeline.store.autonomous_clarification_context(key)
        question=prior[0]['question'] if prior else ''
    _clarify(pipeline,key,messages[0],question,notice=notice,technical=technical)
    return result


def _clarify(pipeline,case_key,source,question,*,notice=True,technical=False):
    question=group_notices.address_question(question or group_notices.question('fallback'))
    if not notice or pipeline.config.mode=='dry-run' or pipeline.notice_sender is None:
        return
    try:
        if technical:
            pipeline.store.reserve_autonomous_outage(source)  # Chat outage telemetry never suppresses a new batch notice.
        if not pipeline.store.reserve_autonomous_clarification(case_key,source,question):
            return
    except Exception:
        return  # Durable reservation unavailable: fail closed before any send.
    group_notices.notify(pipeline.store,pipeline.notice_sender,source,
        Decision('needs_human','model',case_key=case_key),(question,),pipeline.config.gh_repo,
        text_override=question if technical else 'Em cần Anh/Chị làm rõ thêm một chút nhé: '+question)


def process(pipeline,message,start):
    """Process durable same-chat backlog together; semantic groups decide membership."""
    cached=pipeline.store.autonomous_plan(message.chat_id,message.message_id)
    if cached and cached.get('host_repo')!=pipeline.config.gh_repo:
        return _quarantine(pipeline,message,'cached_repo_changed')
    sources=[message]
    if cached:
        from .store import _to_message
        sources=[]
        for mid in cached['message_ids']:
            row=pipeline.store.get(message.chat_id,mid)
            if row is None:
                return _quarantine(pipeline,message,'cached_sources_missing')
            old=_to_message(row)
            if row['decision']=='queued':
                pipeline.store.start_classifying(old)
            sources.append(old)
    for queued in pipeline.store.queued_messages():
        from .pacing import RELEASED_SOURCES
        released=RELEASED_SOURCES.get()
        if (cached is None and queued.chat_id==message.chat_id and queued.message_id!=message.message_id and len(sources)<20
                and (released is None or (queued.chat_id,queued.message_id) in released)):
            if pipeline.store.start_classifying(queued):
                sources.append(queued)
    sources.sort(key=lambda m:m.message_id)
    key=f'{message.chat_id}:{message.message_id}'
    with decision_turn(pipeline,'ingress:'+key,key,message.message_id):
        host_groups=[] if cached else [g for source in sources
            if (g:=analysis_choices.selection_group(pipeline.store,source,pipeline.config.gh_repo)) is not None]
        selection_ids={i for g in host_groups for i in g['message_ids']}
        routing_sources=[m for m in sources if m.message_id not in selection_ids]
        candidates=[]
        ordered=[m for m in routing_sources if m.reply_to_id is not None][:6]
        for source in ordered:
            found=pipeline.store.autonomy_candidates(source,pipeline.config.gh_repo)
            for candidate in found:
                if not candidate['exact_reply']:
                    continue  # No implicit historical target, even for matching text.
                existing=next((c for c in candidates if c['source']['root_key']==candidate['source']['root_key']),None)
                if existing:
                    if candidate['exact_reply']:
                        existing.setdefault('reply_source_ids',[]).append(source.message_id)
                        existing['exact_reply']=True
                    continue
                if len(candidates)>=6 or sum(len(c['messages']) for c in candidates)+len(candidate['messages'])>20:
                    continue
                candidate['reply_source_ids']=[source.message_id] if candidate['exact_reply'] else []
                candidates.append(candidate)
        for i,candidate in enumerate(candidates,1):
            candidate['candidate_id']=f'c{i}'
        read_context=pipeline.store.autonomous_read_context(routing_sources) if routing_sources else []
        turn=CURRENT.get()
        if turn:
            turn.record('ingress_candidates',dict(source_ids=[m.message_id for m in sources],
                candidates=[dict(candidate_id=c['candidate_id'],case_key=c['source']['case_key'],
                    exact_reply=c['exact_reply'],pending=c['pending']) for c in candidates]))
        # Current sources take vision slots first, then exact-reply history;
        # read_context fills any remaining slots.
        vision_sources={m.message_id:m for m in routing_sources}
        for candidate in candidates:
            if candidate['exact_reply']:
                for old in candidate['messages']:
                    vision_sources.setdefault(old.message_id,old)
        for old in read_context:
            vision_sources.setdefault(old.message_id,old)
        document_rows=[] if cached else document_reader.read_documents(routing_sources,pipeline.attachments.downloader)
        if document_rows:
            from .document_processing import prepare
            document_rows=prepare(document_rows,pipeline.classifier)
        pipeline._current_document_context=(message.chat_id,document_rows)
        try:
            if routing_sources and any(m.has_image for m in vision_sources.values()) and getattr(pipeline.classifier,'supports_vision',False):
                pipeline._prepared_images=pipeline.attachments.prepare_images(key,list(vision_sources.values()))
            ask=getattr(pipeline.classifier,'analyze_ingress',None)
            if cached:
                if cached.get('action')=='NEW' and any(document_reader.text_intent(m) for m in sources) and not (document_reader.valid_proof(cached.get('host_document_reads'),sources) and document_reader.valid_task_details(cached['analysis'])):
                    return _quarantine(pipeline,message,'cached_document_invalid')
                if cached.get('action')=='STATUS' and not analysis_task.valid_payload(cached):
                    return _quarantine(pipeline,message,'cached_analysis_invalid')
                if cached.get('action')=='ANALYZE' and not _valid_cached_analysis(pipeline,cached,sources):
                    return _quarantine(pipeline,message,'cached_analysis_invalid')
                groups=[cached]
                candidate=cached.get('candidate')
                if candidate is not None and not _has_reply_authority(cached,sources,candidate):
                    return _quarantine(pipeline,message,'cached_reply_authority_revoked')
                if candidate and not candidate['pending']:
                    proof=candidate['source']; stored=pipeline.store.get(message.chat_id,proof['source_message_id'])
                    if (proof['chat_id']!=message.chat_id or stored is None
                            or stored['issue_url']!=f"https://github.com/{pipeline.config.gh_repo}/issues/{proof['issue_number']}"):
                        raise ValueError('Cached ingress target proof changed')
            else:
                doc_options={'document_contents':document_rows} if document_rows else {}
                document_failure=any(r.get('read_status')=='processing_failed' for r in document_rows)
                if document_failure:pipeline.classifier.last_ingress_outcome='document_processing_failed'
                answer=ask(routing_sources,candidates,read_context=read_context,**doc_options,**pipeline._vision_options(list(vision_sources.values()))) if ask and routing_sources and not document_failure else None
                groups=_validate(answer,routing_sources,candidates,pipeline.config.ollama.confidence_threshold,read_context=read_context) if routing_sources else []
            if groups is None:
                # No guessed membership on provider/schema failure, even adjacent messages.
                reason=getattr(pipeline.classifier,'last_ingress_outcome','invalid_ingress_output')
                if reason=='ok':
                    reason='invalid_ingress_output'  # Parsed JSON is not host-authorized source accounting.
                groups=[dict(message_ids=[source.message_id],action='CLARIFY',candidate=None,candidate_id=None,
                    confidence=0,question='',analysis=CaseAnalysis('needs_human',None,(source.message_id,)),
                    host_reason=reason,host_batch_notice_anchor=routing_sources[0].message_id) for source in routing_sources]
            if not cached:
                groups+=host_groups
            if not cached and not any('host_batch_notice_anchor' in group for group in groups):
                pipeline.store.close_autonomous_outage(message.chat_id)
            if not cached:
                for group in groups:
                    own=[m for m in sources if m.message_id in group['message_ids']]
                    rows=[r for r in document_rows if r['message_id'] in group['message_ids']]
                    group.pop('host_document_reads',None)
                    if rows and group['action']!='STATUS':
                        group['host_document_reads']=document_reader.proof(rows)
                    text_rows=[r for r in rows if any(m.message_id==r['message_id'] and document_reader.text_intent(m) for m in own)]
                    if text_rows and group['action']!='STATUS' and not document_reader.valid_proof(document_reader.proof(rows),own):
                        group.update(action='CLARIFY',candidate=None,candidate_id=None,host_reason='document_read_failed',question=document_reader.failure_text(text_rows))
                    if group['action']=='NEW' and text_rows and not document_reader.valid_task_details(group['analysis']):
                        group.update(action='CLARIFY',candidate=None,candidate_id=None,host_reason='document_task_incomplete',
                            question='Em đã đọc tài liệu nhưng kết quả phân tích chưa có việc cần làm và tiêu chí hoàn thành đầy đủ. Anh/Chị reply thử phân tích lại nhé.')
                    if group.get('host_reason')=='document_context_budget_exhausted':
                        group['question']='Nội dung tài liệu vượt giới hạn 48 KiB sau mã hóa để phân tích đầy đủ. Anh/Chị chia nhỏ tài liệu rồi gửi lại nhé.'
                    if group['action']=='NEW' and pipeline._vision_unavailable([m for m in sources if m.message_id in group['message_ids']]) and not pipeline._unseen_tolerated([m for m in sources if m.message_id in group['message_ids']]):
                        group['action']='CLARIFY'; group['host_reason']='ingress_pixels_unavailable'
                    if group['action']=='NEW':
                        related=[c for c in candidates if not c['pending'] and set(c['reply_source_ids'])&set(group['message_ids'])]
                        if len(related)==1:
                            group['host_related_issue']=related[0]['source']['issue_number']
                pipeline.store.save_autonomous_plans(sources,groups,pipeline.config.gh_repo)
            by_id={m.message_id:m for m in sources}; results=[]
            for group in groups:
                own=[by_id[i] for i in group['message_ids']]
                own.sort(key=lambda m:m.message_id)
                candidate=group['candidate']; action=group['action']; analysis=group['analysis']
                target=candidate['source']['case_key'] if candidate else f'{own[0].chat_id}:{own[0].message_id}'
                reason=group.get('host_reason','semantic_'+action.lower())
                pipeline.store.autonomy_route(own,action,candidate,target,reason)
                if action in ('CONTINUE','CLARIFY') and candidate and not candidate['pending']:
                    from .followups import sweep
                    job_id=pipeline.store.enqueue_followup_batch(own,candidate['source'])
                    if action=='CLARIFY':
                        job=dict(pipeline.store.connection.execute('SELECT * FROM followup_jobs WHERE id=?',(job_id,)).fetchone())
                        if job['status']=='queued':
                            pipeline.store.save_followup_plan(job,dict(action='clarify',questions=[
                                group['question'] or next(iter(analysis.missing_info),group_notices.question('fallback'))],
                                context_reason='ingress_scope_clarification'),[])
                    sweep(pipeline)
                    results.append(Decision('followup','model',case_key=target,issue_number=candidate['source']['issue_number']))
                    continue
                if action=='CLARIFY' or action=='CONTINUE' and (analysis.verdict!='ticket' or not analysis.actionable):
                    batch_anchor=group.get('host_batch_notice_anchor')
                    question=(group['question'] if group.get('host_reason') in ('document_context_budget_exhausted','document_read_failed','document_task_incomplete') else group_notices.question('technical_context') if batch_anchor is not None else group['question'] or next(iter(analysis.missing_info),''))
                    results.append(_pending(pipeline,own,candidate,question,reason,
                        notice=batch_anchor is None or own[0].message_id==batch_anchor,technical=batch_anchor is not None))
                    continue
                case_key,group_key,members=pipeline.store.autonomy_case(own,candidate if action=='CONTINUE' else None)
                first=min(members,key=lambda m:m.message_id)
                if action=='STATUS':
                    results.append(_status(pipeline,case_key,first,own))
                    continue
                if action=='ANALYZE':
                    results.append(_analyze(pipeline,case_key,first,group.get('answer',''),group.get('options',[]),group.get('task')))
                    continue
                if action=='CHAT':
                    from .pipeline import Plan
                    plan=Plan(group_key,members,first,'model','chat',reason='semantic_chat')
                else:
                    value=analysis.classification
                    model_group=TopicGroup('',tuple(m.message_id for m in members),analysis.verdict,
                        type=value.type if value else None,category=analysis.category,
                        title=value.title if value else '',summary=value.summary if value else '',
                        priority=value.priority if value else None,confidence=value.confidence if value else None,
                        missing_info=analysis.missing_info,actionable=analysis.actionable,
                        reason=analysis.reason,details=analysis.details)
                    plan=pipeline._safety(pipeline._plan_group({m.message_id:m for m in members},model_group,{}))
                    plan=replace(plan,key=group_key,key_ids=analysis.key_message_ids)
                    plan=replace(plan,related_issue_number=group.get('host_related_issue'),document_reads=tuple(group.get('host_document_reads',())))
                result=pipeline._run(plan,case_key)
                if candidate and candidate['pending'] and result.issue_number and pipeline.notice_sender is not None:
                    if pipeline.store.reserve_autonomous_confirmation(first,result.issue_number):
                        notice_id=None
                        try:
                            notice_id=pipeline.notice_sender(first.chat_id,first.message_id,
                                group_notices.notice_text(result,plan.clarify,pipeline.config.gh_repo))
                        except Exception:
                            pass  # Reserved delivery is ambiguous; never blindly replay a send.
                        pipeline.store.finish_autonomous_confirmation(first,result.issue_number,notice_id)
                pipeline.store.close_case(case_key,result,max(0,int((pipeline.timer()-start)*1000)))
                results.append(result)
            if turn:
                turn.record('ingress_result',dict(source_ids=[m.message_id for m in sources],
                    results=[dict(decision=r.decision,issue_number=r.issue_number) for r in results]),
                    status=('outcome_unverified' if any(r.decision in ('error','rate_limited') for r in results)
                            else 'routing_recorded' if any(r.decision=='followup' for r in results) else 'accounting_verified'))
            return pipeline._combine(results[0].case_key if len(results)==1 else key,results)
        finally:
            if pipeline._prepared_images is not None:
                pipeline._prepared_images.close()
                pipeline._prepared_images=None


def _valid_cached_analysis(pipeline,cached,sources):
    if cached.get('candidate') is not None or not analysis_task.valid_payload(cached):
        return False
    origin=cached.get('host_selection_origin')
    if origin is None:
        return True
    for source in sources:
        proof=analysis_choices.offer(pipeline.store,source,pipeline.config.gh_repo,origin=origin)
        if proof is None or proof['options']!=cached.get('host_selection_offer'):
            return False
    return True


def _status(pipeline,case_key,first,sources):
    text=ticket_status.render(ticket_status.resolve(pipeline.store,sources,pipeline.config.gh_repo))
    result=Decision('chat','model',case_key=case_key,reason='semantic_status')
    pipeline.store.finish(first,result)
    pipeline.store.close_case(case_key,result,0)
    if pipeline.config.mode!='dry-run':
        group_notices.notify(pipeline.store,pipeline.notice_sender,first,result,(),pipeline.config.gh_repo,text_override=text)
    return result


def _analyze(pipeline,case_key,first,answer,options=(),task=None):
    result=Decision('chat','model',case_key=case_key,reason='semantic_analyze')
    pipeline.store.finish(first,result)
    pipeline.store.close_case(case_key,result,0)
    if pipeline.config.mode!='dry-run':
        group_notices.notify(pipeline.store,pipeline.notice_sender,first,result,(),pipeline.config.gh_repo,
            text_override=analysis_task.render(task) if task is not None else analysis_choices.render(answer,options))
    return result


def resume_case(pipeline,case_key):
    """Resume cached canonical draft after a crash; never repeat an ambiguous GitHub POST."""
    from .pipeline import Plan
    case=pipeline.store.get_case(case_key)
    cached=pipeline.store.autonomous_plan(case['chat_id'],case_key=case_key)
    if cached is None:
        return pipeline._analyze_case(case_key,pipeline.timer())  # legacy intake drained on activation
    if cached.get('host_repo')!=pipeline.config.gh_repo:
        return _quarantine(pipeline,pipeline.store.case_messages(case_key)[0],'cached_repo_changed',case_key)
    if not pipeline.store.begin_analysis(case_key):
        return None
    members=pipeline.store.case_messages(case_key)
    if cached.get('candidate') is not None and not _has_reply_authority(cached,members,cached['candidate']):
        return _quarantine(pipeline,members[0],'cached_reply_authority_revoked',case_key)
    first=min(members,key=lambda m:m.message_id); group_key=f'{first.chat_id}:{first.message_id}'
    pipeline.store.plan_groups(case_key,[(group_key,[m.message_id for m in members])])
    action=cached['action']; analysis=cached['analysis']
    if action=='NEW' and any(document_reader.text_intent(m) for m in members) and not (document_reader.valid_proof(cached.get('host_document_reads'),members) and document_reader.valid_task_details(cached['analysis'])):
        return _quarantine(pipeline,first,'cached_document_invalid',case_key)
    if action=='STATUS':
        if not analysis_task.valid_payload(cached):
            return _quarantine(pipeline,first,'cached_analysis_invalid',case_key)
        return _status(pipeline,case_key,first,members)
    if action=='ANALYZE':
        if not _valid_cached_analysis(pipeline,cached,members):
            return _quarantine(pipeline,first,'cached_analysis_invalid',case_key)
        return _analyze(pipeline,case_key,first,cached.get('answer',''),cached.get('options',[]),cached.get('task'))
    if action=='CLARIFY' or action=='CONTINUE' and not analysis.actionable:
        plan=Plan(group_key,members,first,'model','pending_review',reason='semantic_clarify')
    elif action=='CHAT':
        plan=Plan(group_key,members,first,'model','chat',reason='semantic_chat')
    else:
        value=analysis.classification
        model_group=TopicGroup('',tuple(m.message_id for m in members),analysis.verdict,
            type=value.type if value else None,category=analysis.category,
            title=value.title if value else '',summary=value.summary if value else '',
            priority=value.priority if value else None,confidence=value.confidence if value else None,
            missing_info=analysis.missing_info,actionable=analysis.actionable,reason=analysis.reason,details=analysis.details)
        plan=pipeline._safety(pipeline._plan_group({m.message_id:m for m in members},model_group,{}))
        plan=replace(plan,key=group_key,key_ids=analysis.key_message_ids)
        plan=replace(plan,related_issue_number=cached.get('host_related_issue'),document_reads=tuple(cached.get('host_document_reads',())))
    result=pipeline._run(plan,case_key)
    pipeline.store.close_case(case_key,result,0)
    if result.decision=='pending_review' and pipeline.config.mode!='dry-run':
        batch_anchor=cached.get('host_batch_notice_anchor')
        _clarify(pipeline,case_key,first,cached['question'] if cached.get('host_reason') in ('document_context_budget_exhausted','document_read_failed','document_task_incomplete') else group_notices.question('technical_context') if batch_anchor is not None else cached['question'] or group_notices.question('fallback'),
                 notice=batch_anchor is None or first.message_id==batch_anchor,technical=batch_anchor is not None)
    return result


def _quarantine(pipeline,message,reason,case_key=None):
    """Configuration changes revoke cached tool authority; retain evidence and ask visibly."""
    if case_key:
        pipeline.store.fail_case(case_key,reason)
    else:
        pipeline.store.fail_message(message,reason)
    result=Decision('error','none',case_key=case_key,error=reason,reason=reason)
    if pipeline.config.mode!='dry-run':
        group_notices.notify(pipeline.store,pipeline.notice_sender,message,
            Decision('needs_human','none',case_key=case_key),
            (('Để bổ sung nội dung cũ, Anh/Chị reply đúng tin gốc hoặc tin xác nhận ticket giúp Em nhé.'
              if reason=='cached_reply_authority_revoked' else
              'Em chưa xác minh được bản tài liệu đã đọc để tạo ticket. Anh/Chị gửi lại tệp .md/.txt nhé.'
              if reason=='cached_document_invalid' else
              'Em chưa khôi phục được nội dung phân tích đã lưu. Anh/Chị có thể reply thử phân tích lại nhé.'
              if reason in ('cached_analysis_invalid','cached_document_invalid') else
              'Repository đã thay đổi từ lần phân tích trước. Nhờ Owner xác nhận nơi ghi nhận yêu cầu này giúp Em nhé.'),),
            pipeline.config.gh_repo)
    return result

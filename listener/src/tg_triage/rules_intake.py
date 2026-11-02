"""Trusted code facts for the rules-based intake path."""
from . import rules
from .decision_rules import CodeFacts


def _direct_closed_evidence(snapshot, members):
    return (len(members)==1 and not members[0].is_forward and not members[0].has_attachment and
            members[0].text.strip() in snapshot.extensions.explicit_evidence)


def _source_evidence_id(message):
    import hashlib, json
    value=dict(vars(message))
    value['received_at']=message.received_at.isoformat()
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode('utf-8')).hexdigest()


def _task_grounded(task, members, read_context=(), documents=()):
    import re
    from .analysis_task import projection, render_excerpts, explicit_task
    task=projection(task)
    if task is None: return False
    try:
        render_excerpts(task)
    except ValueError:
        return False
    if len(members)==1 and explicit_task(task,members[0].text,members[0].is_forward):
        return True
    ids={m.message_id for m in members}
    reply_ids={m.reply_to_id for m in members if m.reply_to_id is not None}
    evidence=[m.text for m in members] + [m.text for m in read_context if m.message_id in reply_ids]
    evidence += [row.get('text','') for row in documents if row.get('read_status')=='read' and row.get('message_id') in ids]
    # Match all excerpts within one source. This proves provenance only; the
    # renderer must not represent this as proof of intent or meaning.
    for value in evidence:
        if not isinstance(value,str): continue
        passages=[fragment.strip() for fragment in re.split(r'[.!?;\n]+',value) if fragment.strip()]
        if all(claim in passages for claim in (task['title'],task['work'],*task['acceptance'])):
            return True
    return False


def code_facts(sources, *, seen_image_ids=frozenset(), extraction_ok=True, reply_target='none', document_text_ids=frozenset()):
    sources = list(sources)
    if not sources or len({m.message_id for m in sources}) != len(sources): raise ValueError('invalid source refs')
    first = sources[0]
    match = rules.COMMAND.match(first.text or '')
    command = '/' + match[1].lower() if match else '∅'
    payload_text = first.text[match.end():].strip() if match else first.text.strip()
    has_text = bool(payload_text or any(m.text.strip() for m in sources[1:]))
    has_media = any(m.has_attachment for m in sources)
    payload = ('none' if not match else 'text' if payload_text else
               'media' if first.has_attachment else 'empty')
    ids = frozenset(m.message_id for m in sources)
    images = frozenset(m.message_id for m in sources if m.has_image)
    text_ids = frozenset(m.message_id for m in sources if m.text.strip()) | frozenset(document_text_ids)
    documents = frozenset(m.message_id for m in sources if m.has_document)
    return CodeFacts(command,payload,'có' if has_text else 'không','true' if extraction_ok else 'false',
                     reply_target,ids,images,frozenset(seen_image_ids),text_ids,documents)


def early_decision(snapshot, sources):
    """Only table-prioritized outcomes independent of extraction, for a single new source."""
    sources = list(sources)
    if len(sources) != 1: return None
    result = snapshot.evaluate({}, code_facts(sources, extraction_ok=False))
    row = next(row for row in snapshot.tables['readiness']['decision'] if row.cells['#'] == result.rule)
    independent = (row.cells['extraction_ok'] == '*' and row.cells['intent'] == '*'
                   and row.cells['cần CÓ tất cả'] == '—' and row.cells['cần CÓ ít nhất một'] == '—')
    if sources[0].reply_to_id is not None and row.cells['reply_target'] != '*': independent = False
    return result if independent else None


def evaluate_batch(snapshot, provider, sources, *, images=None, reply_target_for=None, asked_counts=None, allow_mixed=False, documents=None, read_context=()):
    """Validate every group before returning decisions; None means the whole batch holds."""
    sources = list(sources)
    if not sources: return None
    early = early_decision(snapshot, sources)
    if early is not None: return ((tuple(sources),early),)
    from .rules_extraction import partition_and_extract
    try:
        groups = partition_and_extract(provider, snapshot, sources, images=images, allow_mixed=allow_mixed, documents=documents, read_context=read_context)
        if groups is None: return None
        if allow_mixed and len(groups) != 1: return None
        owner = {mid: n for n,(ids,_) in enumerate(groups) for mid in ids}
        by_id = {source.message_id:source for source in sources}
        for source in sources:
            target = by_id.get(source.reply_to_id)
            if target is not None and (source.has_image or target.has_image) and owner[source.message_id] != owner[target.message_id]:
                return None
        outcomes = []
        for ids, facts in groups:
            members = tuple(source for source in sources if source.message_id in ids)
            if len(members) != len(ids): return None
            if snapshot.extensions:
                from .decision_rules import Decision
                from types import MappingProxyType
                intent = facts.get('intent', {})
                selected = snapshot.extensions.route(intent.get('value'))
                if 'task' in facts and selected != 'ANALYZE': return None
                if selected:
                    if 'lifecycle' in facts: return None
                    grounded_sources = {m.message_id for m in members if m.text.strip()}
                    if selected == 'ANALYZE':
                        grounded_sources |= set(images or {}) & set(ids)
                    if (len(members) != 1 or not intent.get('source_ids') or
                            not set(intent['source_ids']) <= set(ids) or
                            not set(intent['source_ids']) & grounded_sources or
                            (selected == 'ANALYZE' and 'task' not in facts) or
                            code_facts(members).command != '∅'):
                        return None
                    if selected == 'ANALYZE':
                        # Conservative grounding: until semantic claims can be
                        # independently verified, each output phrase must occur
                        # in source or actually-read context/document text.
                        if not _task_grounded(facts['task'],members,read_context,documents or ()):
                            return None
                    outcomes.append((members, Decision(selected, intent['value'], 'extension',
                        'extension_' + intent['value'], MappingProxyType({}), (), (),
                        snapshot.fingerprint, facts.get('task'))))
                    continue
            target = reply_target_for(members) if reply_target_for is not None else 'none'
            lifecycle = facts.get('lifecycle')
            if target == 'ticket_closed' and snapshot.extensions:
                reply_ids={m.reply_to_id for m in members if m.reply_to_id is not None}
                trusted_context=[m for m in read_context if m.message_id in reply_ids]
                allowed = set(ids) | {m.message_id for m in trusted_context}
                if lifecycle is None or not set(lifecycle['knownworking_source_ids'] + lifecycle['recurrence_source_ids']) <= allowed:
                    return None
            selected_images = frozenset(set(images or {}) & set(ids))
            doc_ids=frozenset(row['message_id'] for row in documents or () if row.get('read_status')=='read' and row['message_id'] in ids)
            unread_doc_ids=frozenset(row['message_id'] for row in documents or () if row.get('read_status')!='read' and row['message_id'] in ids)
            # A caption and unseen pixels can share one message ID. Restrict
            # only this ambiguous case, without adding a pure-text rule.
            from .decision_rules import normalize
            by_ref={m.message_id:m for m in members}
            grounded={}
            for name,field in facts.items():
                if name == 'lifecycle': continue
                cited=[by_ref[ref] for ref in field['source_ids'] if ref in by_ref]
                ambiguous=(name not in ('intent','media_ids') and cited and
                           all(m.has_image and m.message_id not in selected_images and m.message_id not in doc_ids for m in cited) and
                           any(m.text.strip() for m in cited))
                ambiguous_document=(name not in ('intent','media_ids') and cited and
                                    any(m.message_id in unread_doc_ids for m in cited) and
                                    not any(m.message_id in selected_images for m in cited))
                if not (ambiguous or ambiguous_document) or (normalize(field['value']) and normalize(field['value']) in normalize(' '.join([m.text for m in cited] + [row.get('text','') for row in documents or () if row.get('read_status')=='read' and row.get('message_id') in {m.message_id for m in cited}]))):
                    grounded[name]=field
            evaluated = snapshot.evaluate(grounded,code_facts(members,seen_image_ids=selected_images,reply_target='ticket_open' if target=='ticket_closed' else target,document_text_ids=doc_ids),asked_counts)
            if target == 'ticket_closed' and snapshot.extensions and evaluated.state != 'CHAT':
                from dataclasses import replace
                if (lifecycle['kind']=='evidence' and _direct_closed_evidence(snapshot,members)
                        and snapshot.extensions.disposition('evidence')=='APPEND_CLOSED'):
                    evaluated=replace(evaluated,state='APPEND_CLOSED',rule='lifecycle_direct_evidence')
                    outcomes.append((members,evaluated))
                    continue
                if (lifecycle['kind'] in ('unclear', 'reused_success') and len(members)==1
                        and snapshot.extensions.disposition(lifecycle['kind'])=='QUESTION'):
                    evaluated=replace(evaluated,state='LIFECYCLE_QUESTION',rule='lifecycle_clarification')
                    outcomes.append((members,evaluated))
                    continue
                # Related episode and clarification need their own durable
                # execution path; never approximate them with an append.
                # A model's label alone is not evidence that the issue never
                # worked. Until host-verifiable provenance establishes that
                # distinction, hold even when the model says "evidence".
                reason='lifecycle_'+lifecycle['kind']+'_pending'
                if lifecycle['kind']=='recurrence':
                    from .lifecycle import grounded_recurrence
                    if grounded_recurrence(lifecycle,trusted_context,members):
                        reason='lifecycle_recurrence_grounded_pending'
                evaluated = replace(evaluated,state='HOLD',rule=reason)
            if evaluated.state == 'NEED_INFO' and unread_doc_ids:
                return None
            outcomes.append((members,evaluated))
        return tuple(outcomes)
    except Exception:
        return None


def process(pipeline, message, start):
    """Partial integration; startup remains blocked until all paths are complete."""
    from .pacing import RELEASED_SOURCES
    if message.chat_id == pipeline.config.telegram.ops_chat_id:
        return _process_ops_reply(pipeline,message)
    answer=_lifecycle_answer(pipeline,message)
    if answer is not None:
        return answer
    guarded=_hold_lifecycle_reply(pipeline,message)
    if guarded is not None:
        return guarded
    if message.reply_to_id is not None:
        with pipeline.store._lock:
            manual=pipeline.store.connection.execute('''SELECT 1 FROM rules_sources s JOIN rules_topics t ON t.topic_id=s.topic_id
                WHERE s.chat_id=? AND s.message_id=? AND t.status='OPEN' AND t.phase='LEGACY_REVIEW' LIMIT 1''',
                (message.chat_id,message.reply_to_id)).fetchone()
        if manual:
            from .models import Decision
            result=Decision('pending_review','rule',reason='legacy_transition_awaiting_release')
            pipeline.store.finish(message,result)
            return result
    released = RELEASED_SOURCES.get()
    sources=[message]
    if released:
        for queued in pipeline.store.queued_messages():
            if queued.chat_id == message.chat_id and (queued.chat_id,queued.message_id) in released and queued.message_id != message.message_id:
                if pipeline.store.start_classifying(queued): sources.append(queued)
    sources.sort(key=lambda m:(m.received_at,m.message_id))
    if len(sources) > 1: return _process_batch(pipeline,sources)
    decision = early_decision(pipeline.rules_snapshot, [message])
    if decision is None:
        if pipeline.rules_snapshot.extensions:
            return _process_batch(pipeline, [message])
        if message.reply_to_id is not None:
            return _process_source_reply(pipeline,message)
        return _process_single_pending(pipeline, message)
    from .models import Decision
    topic = f'{message.chat_id}:{message.message_id}'
    pipeline.rules_pending.create(topic, message.chat_id, message.message_id)
    pipeline.rules_pending.record_decision(topic, decision)
    state = 'chat' if decision.state == 'CHAT' else 'pending_review'
    result = Decision(state, 'rule', case_key=topic, reason=decision.rule)
    pipeline.store.finish(message, result)
    if decision.state == 'CHAT':
        pipeline.rules_pending.close(topic, 'CLOSED')
    return result


def _process_single_pending(pipeline, message, *, allow_unmatched_reply=False):
    """Single-source NEED_INFO path; other paths remain blocked pending integration."""
    if (message.reply_to_id is not None and not allow_unmatched_reply) or message.chat_id == pipeline.config.telegram.ops_chat_id:
        raise RuntimeError('rules reply integration incomplete')
    if message.has_image and getattr(pipeline.classifier,'supports_vision',False):
        pipeline._prepared_images=pipeline.attachments.prepare_images(f'{message.chat_id}:{message.message_id}',[message])
    try:
        return _process_single_with_images(pipeline,message)
    finally:
        if pipeline._prepared_images is not None:
            pipeline._prepared_images.close()
            pipeline._prepared_images=None


def _process_single_with_images(pipeline, message):
    from dataclasses import replace
    from .decision_rules import Field
    from .models import Decision
    from .thinking import decision_turn
    topic = f'{message.chat_id}:{message.message_id}'
    pending = pipeline.rules_pending
    pending.create(topic, message.chat_id, message.message_id)
    attempt = pending.reserve_attempt(topic, pipeline.rules_snapshot.limits['hold_max_retries'])
    if attempt is None:
        return _escalate(pipeline,message,topic)
    pipeline.store.finish(message,Decision('pending_review','rule',case_key=topic,reason='rules_extract_pending'))
    with decision_turn(pipeline, 'rules:' + topic, topic, message.message_id):
        from . import document_reader, document_processing
        documents=document_reader.read_documents([message],pipeline.attachments.downloader) if message.has_document else []
        if documents: documents=document_processing.prepare(documents,pipeline.classifier)
        images=pipeline._prepared_images.for_messages([message]) if pipeline._prepared_images is not None else {}
        unread=any(document_reader.text_intent(message) and row.get('read_status')!='read' for row in documents)
        context=pipeline.store.autonomous_read_context([message]) if pipeline.rules_snapshot.extensions else ()
        outcome = None if unread else evaluate_batch(pipeline.rules_snapshot,pipeline.classifier,[message],images=images,documents=documents,read_context=context)
    if outcome is None:
        pending.finish_attempt(topic,attempt)
        pending.record_decision(topic,pipeline.rules_snapshot.evaluate({},code_facts([message],extraction_ok=False)))
        result=Decision('pending_review','rule',case_key=topic,reason='rules_hold')
        pipeline.store.finish(message,result)
        return result
    _,decision = outcome[0]
    refs = {pair:ref for ref,pair in pending.sources(topic).items()}
    present = {name:Field(field.value,tuple(refs[(message.chat_id,mid)] for mid in field.source_ids))
               for name,field in decision.present.items()}
    pending.record_decision(topic,replace(decision,present=present))
    if decision.state in ('ANALYZE', 'STATUS'):
        result = _read_only_result(pipeline, message, topic, decision)
        if result.decision=='pending_review':
            pipeline.store.finish(message,result)
            pending.finish_attempt(topic,attempt)
            return result
        pending.close(topic, 'CLOSED')
        pending.finish_attempt(topic, attempt, success=True)
        return result
    if decision.state == 'CHAT':
        result=Decision('chat','rule',case_key=topic,reason=decision.rule)
        pipeline.store.finish(message,result)
        pending.close(topic,'CLOSED')
        pending.finish_attempt(topic,attempt,success=True)
        return result
    if decision.state == 'READY':
        _resume_source(pipeline,message)
        result=_create_single(pipeline,message,topic,decision,document_reads=document_reader.proof(documents))
        pending.finish_attempt(topic,attempt,success=True)
        return result
    if decision.state != 'NEED_INFO':
        raise RuntimeError('rules decision integration incomplete')
    result=Decision('pending_review','rule',case_key=topic,reason=decision.rule)
    pipeline.store.finish(message,result)
    from .pipeline import Plan
    pipeline._acknowledge(Plan(topic,[message],message,'rule','pending_review'))
    _question(pipeline,message,topic,decision)
    pending.finish_attempt(topic,attempt,success=True)
    return result


def _read_only_result(pipeline, source, topic, decision, *, plan_id=None):
    """Render validated analysis or re-read host status; no issue write authority."""
    import hashlib
    from . import analysis_task, ticket_status
    from .models import Decision
    if decision.state == 'ANALYZE':
        body = (analysis_task.render(decision.task) if analysis_task.explicit_task(decision.task,source.text,source.is_forward)
                else analysis_task.render_excerpts(decision.task))
        reason = 'semantic_analyze'
    elif decision.state == 'STATUS':
        body = ticket_status.render(ticket_status.resolve(pipeline.store, [source], pipeline.config.gh_repo))
        reason = 'semantic_status'
    else:
        raise ValueError('unsupported read-only result')
    result = Decision('chat', 'rule', case_key=topic, reason=reason)
    if pipeline.config.mode != 'dry-run' and pipeline.notice_sender is None:
        return Decision('pending_review','rule',case_key=topic,reason='readonly_notice_unavailable')
    if pipeline.config.mode != 'dry-run' and plan_id is None:
        return Decision('pending_review','rule',case_key=topic,reason='readonly_plan_missing')
    pipeline.store.finish(source, result)
    if pipeline.config.mode != 'dry-run':
        from .rules_pending_store import RulesHalt
        store=pipeline.store
        with store._lock, store.connection:
            existing=store.connection.execute('SELECT 1 FROM group_notices WHERE chat_id=? AND message_id=?',(source.chat_id,source.message_id)).fetchone()
            if existing:
                return Decision('pending_review','rule',case_key=topic,reason='readonly_notice_conflict')
            store.connection.execute('INSERT INTO group_notices(chat_id,message_id,kind,status,reserved_at) VALUES(?,?,?,?,?)',
                (source.chat_id,source.message_id,decision.state,'reserved',store.clock().isoformat()))
            store.connection.execute('INSERT INTO rules_readonly_notices VALUES(?,?,?,?,?,?)',
                (source.chat_id,source.message_id,decision.state,plan_id,hashlib.sha256(body.encode('utf-8')).hexdigest(),body))
        try:
            sent=pipeline.notice_sender(source.chat_id,source.message_id,body)
        except Exception:
            sent=None
        status='sent' if type(sent) is int and sent>0 else 'failed'
        try:
            store.finish_group_notice(source.chat_id,source.message_id,status,sent if status=='sent' else None)
        except Exception as error:
            raise RulesHalt('read-only notice outcome unresolved; Owner inspection required') from error
        if status!='sent':
            raise RulesHalt('read-only notice outcome unresolved; Owner inspection required')
        try:
            with store._lock:
                notice=store.connection.execute('''SELECT n.kind,n.status,n.notice_message_id,
                    p.kind,p.plan_id,p.body_hash,p.body FROM group_notices n JOIN rules_readonly_notices p
                    ON p.chat_id=n.chat_id AND p.message_id=n.message_id
                    WHERE n.chat_id=? AND n.message_id=?''',(source.chat_id,source.message_id)).fetchone()
        except Exception as error:
            raise RulesHalt('read-only notice outcome unresolved; Owner inspection required') from error
        expected=(decision.state,'sent',sent,decision.state,plan_id,hashlib.sha256(body.encode('utf-8')).hexdigest(),body)
        if notice is None or tuple(notice)!=expected:
            raise RulesHalt('read-only notice outcome unresolved; Owner inspection required')
    return result


def _send_lifecycle_question(pipeline, source, topic, target, question, plan_id, batch_id, ordinal):
    """Reserve the exact group question before sending, retaining reply authority."""
    from .models import Decision
    from .rules_pending_store import RulesHalt
    result=Decision('pending_review','rule',case_key=topic,reason='lifecycle_question_sent')
    if pipeline.config.mode=='dry-run':
        return Decision('pending_review','rule',case_key=topic,reason='lifecycle_question_dry_run')
    if pipeline.notice_sender is None:
        return Decision('pending_review','rule',case_key=topic,reason='lifecycle_question_unavailable')
    if _resolve_reply_target(pipeline,source)!=target or target['state']!='closed':
        return Decision('pending_review','rule',case_key=topic,reason='lifecycle_lookup_failed')
    store=pipeline.store
    store.finish(source,result)
    with store._lock,store.connection:
        if store.connection.execute('SELECT 1 FROM group_notices WHERE chat_id=? AND message_id=?',(source.chat_id,source.message_id)).fetchone():
            return Decision('pending_review','rule',case_key=topic,reason='lifecycle_notice_conflict')
        store.connection.execute('INSERT INTO group_notices(chat_id,message_id,kind,status,reserved_at) VALUES(?,?,?,?,?)',
            (source.chat_id,source.message_id,'LIFECYCLE_QUESTION','reserved',store.clock().isoformat()))
        choices=pipeline.rules_snapshot.extensions.clarification
        store.connection.execute('INSERT INTO rules_lifecycle_questions VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
            (source.chat_id,source.message_id,topic,target['number'],pipeline.config.gh_repo,plan_id,question,
             pipeline.rules_snapshot.fingerprint,choices['evidence_answer'],choices['recurrence_answer'],batch_id,ordinal))
        store.connection.execute("UPDATE rules_topics SET phase='WAITING' WHERE topic_id=? AND status='OPEN'",(topic,))
    try:
        sent=pipeline.notice_sender(source.chat_id,source.message_id,question)
    except Exception:
        sent=None
    try:
        store.finish_group_notice(source.chat_id,source.message_id,'sent' if type(sent) is int and sent>0 else 'failed',sent if type(sent) is int and sent>0 else None)
        with store._lock:
            row=store.connection.execute('''SELECT n.kind,n.status,n.notice_message_id,q.topic_id,q.parent_number,q.repo,q.plan_id,q.question,q.fingerprint,q.evidence_answer,q.recurrence_answer,q.batch_id,q.ordinal
                FROM group_notices n JOIN rules_lifecycle_questions q ON q.chat_id=n.chat_id AND q.source_id=n.message_id
                WHERE n.chat_id=? AND n.message_id=?''',(source.chat_id,source.message_id)).fetchone()
            collisions=store.connection.execute("SELECT count(*) FROM group_notices WHERE chat_id=? AND notice_message_id=? AND status='sent'",(source.chat_id,sent)).fetchone()[0]
            local_collision=store.connection.execute('SELECT 1 FROM messages WHERE chat_id=? AND message_id=?',(source.chat_id,sent)).fetchone()
        if (type(sent) is not int or sent<=source.message_id or row is None or tuple(row)!=
                ('LIFECYCLE_QUESTION','sent',sent,topic,target['number'],pipeline.config.gh_repo,plan_id,question,
                 pipeline.rules_snapshot.fingerprint,choices['evidence_answer'],choices['recurrence_answer'],batch_id,ordinal) or collisions!=1 or local_collision):
            raise RulesHalt('lifecycle question outcome unresolved; Owner inspection required')
    except RulesHalt:
        raise
    except Exception as error:
        raise RulesHalt('lifecycle question outcome unresolved; Owner inspection required') from error
    return result


def _lifecycle_answer(pipeline,message):
    """Process only an exact reply to a sent lifecycle question in the same chat."""
    if message.reply_to_id is None or not pipeline.rules_snapshot.extensions:
        return None
    from .models import Decision
    from .rules_pending_store import RulesHalt
    from .store import _to_message
    from types import SimpleNamespace
    store=pipeline.store
    with store._lock:
        matches=store.connection.execute('''SELECT q.* FROM rules_lifecycle_questions q JOIN group_notices n
            ON n.chat_id=q.chat_id AND n.message_id=q.source_id
            JOIN rules_topics t ON t.topic_id=q.topic_id AND t.status='OPEN'
            WHERE n.chat_id=? AND n.notice_message_id=? AND n.kind='LIFECYCLE_QUESTION' AND n.status='sent' ''',
            (message.chat_id,message.reply_to_id)).fetchall()
        count=store.connection.execute("SELECT count(*) FROM group_notices WHERE chat_id=? AND notice_message_id=? AND status='sent'",(message.chat_id,message.reply_to_id)).fetchone()[0]
        local_collision=store.connection.execute('SELECT 1 FROM messages WHERE chat_id=? AND message_id=?',(message.chat_id,message.reply_to_id)).fetchone()
    if not matches: return None
    if len(matches)!=1 or count!=1 or local_collision:
        waiting=Decision('pending_review','rule',reason='lifecycle_notice_ambiguous')
        store.finish(message,waiting)
        return waiting
    q=matches[0]
    choices=pipeline.rules_snapshot.extensions.clarification
    waiting=Decision('pending_review','rule',case_key=q['topic_id'],reason='lifecycle_answer_unresolved')
    if (message.is_forward or message.has_attachment or q['repo']!=pipeline.config.gh_repo
            or q['fingerprint']!=pipeline.rules_snapshot.fingerprint
            or q['evidence_answer']!=choices['evidence_answer'] or q['recurrence_answer']!=choices['recurrence_answer']
            or q['question']!=choices['question'] or message.reply_to_id<=q['source_id']
            or message.message_id<=message.reply_to_id
            or message.text.strip() not in (choices['evidence_answer'],choices['recurrence_answer'])):
        pipeline.rules_pending.add_source(q['topic_id'],message.chat_id,message.message_id)
        store.finish(message,waiting)
        return waiting
    source_row=store.get(message.chat_id,q['source_id'])
    if source_row is None:
        store.finish(message,waiting); return waiting
    source=_to_message(source_row)
    from .rules_pending_store import readonly_plan_id
    import json
    with store._lock:
        planned=store.connection.execute('''SELECT g.plan,g.status,b.fingerprint FROM rules_batch_groups g
            JOIN rules_batches b ON b.batch_id=g.batch_id WHERE g.batch_id=? AND g.ordinal=?''',(q['batch_id'],q['ordinal'])).fetchone()
    try:
        plan=json.loads(planned['plan']) if planned else None
        if (not planned or planned['status']!='DONE' or planned['fingerprint']!=q['fingerprint']
                or readonly_plan_id(q['batch_id'],q['ordinal'],q['fingerprint'],plan)!=q['plan_id']
                or plan.get('state')!='LIFECYCLE_QUESTION' or plan.get('source_keys')!=[[message.chat_id,q['source_id']]]
                or plan.get('lifecycle_source_evidence')!=_source_evidence_id(source)
                or plan.get('lifecycle_question')!=q['question'] or plan.get('lifecycle_evidence_answer')!=q['evidence_answer']
                or plan.get('lifecycle_recurrence_answer')!=q['recurrence_answer']):
            raise ValueError('question plan mismatch')
    except (TypeError,ValueError,KeyError):
        pipeline.rules_pending.add_source(q['topic_id'],message.chat_id,message.message_id)
        store.finish(message,waiting); return waiting
    try:
        proof=_resolve_reply_target(pipeline,source)
    except Exception:
        proof=None
    if not proof or proof['state']!='closed' or proof['number']!=q['parent_number'] or plan.get('targets')!=[proof]:
        pipeline.rules_pending.add_source(q['topic_id'],message.chat_id,message.message_id)
        store.finish(message,waiting); return waiting
    choice='evidence' if message.text.strip()==choices['evidence_answer'] else 'recurrence'
    with store._lock,store.connection:
        topic_state=store.connection.execute("SELECT status FROM rules_topics WHERE topic_id=?",(q['topic_id'],)).fetchone()
        if topic_state is None or topic_state['status']!='OPEN':
            store.finish(message,waiting)
            return waiting
        inserted=store.connection.execute("INSERT OR IGNORE INTO rules_lifecycle_answers VALUES(?,?,?,?,'reserved')",
            (message.chat_id,q['source_id'],message.message_id,choice)).rowcount
        if inserted:
            ref=store.connection.execute('SELECT COALESCE(MAX(ref),0)+1 FROM rules_sources WHERE topic_id=?',(q['topic_id'],)).fetchone()[0]
            store.connection.execute('INSERT INTO rules_sources VALUES(?,?,?,?)',(q['topic_id'],ref,message.chat_id,message.message_id))
    if not inserted:
        store.finish(message,waiting); return waiting
    with store._lock:
        bound=store.connection.execute('''SELECT m.* FROM rules_sources s JOIN messages m
            ON m.chat_id=s.chat_id AND m.message_id=s.message_id WHERE s.topic_id=? ORDER BY s.ref''',(q['topic_id'],)).fetchall()
        member_count=store.connection.execute('SELECT count(*) FROM rules_sources WHERE topic_id=?',(q['topic_id'],)).fetchone()[0]
    context=[_to_message(row) for row in bound if row['message_id'] not in (source.message_id,message.message_id)]
    # An explicit A can append additional text as evidence, but it cannot
    # authorize media, forwarded instructions or unrelated conversation.
    # Recurrence still requires its own fresh success proof and holds context.
    supported_context=(choice=='evidence' and member_count==len(bound) and len(context)<=8 and
        all(m.chat_id==source.chat_id and source.message_id<m.message_id<message.message_id and
            m.reply_to_id==source.message_id and not m.is_forward and not m.has_attachment and
            m.text.strip().startswith(choices['evidence_context_prefix']+' ') and
            m.text.strip()[len(choices['evidence_context_prefix']):].strip()
            for m in context))
    exact_members=(member_count==len(bound) and {(r['chat_id'],r['message_id']) for r in bound}.issuperset(
        {(source.chat_id,source.message_id),(message.chat_id,message.message_id)}))
    if not exact_members or (member_count!=2 and not supported_context):
        with store._lock,store.connection:
            changed=store.connection.execute("UPDATE rules_lifecycle_answers SET status='awaiting_context' WHERE chat_id=? AND source_id=? AND answer_id=? AND status='reserved'",(message.chat_id,q['source_id'],message.message_id)).rowcount
        if changed!=1: raise RulesHalt('lifecycle answer outcome unresolved; Owner inspection required')
        store.finish(message,waiting)
        return waiting
    if choice=='recurrence':
        return _create_related_episode(pipeline,q,source,message,proof,plan)
    result=_append_batch_group(pipeline,q['topic_id'],[source,*context,message],SimpleNamespace(state='APPEND_CLOSED',present={},rule='lifecycle_confirmed_evidence'),[proof],())
    if result.decision!='followup':
        raise RulesHalt('lifecycle answer outcome unresolved; Owner inspection required')
    with store._lock,store.connection:
        changed=store.connection.execute("UPDATE rules_lifecycle_answers SET status='done' WHERE chat_id=? AND source_id=? AND answer_id=? AND status='reserved'",(message.chat_id,q['source_id'],message.message_id)).rowcount
    if changed!=1: raise RulesHalt('lifecycle answer outcome unresolved; Owner inspection required')
    return result


def _create_related_episode(pipeline,q,source,answer,proof,plan):
    """Create the first explicitly confirmed recurrence with readback and no replay."""
    from dataclasses import replace
    from .models import Classification, Decision
    from .issue_format import format_issue
    from .followups import _recurrence_body
    from .rules_pending_store import RulesHalt
    from .ticket_templates import active
    store=pipeline.store; repo=pipeline.config.gh_repo; parent=q['parent_number']
    def defer():
        with store._lock,store.connection:
            changed=store.connection.execute("UPDATE rules_lifecycle_answers SET status='awaiting_recurrence' WHERE chat_id=? AND source_id=? AND answer_id=? AND status='reserved'",(source.chat_id,source.message_id,answer.message_id)).rowcount
        if changed!=1: raise RulesHalt('recurrence answer outcome unresolved; Owner inspection required')
        result=Decision('pending_review','rule',case_key=q['topic_id'],reason='lifecycle_recurrence_answer_pending')
        store.finish(answer,result)
        return result
    if (pipeline.rules_snapshot.extensions.disposition('recurrence')!='RELATED' or pipeline.config.mode=='dry-run'
            or pipeline.notice_sender is None or source.has_attachment or not source.text.strip()):
        return defer()
    try:
        facts=dict(plan['present'])
        facts['intent']={'value':plan['intent'],'source_ids':[source.message_id]}
        ready=pipeline.rules_snapshot.evaluate(facts,code_facts([source],reply_target='none'))
        if ready.state!='READY' or ready.intent not in ('bug','task','feedback'):
            return defer()
        values=[ready.present[name].value for name in ('object','observed','work','problem','proposal') if name in ready.present]
        if not values: return defer()
        summary='; '.join(values)[:200]
        classification=Classification(ready.intent,1.0,summary[:80],summary,'medium')
    except (ValueError,KeyError,TypeError):
        return defer()
    try:
        issue=format_issue(source,classification,'rule',False,pipeline.config.ollama.model,
                           case_messages=[source,answer],key_message_ids=(source.message_id,),max_quoted=2,details={})
        issue=replace(issue,body=_recurrence_body(issue.body,f'https://github.com/{repo}/issues/{parent}'))
    except Exception:
        return defer()
    try:
        if _resolve_reply_target(pipeline,source)!=proof: return defer()
    except Exception:
        return defer()
    with store._lock:
        try:
            store.connection.execute('BEGIN IMMEDIATE')
            # Existing legacy episodes or operations make this deliberately
            # narrow first-episode path ineligible; never reuse old success.
            prior=store.connection.execute('''SELECT 1 FROM followup_episodes e JOIN followup_roots r
                ON r.root_key=e.parent_root_key WHERE r.issue_number=? AND e.repo=? COLLATE NOCASE LIMIT 1''',(parent,repo)).fetchone()
            prior_op=store.connection.execute('''SELECT 1 FROM followup_operations o JOIN followup_jobs j ON j.id=o.job_id
                JOIN followup_roots r ON r.root_key=j.root_key WHERE r.issue_number=?
                AND o.kind='related_create' LIMIT 1''',(parent,)).fetchone()
            existing=store.connection.execute('SELECT 1 FROM rules_related_episodes WHERE repo=? COLLATE NOCASE AND parent_number=?',(repo,parent)).fetchone()
            notice=store.connection.execute('SELECT 1 FROM group_notices WHERE chat_id=? AND message_id=?',(answer.chat_id,answer.message_id)).fetchone()
            members=store.connection.execute('SELECT count(*) FROM rules_sources WHERE topic_id=?',(q['topic_id'],)).fetchone()[0]
            source_state=store.connection.execute('SELECT decision,github_called_at,case_key,group_key FROM messages WHERE chat_id=? AND message_id=?',(source.chat_id,source.message_id)).fetchone()
            if (prior or prior_op or existing or notice or members!=2 or source_state is None or tuple(source_state)!=('pending_review',None,None,None)
                    or store.count_recent_issues()>=pipeline.config.max_issues_per_hour):
                store.connection.rollback()
                return defer()
            store.connection.execute("INSERT INTO rules_related_episodes(chat_id,repo,parent_number,source_id,answer_id,status) VALUES(?,?,?,?,?,'reserved')",
                (source.chat_id,repo,parent,source.message_id,answer.message_id))
            changed=store.connection.execute("UPDATE messages SET decision='pending',github_called_at=? WHERE chat_id=? AND message_id=? AND decision='pending_review' AND github_called_at IS NULL",
                (store.clock().isoformat(),source.chat_id,source.message_id)).rowcount
            if changed!=1: raise RulesHalt('recurrence reservation changed')
            store.connection.commit()
        except Exception as error:
            if store.connection.in_transaction: store.connection.rollback()
            raise RulesHalt('recurrence reservation unresolved; Owner inspection required') from error
    try:
        number,_=pipeline.github.create_issue(issue)
        if type(number) is not int or number<=0 or pipeline.github.verify_created_issue(number,issue)!=number:
            raise ValueError('related issue readback not verified')
        url=f'https://github.com/{repo}/issues/{number}'
        notice_body=active().render('group-replies.md','followup.recurrence')+'\n'+url
        with store._lock,store.connection:
            changed=store.connection.execute("UPDATE rules_related_episodes SET status='verified',issue_number=?,issue_url=?,notice_body=? WHERE chat_id=? AND repo=? AND parent_number=? AND status='reserved'",
                (number,url,notice_body,source.chat_id,repo,parent)).rowcount
            if changed!=1: raise RulesHalt('related issue ledger changed')
            store.connection.execute("INSERT INTO group_notices(chat_id,message_id,kind,status,reserved_at) VALUES(?,?,?,'reserved',?)",
                (answer.chat_id,answer.message_id,'LIFECYCLE_EPISODE',store.clock().isoformat()))
        sent=pipeline.notice_sender(answer.chat_id,answer.message_id,notice_body)
        if type(sent) is not int or sent<=answer.message_id: raise RulesHalt('related episode notice unresolved')
        store.finish_group_notice(answer.chat_id,answer.message_id,'sent',sent)
        with store._lock,store.connection:
            notice=store.connection.execute('SELECT kind,status,notice_message_id FROM group_notices WHERE chat_id=? AND message_id=?',(answer.chat_id,answer.message_id)).fetchone()
            episode=store.connection.execute('SELECT status,issue_number,issue_url,notice_body FROM rules_related_episodes WHERE chat_id=? AND repo=? AND parent_number=?',(source.chat_id,repo,parent)).fetchone()
            collisions=store.connection.execute("SELECT count(*) FROM group_notices WHERE chat_id=? AND notice_message_id=? AND status='sent'",(answer.chat_id,sent)).fetchone()[0]
            local_collision=store.connection.execute('SELECT 1 FROM messages WHERE chat_id=? AND message_id=?',(answer.chat_id,sent)).fetchone()
            if notice is None or tuple(notice)!=('LIFECYCLE_EPISODE','sent',sent) or collisions!=1 or local_collision or episode is None or tuple(episode)!=('verified',number,url,notice_body):
                raise RulesHalt('related episode notice unresolved')
            for item,state in ((source,'issue'),(answer,'followup')):
                changed=store.connection.execute("UPDATE messages SET decision=?,tier='rule',issue_number=?,issue_url=?,reason='lifecycle_related_episode' WHERE chat_id=? AND message_id=?",
                    (state,number,url,item.chat_id,item.message_id)).rowcount
                mapped=store.connection.execute('SELECT decision,issue_number,issue_url,reason FROM messages WHERE chat_id=? AND message_id=?',(item.chat_id,item.message_id)).fetchone()
                if changed!=1 or mapped is None or tuple(mapped)!=(state,number,url,'lifecycle_related_episode'):
                    raise RulesHalt('related source mapping unresolved')
            if store.connection.execute("UPDATE rules_related_episodes SET status='done' WHERE chat_id=? AND repo=? AND parent_number=? AND status='verified'",(source.chat_id,repo,parent)).rowcount!=1:
                raise RulesHalt('related issue ledger changed')
            if store.connection.execute("UPDATE rules_lifecycle_answers SET status='done' WHERE chat_id=? AND source_id=? AND answer_id=? AND status='reserved'",(source.chat_id,source.message_id,answer.message_id)).rowcount!=1:
                raise RulesHalt('recurrence answer ledger changed')
            if store.connection.execute("UPDATE rules_topics SET status='CLOSED' WHERE topic_id=? AND status='OPEN'",(q['topic_id'],)).rowcount!=1:
                raise RulesHalt('related topic completion unresolved')
        return Decision('issue','rule',case_key=q['topic_id'],issue_number=number,issue_url=url,reason='lifecycle_related_episode')
    except Exception as error:
        raise RulesHalt('related episode outcome unresolved; Owner inspection required') from error


def _hold_lifecycle_reply(pipeline,message):
    """Never reclassify descendants of an unresolved closed-ticket episode."""
    if message.reply_to_id is None or not pipeline.rules_snapshot.extensions: return None
    from .models import Decision
    store=pipeline.store
    with store._lock:
        row=store.connection.execute('''SELECT DISTINCT q.topic_id,t.status FROM rules_lifecycle_questions q
            JOIN rules_topics t ON t.topic_id=q.topic_id
            LEFT JOIN rules_sources s ON s.topic_id=q.topic_id AND s.chat_id=q.chat_id
            LEFT JOIN group_notices n ON n.chat_id=q.chat_id AND n.message_id=q.source_id AND n.status='sent'
            WHERE q.chat_id=? AND ((t.status='OPEN' AND s.message_id=?) OR n.notice_message_id=?)''',
            (message.chat_id,message.reply_to_id,message.reply_to_id)).fetchall()
        parent=store.get(message.chat_id,message.reply_to_id) if not row else None
    if not row and not (parent and parent['decision']=='pending_review' and
                        isinstance(parent['reason'],str) and parent['reason'].startswith('lifecycle_')):
        return None
    topic=row[0]['topic_id'] if len(row)==1 else None
    if topic is not None and row[0]['status']=='OPEN':
        pipeline.rules_pending.add_source(topic,message.chat_id,message.message_id)
    result=Decision('pending_review','rule',case_key=topic,reason='lifecycle_reply_awaiting_resolution')
    store.finish(message,result)
    return result


def _process_batch(pipeline, sources, batch_id=None):
    if any(m.has_image for m in sources) and getattr(pipeline.classifier,'supports_vision',False):
        pipeline._prepared_images=pipeline.attachments.prepare_images(batch_id or f'{sources[0].chat_id}:{sources[0].message_id}',sources)
    try:
        return _process_batch_inner(pipeline,sources,batch_id)
    finally:
        if pipeline._prepared_images is not None:
            pipeline._prepared_images.close()
            pipeline._prepared_images=None


def _process_batch_inner(pipeline, sources, batch_id=None):
    from .models import Decision
    from .thinking import decision_turn
    pending=pipeline.rules_pending
    handled=[]; remaining=[]
    for member in sources:
        result=(_lifecycle_answer(pipeline,member) if batch_id is None else None) or _hold_lifecycle_reply(pipeline,member)
        if result is None: remaining.append(member)
        else: handled.append(result)
    if handled:
        if batch_id is not None:
            return Decision('pending_review','rule',case_key=batch_id,reason='lifecycle_reply_awaiting_resolution')
        if remaining:
            handled.append(_process_batch(pipeline,remaining))
        return handled[0] if len(handled)==1 else pipeline._combine('lifecycle-batch',handled)
    with pipeline.store._lock:
        blocked=any(m.reply_to_id is not None and pipeline.store.connection.execute('''SELECT 1 FROM rules_sources s
            JOIN rules_topics t ON t.topic_id=s.topic_id WHERE s.chat_id=? AND s.message_id=?
            AND t.status='OPEN' AND t.phase='LEGACY_REVIEW' LIMIT 1''',(m.chat_id,m.reply_to_id)).fetchone() for m in sources)
    if blocked:
        result=Decision('pending_review','rule',reason='legacy_transition_awaiting_release')
        for m in sources: pipeline.store.finish(m,result)
        return result
    ids={m.message_id for m in sources}
    external=[m for m in sources if m.reply_to_id is not None and m.reply_to_id not in ids]
    batch_id=batch_id or 'batch:' + str(sources[0].chat_id) + ':' + str(min(m.message_id for m in sources))
    cached=pending.batch(batch_id)
    if cached is None:
        pending.create(batch_id,sources[0].chat_id,sources[0].message_id)
        for source in sources[1:]: pending.add_source(batch_id,source.chat_id,source.message_id)
        attempt=pending.reserve_attempt(batch_id,pipeline.rules_snapshot.limits['hold_max_retries'])
        if attempt is None: return _escalate(pipeline,sources[0],batch_id)
        for source in sources:
            pipeline.store.finish(source,Decision('pending_review','rule',case_key=batch_id,reason='rules_extract_pending'))
        with decision_turn(pipeline,'rules:' + batch_id,batch_id,sources[0].message_id):
            pending_bindings={}
            proofs={}
            if not pipeline.rules_snapshot.extensions:
                try:
                    for m in external:
                        pending_bindings[m.message_id]=pending.topic_for_source_reply(m.chat_id,m.reply_to_id,
                            pipeline.rules_snapshot.limits['pending_expiry_days'])
                        proofs[m.message_id]=None if pending_bindings[m.message_id] else _resolve_reply_target(pipeline,m)
                except Exception:
                    pending.finish_attempt(batch_id,attempt)
                    return Decision('pending_review','rule',case_key=batch_id,reason='lifecycle_lookup_failed')
                if any(proof and proof['state']=='closed' for proof in proofs.values()):
                    pending.finish_attempt(batch_id,attempt,success=True)
                    return Decision('pending_review','rule',case_key=batch_id,reason='closed_target_not_supported')
            def reply_target(members):
                for m in members:
                    if m not in external or m.message_id in pending_bindings: continue
                    binding=pending.topic_for_source_reply(m.chat_id,m.reply_to_id,
                        pipeline.rules_snapshot.limits['pending_expiry_days'])
                    pending_bindings[m.message_id]=binding
                    proofs[m.message_id]=None if binding else _resolve_reply_target(pipeline,m)
                targets={proofs[m.message_id]['number'] for m in members if proofs.get(m.message_id)}
                if len(targets)>1: raise ValueError('conflicting reply targets')
                states={proofs[m.message_id]['state'] for m in members if proofs.get(m.message_id)}
                if len(states)>1: raise ValueError('conflicting lifecycle states')
                return ('ticket_closed' if states=={'closed'} else 'ticket_open') if targets else 'none'
            from . import document_reader, document_processing
            documents=document_reader.read_documents(sources,pipeline.attachments.downloader)
            if documents: documents=document_processing.prepare(documents,pipeline.classifier)
            by_id={m.message_id:m for m in sources}
            unread=any(document_reader.text_intent(by_id[row['message_id']]) and row.get('read_status')!='read' for row in documents)
            images=pipeline._prepared_images.for_messages(sources) if pipeline._prepared_images is not None else {}
            context=pipeline.store.autonomous_read_context(sources) if pipeline.rules_snapshot.extensions else ()
            groups=None if unread else evaluate_batch(pipeline.rules_snapshot,pipeline.classifier,sources,images=images,documents=documents,reply_target_for=reply_target,read_context=context)
            pending_groups={}
            if groups is not None:
                from dataclasses import replace
                from .store import _to_message
                bound_groups=[(index,members,{pending_bindings[m.message_id][0] for m in members if pending_bindings.get(m.message_id)})
                              for index,(members,_) in enumerate(groups)]
                bound_groups=[item for item in bound_groups if item[2]]
                if len(bound_groups)>1 or any(len(bound)!=1 for _,_,bound in bound_groups):
                    groups=None
                if groups is not None and bound_groups:
                    _,_,bound=bound_groups[0]
                    history=[pipeline.store.get(*pair) for pair in pending.sources(next(iter(bound))).values()]
                    if any(row is None or row['has_document'] for row in history): groups=None
            if groups is not None:
                for index,(members,_) in enumerate(groups):
                    bound={pending_bindings[m.message_id][0] for m in members if pending_bindings.get(m.message_id)}
                    if not bound: continue
                    if len(bound)!=1 or pending_groups:
                        groups=None; break  # One ordinary turn cannot safely re-extract multiple pending topics.
                    topic=next(iter(bound))
                    snapshot=pending.topic_snapshot(topic)
                    if snapshot is None or snapshot['status'] != 'OPEN': groups=None; break
                    refs={ref:(chat_id,message_id) for ref,chat_id,message_id in snapshot['sources']}
                    additions=[]
                    for member in members:
                        pair=(member.chat_id,member.message_id)
                        if pair not in refs.values():
                            ref=max(refs,default=0)+1
                            refs[ref]=pair
                            additions.append((ref,*pair))
                    rows=[(ref,pipeline.store.get(*pair)) for ref,pair in sorted(refs.items())]
                    if any(row is None for _,row in rows): groups=None; break
                    originals=[(ref,_to_message(row)) for ref,row in rows]
                    if any(m.has_document for _,m in originals): groups=None; break
                    lookup={(m.chat_id,m.message_id):ref for ref,m in originals}
                    scoped=[replace(m,message_id=ref,reply_to_id=lookup.get((m.chat_id,m.reply_to_id))) for ref,m in originals]
                    asked=next(pending_bindings[m.message_id][1] for m in members if pending_bindings.get(m.message_id))
                    scoped_images={}
                    if any(m.has_image for _,m in originals) and getattr(pipeline.classifier,'supports_vision',False):
                        actual={(m.chat_id,m.message_id):m for m in sources}
                        actual.update({(m.chat_id,m.message_id):m for _,m in originals})
                        if pipeline._prepared_images is not None: pipeline._prepared_images.close()
                        pipeline._prepared_images=pipeline.attachments.prepare_images(batch_id,list(actual.values()))
                        scoped_images={lookup[(m.chat_id,m.message_id)]:path for m,path in pipeline._prepared_images.accepted
                                       if (m.chat_id,m.message_id) in lookup}
                    merged=evaluate_batch(pipeline.rules_snapshot,pipeline.classifier,scoped,images=scoped_images,asked_counts=asked,allow_mixed=True)
                    if merged is None or len(merged)!=1: groups=None; break
                    pending_groups[index]=(topic,merged[0][1],tuple(m.message_id for m in members if pending_bindings.get(m.message_id)),snapshot,additions)
        if groups is None:
            pending.finish_attempt(batch_id,attempt)
            return Decision('pending_review','rule',case_key=batch_id,reason='rules_hold')
        owner={m.message_id:i for i,(members,_) in enumerate(groups) for m in members}
        if any(m.reply_to_id in owner and owner[m.message_id] != owner[m.reply_to_id]
               and groups[owner[m.message_id]][1].state not in ('ANALYZE','STATUS') for m in sources):
            pending.finish_attempt(batch_id,attempt)
            return Decision('pending_review','rule',case_key=batch_id,reason='rules_reply_partition_conflict')
        plans=[]
        for index,(members,decision) in enumerate(groups):
            pending_group=pending_groups.get(index)
            if pending_group: decision=pending_group[1]
            if decision.state not in ('READY','APPEND','APPEND_CLOSED','NEED_INFO','CHAT','MEDIA_ONLY','ANALYZE','STATUS','HOLD','LIFECYCLE_QUESTION'):
                pending.finish_attempt(batch_id,attempt)
                return Decision('pending_review','rule',case_key=batch_id,reason='rules_unsupported')
            plans.append(dict(source_keys=[[m.chat_id,m.message_id] for m in members],state=decision.state,
                intent=decision.intent,intent_rule=decision.intent_rule,rule=decision.rule,
                present={k:{'value':v.value,'source_ids':list(v.source_ids)} for k,v in decision.present.items()},
                questions=list(decision.questions),asked_fields=list(decision.asked_fields),
                task=decision.task,
                lifecycle_question=(pipeline.rules_snapshot.extensions.clarification['question'] if decision.state=='LIFECYCLE_QUESTION' else None),
                lifecycle_evidence_answer=(pipeline.rules_snapshot.extensions.clarification['evidence_answer'] if decision.state=='LIFECYCLE_QUESTION' else None),
                lifecycle_recurrence_answer=(pipeline.rules_snapshot.extensions.clarification['recurrence_answer'] if decision.state=='LIFECYCLE_QUESTION' else None),
                lifecycle_source_evidence=(_source_evidence_id(members[0]) if decision.state=='LIFECYCLE_QUESTION' else None),
                targets=[proofs[m.message_id] for m in members if proofs.get(m.message_id)],
                pending_topic=pending_group[0] if pending_group else None,
                pending_reply_ids=list(pending_group[2]) if pending_group else [],
                document_reads=document_reader.proof([row for row in documents if row['message_id'] in {m.message_id for m in members}])))
        updates=[(index,group[0],group[3],group[4]) for index,group in pending_groups.items()]
        if not pending.save_batch(batch_id,pipeline.rules_snapshot.fingerprint,plans,updates):
            pending.finish_attempt(batch_id,attempt)
            return Decision('pending_review','rule',case_key=batch_id,reason='rules_pending_changed')
        cached=pending.batch(batch_id)
    fingerprint,plans=cached
    if fingerprint != pipeline.rules_snapshot.fingerprint:
        return Decision('pending_review','rule',case_key=batch_id,reason='rules_fingerprint_changed')
    if any(status=='BLOCKED' for _,_,status in plans):
        return Decision('pending_review','rule',case_key=batch_id,reason='rules_batch_blocked')
    if any(status in ('RESERVED','AMBIGUOUS') for _,_,status in plans):
        for ordinal,_,status in plans:
            if status == 'RESERVED': pending.transition_group(batch_id,ordinal,'RESERVED','AMBIGUOUS')
        return Decision('pending_review','rule',case_key=batch_id,reason='rules_batch_ambiguous')
    all_keys=[tuple(key) for _,plan,_ in plans for key in plan['source_keys']]
    if len(all_keys)!=len(set(all_keys)) or set(all_keys)!=set(pending.sources(batch_id).values()):
        return Decision('pending_review','rule',case_key=batch_id,reason='rules_partition_changed')
    for _,plan,status in plans:
        if status != 'PLANNED': continue
        rows=[pipeline.store.get(*key) for key in plan['source_keys']]
        ids={key[1] for key in plan['source_keys']}
        if any(row is None for row in rows):
            return Decision('pending_review','rule',case_key=batch_id,reason='rules_authority_changed')
        if plan.get('state')=='APPEND_CLOSED':
            from .store import _to_message
            if (plan.get('rule')!='lifecycle_direct_evidence' or plan.get('pending_topic')
                    or len(plan.get('targets',()))!=1
                    or not _direct_closed_evidence(pipeline.rules_snapshot,[_to_message(row) for row in rows])):
                return Decision('pending_review','rule',case_key=batch_id,reason='lifecycle_evidence_pending')
        if plan.get('state')=='LIFECYCLE_QUESTION':
            from .store import _to_message
            if (len(rows)!=1 or plan.get('pending_topic') or len(plan.get('targets',()))!=1
                    or plan.get('lifecycle_question')!=pipeline.rules_snapshot.extensions.clarification['question']
                    or plan.get('lifecycle_source_evidence')!=_source_evidence_id(_to_message(rows[0]))
                    or plan.get('lifecycle_evidence_answer')!=pipeline.rules_snapshot.extensions.clarification['evidence_answer']
                    or plan.get('lifecycle_recurrence_answer')!=pipeline.rules_snapshot.extensions.clarification['recurrence_answer']):
                return Decision('pending_review','rule',case_key=batch_id,reason='rules_authority_changed')
        if plan['state'] in ('ANALYZE','STATUS'):
            from .analysis_task import valid_task
            if (len(rows)!=1 or plan.get('pending_topic') or plan.get('targets') or
                    (plan['state']=='ANALYZE' and not valid_task(plan.get('task'))) or
                    (plan['state']=='STATUS' and plan.get('task') is not None)):
                return Decision('pending_review','rule',case_key=batch_id,reason='rules_authority_changed')
            if plan['state']=='ANALYZE':
                from .store import _to_message
                from . import document_reader
                source_members=[_to_message(row) for row in rows]
                context=pipeline.store.autonomous_read_context(source_members)
                evidence_docs=[]
                if plan.get('document_reads'):
                    # Re-read the file rather than trusting cached model text.
                    evidence_docs=document_reader.read_documents(source_members,pipeline.attachments.downloader)
                    stored=plan['document_reads']
                    raw_proof=[{k:v for k,v in row.items() if k!='processing'} for row in stored]
                    if (not document_reader.valid_proof(stored,source_members)
                            or not document_reader.valid_contents(evidence_docs)
                            or document_reader.proof(evidence_docs)!=raw_proof):
                        return Decision('pending_review','rule',case_key=batch_id,reason='rules_document_provenance_changed')
                if not _task_grounded(plan.get('task'),source_members,context,evidence_docs):
                    return Decision('pending_review','rule',case_key=batch_id,reason='rules_task_ungrounded')
            continue
        for reply_id in plan.get('pending_reply_ids',()):
            row=next((row for row in rows if row['message_id']==reply_id),None)
            binding=(pending.topic_for_source_reply(row['chat_id'],row['reply_to_id'],pipeline.rules_snapshot.limits['pending_expiry_days']) if row is not None else None)
            if binding is None or binding[0] != plan.get('pending_topic'):
                return Decision('pending_review','rule',case_key=batch_id,reason='rules_authority_changed')
        from .store import _to_message
        try:
            expected={p['reply_message_id']:p for p in plan.get('targets',())}
            for row in rows:
                if row['reply_to_id'] is not None and row['reply_to_id'] not in ids:
                    if _resolve_reply_target(pipeline,_to_message(row)) != expected.get(row['message_id']):
                        return Decision('pending_review','rule',case_key=batch_id,reason='rules_authority_changed')
        except Exception:
            return Decision('pending_review','rule',case_key=batch_id,reason='lifecycle_lookup_failed')
    from .decision_rules import Decision as RuleDecision, Field
    from .store import _to_message
    from .pipeline import Plan
    results=[]
    for ordinal,plan,status in plans:
        if status == 'DONE': continue
        members=[]
        for chat_id,message_id in plan['source_keys']:
            row=pipeline.store.get(chat_id,message_id)
            if row is None: return Decision('pending_review','rule',case_key=batch_id,reason='rules_source_missing')
            members.append(_to_message(row))
        member_ids={m.message_id for m in members}
        if not members or any(m.chat_id != members[0].chat_id for m in members):
            return Decision('pending_review','rule',case_key=batch_id,reason='rules_authority_changed')
        first=min(members,key=lambda m:m.message_id); topic=plan.get('pending_topic') or f'{first.chat_id}:{first.message_id}'
        if plan.get('pending_topic'):
            rows=[pipeline.store.get(*pair) for _,pair in sorted(pending.sources(topic).items())]
            if any(row is None for row in rows): return Decision('pending_review','rule',case_key=batch_id,reason='rules_source_missing')
            originals=[_to_message(row) for row in rows]
            first=originals[0]
            source_members=[m for m in originals if m.chat_id==first.chat_id]
        else:
            pending.create(topic,first.chat_id,first.message_id)
            for member in members:
                if member.message_id != first.message_id: pending.add_source(topic,member.chat_id,member.message_id)
            originals=members
            source_members=members
        decision=RuleDecision(plan['state'],plan['intent'],plan['intent_rule'],plan['rule'],
            {k:Field(v['value'],tuple(v['source_ids'])) for k,v in plan['present'].items()},
            tuple(plan['questions']),tuple(plan['asked_fields']),fingerprint,plan.get('task'))
        claimed=(pending.transition_group(batch_id,ordinal,'PLANNED','RESERVED',topic=topic,snapshot=plan['pending_snapshot'])
                 if plan.get('pending_topic') else pending.transition_group(batch_id,ordinal,'PLANNED','RESERVED'))
        if not claimed:
            return Decision('pending_review','rule',case_key=batch_id,reason='rules_pending_changed')
        if plan.get('pending_topic'):
            pending.record_decision(topic,decision)
        else:
            refs={pair:ref for ref,pair in pending.sources(topic).items()}
            from dataclasses import replace
            journal={k:Field(v.value,tuple(refs[(first.chat_id,mid)] for mid in v.source_ids)) for k,v in decision.present.items()}
            pending.record_decision(topic,replace(decision,present=journal))
        if decision.state=='LIFECYCLE_QUESTION':
            from .rules_pending_store import readonly_plan_id
            result=_send_lifecycle_question(pipeline,first,topic,plan['targets'][0],plan['lifecycle_question'],
                readonly_plan_id(batch_id,ordinal,fingerprint,plan),batch_id,ordinal)
            if result.reason!='lifecycle_question_sent':
                pipeline.store.finish(first,result)
                pending.transition_group(batch_id,ordinal,'RESERVED','BLOCKED')
                results.append(result)
                continue
        elif decision.state in ('ANALYZE','STATUS'):
            from .rules_pending_store import readonly_plan_id
            result=_read_only_result(pipeline,first,topic,decision,plan_id=readonly_plan_id(batch_id,ordinal,fingerprint,plan))
            if result.decision=='pending_review':
                pipeline.store.finish(first,result)
                pending.transition_group(batch_id,ordinal,'RESERVED','BLOCKED')
                results.append(result)
                continue
            pending.close(topic,'CLOSED')
        elif decision.state == 'READY':
            for member in source_members: _resume_source(pipeline,member)
            result=_create_single(pipeline,first,topic,decision,evidence_messages=originals if plan.get('pending_topic') else None,
                                  members=source_members,document_reads=plan.get('document_reads',()))
        elif decision.state in ('APPEND','APPEND_CLOSED'):
            result=_append_batch_group(pipeline,topic,members,decision,plan['targets'],plan.get('document_reads',()))
            if result.decision=='pending_review':
                for member in members: pipeline.store.finish(member,result)
                pending.transition_group(batch_id,ordinal,'RESERVED','BLOCKED')
                results.append(result)
                continue
        else:
            state='chat' if decision.state=='CHAT' else 'pending_review'
            result=Decision(state,'rule',case_key=topic,reason=decision.rule)
            for member in members: pipeline.store.finish(member,result)
            if decision.state=='NEED_INFO':
                pipeline._acknowledge(Plan(topic,members,first,'rule','pending_review'))
                _question(pipeline,first,topic,decision)
            if decision.state=='CHAT': pending.close(topic,'CLOSED')
            if decision.state=='HOLD' and decision.rule.startswith('lifecycle_'):
                # Do not let a later reply reclassify a held closed-issue
                # episode as an unrelated READY ticket.
                pending.close(topic,'ESCALATED')
        if result.decision == 'error':
            pending.transition_group(batch_id,ordinal,'RESERVED','AMBIGUOUS')
            from .rules_pending_store import RulesHalt
            raise RulesHalt('unresolved ambiguous operation; Owner inspection required')
        pending.transition_group(batch_id,ordinal,'RESERVED','DONE')
        results.append(result)
        if pending.unresolved_ambiguity():
            from .rules_pending_store import RulesHalt
            raise RulesHalt('unresolved ambiguous operation; Owner inspection required')
    # Completing a cached batch never calls the model again.
    blocked=any(status=='BLOCKED' for _,_,status in pending.batch(batch_id)[1])
    pending.close(batch_id,'ESCALATED' if blocked else 'CLOSED')
    if cached is not None:
        with pipeline.store._lock,pipeline.store.connection:
            pipeline.store.connection.execute("UPDATE rules_topics SET attempt_reserved=0,phase='WAITING',attempts=0 WHERE topic_id=?",(batch_id,))
    return pipeline._combine(batch_id,results) if results else Decision('chat','rule',case_key=batch_id,reason='rules_batch_complete')


def _resolve_reply_target(pipeline,message):
    if message.reply_to_id is not None and message.message_id<=message.reply_to_id:
        raise ValueError('reply precedes its target')
    row=pipeline.store.get(message.chat_id,message.reply_to_id)
    alias=None
    with pipeline.store._lock:
        aliases=pipeline.store.connection.execute('''SELECT e.answer_id FROM group_notices n JOIN rules_related_episodes e
            ON e.chat_id=n.chat_id AND e.answer_id=n.message_id
            WHERE n.chat_id=? AND n.notice_message_id=? AND n.kind='LIFECYCLE_EPISODE'
              AND n.status='sent' AND e.status='done' ''',(message.chat_id,message.reply_to_id)).fetchall()
        count=pipeline.store.connection.execute("SELECT count(*) FROM group_notices WHERE chat_id=? AND notice_message_id=? AND status='sent'",(message.chat_id,message.reply_to_id)).fetchone()[0]
        if aliases and (len(aliases)!=1 or count!=1 or row is not None or
                        message.reply_to_id<=aliases[0]['answer_id'] or message.message_id<=message.reply_to_id):
            raise ValueError('ambiguous episode notice')
        alias=aliases[0] if aliases else None
    if alias is not None:
        row=pipeline.store.get(message.chat_id,alias['answer_id'])
    if (row is not None and row['decision']=='pending_review' and
            isinstance(row['reason'],str) and row['reason'].startswith('lifecycle_')):
        raise ValueError('closed lifecycle is awaiting reconciliation')
    number=row['issue_number'] if row is not None else None
    if (row is None or type(number) is not int or number <= 0 or row['decision'] not in ('issue','followup')
            or row['issue_url'] != f'https://github.com/{pipeline.config.gh_repo}/issues/{number}'):
        return None
    observed=pipeline.github.lifecycle_snapshot(number)
    if (observed.get('number')!=number or observed.get('repo','').casefold()!=pipeline.config.gh_repo.casefold()
            or observed.get('state') not in ('open','closed') or not observed.get('observed_at') or
            (observed.get('state')=='closed' and (not isinstance(observed.get('updated_at'),str) or not observed['updated_at'].strip()))):
        raise ValueError('invalid lifecycle')
    proof={'chat_id':message.chat_id,'reply_message_id':message.message_id,'reply_to_id':message.reply_to_id,
            'number':number,'state':observed['state']}
    if observed['state']=='closed': proof['updated_at']=observed['updated_at']
    if alias is not None: proof['target_source_id']=alias['answer_id']
    return proof


def _finish_comment_proven(pipeline, chat_id, message_id, number, comment_id):
    from .rules_pending_store import RulesHalt
    try:
        changed=pipeline.rules_pending.finish_comment(chat_id,message_id,comment_id)
        with pipeline.store._lock:
            row=pipeline.store.connection.execute('SELECT repo,issue_number,status,result FROM rules_comments WHERE chat_id=? AND message_id=?',(chat_id,message_id)).fetchone()
        success=type(comment_id) is int and comment_id>0
        expected=(pipeline.config.gh_repo,number,'done' if success else 'ambiguous',comment_id if success else None)
        if not changed or row is None or tuple(row)!=expected:
            raise RulesHalt('comment outcome unresolved; Owner inspection required')
    except RulesHalt:
        raise
    except Exception as error:
        raise RulesHalt('comment outcome unresolved; Owner inspection required') from error


def _append_batch_group(pipeline,topic,members,decision,proofs,document_reads):
    from .models import Decision
    from .pipeline import Plan
    from .followups import _enrichment_comment, _source_body
    from .issue_format import telegram_link
    from .ticket_templates import active
    if not proofs or len({p['number'] for p in proofs}) != 1: raise ValueError('invalid append target')
    proof=proofs[0]; number=proof['number']
    authority=next(m for m in members if m.message_id==proof['reply_message_id'])
    current=_resolve_reply_target(pipeline,authority)
    expected_state = 'closed' if decision.state == 'APPEND_CLOSED' else 'open'
    if current != proof or current['state'] != expected_state:
        return Decision('pending_review','rule',case_key=topic,reason='lifecycle_lookup_failed')
    if pipeline.config.mode=='dry-run':
        result=Decision('chat','rule',case_key=topic,reason='dry_run_append')
        for member in members: pipeline.store.finish(member,result)
        return result
    pipeline._acknowledge(Plan(topic,members,authority,'rule','issue'))
    if not pipeline.rules_pending.reserve_comment(authority.chat_id,authority.message_id,pipeline.config.gh_repo,number,topic):
        return Decision('error','rule',case_key=topic,reason='comment_reserved')
    summary=(authority.text if decision.state=='APPEND_CLOSED' else
             '; '.join(field.value for field in decision.present.values()) or authority.text)
    body=_enrichment_comment(summary,'\n'.join(m.text for m in members),telegram_link(authority))
    for member in members:
        link=telegram_link(member)
        if member is not authority and link: body=_source_body(body,link)
    found=pipeline._images(topic,members,live=True,document_reads=document_reads) if any(m.has_attachment for m in members) else None
    if found:
        body += ''.join('\n\n!['+alt+']('+url+')' for alt,url in found.images)
        body += ''.join('\n\n['+name+']('+url+')' for name,url in found.documents)
        body += ''.join('\n\n'+note for note in found.notes)
    try: comment_id=pipeline.github.add_comment(number,body)
    except Exception: comment_id=None
    _finish_comment_proven(pipeline,authority.chat_id,authority.message_id,number,comment_id)
    if type(comment_id) is not int or comment_id<=0:
        _send_ops(pipeline,authority,topic,'error',active().render('group-replies.md','notice.comment_ambiguous'))
        return Decision('error','rule',case_key=topic,reason='comment_ambiguous')
    target=pipeline.store.get(authority.chat_id,proof.get('target_source_id',authority.reply_to_id))
    expected=f'https://github.com/{pipeline.config.gh_repo}/issues/{number}'
    _map_append_proven(pipeline,members,number,expected,target)
    if not _send_ops(pipeline,authority,topic,'other',active().render('group-replies.md','followup.updated')+'\n'+expected):
        from .rules_pending_store import RulesHalt
        raise RulesHalt('comment notice unavailable; Owner inspection required')
    pipeline.rules_pending.finish_append(authority.chat_id,authority.message_id,topic)
    pipeline.rules_pending.close(topic,'CLOSED')
    return Decision('followup','rule',case_key=target['case_key'],issue_number=number,issue_url=expected,reason=decision.rule)


def _map_append_proven(pipeline,members,number,expected,target):
    from .rules_pending_store import RulesHalt
    try:
        with pipeline.store._lock,pipeline.store.connection:
            for member in members:
                changed=pipeline.store.connection.execute("UPDATE messages SET decision='followup',issue_number=?,issue_url=?,case_key=?,group_key=? WHERE chat_id=? AND message_id=?",
                    (number,expected,target['case_key'],target['group_key'],member.chat_id,member.message_id)).rowcount
                mapped=pipeline.store.connection.execute('SELECT decision,issue_number,issue_url,case_key,group_key FROM messages WHERE chat_id=? AND message_id=?',(member.chat_id,member.message_id)).fetchone()
                if changed!=1 or mapped is None or tuple(mapped)!=('followup',number,expected,target['case_key'],target['group_key']):
                    raise RulesHalt('comment mapping unresolved; Owner inspection required')
    except RulesHalt:
        raise
    except Exception as error:
        raise RulesHalt('comment mapping unresolved; Owner inspection required') from error


def _create_single(pipeline, source, topic, decision, evidence_messages=None, members=None, document_reads=()):
    from .models import Classification
    from .pipeline import Plan
    from . import group_notices
    values = [decision.present[name].value for name in ('object','observed','work','problem','proposal') if name in decision.present]
    if not values or decision.intent not in ('bug','task','feedback'): raise ValueError('invalid ready facts')
    summary = '; '.join(values)[:200]
    classification = Classification(decision.intent,1.0,summary[:80],summary,'medium')
    case,group,members = pipeline.store.autonomy_case(list(members or [source]))
    plan = Plan(group,members,source,'rule','issue',classification,typed=True,reason=decision.rule,
                key_ids=tuple(m.message_id for m in members) if evidence_messages is None else (),
                evidence_messages=tuple(evidence_messages) if evidence_messages is not None else None,
                document_reads=tuple(document_reads))
    sender = pipeline.notice_sender
    pipeline.notice_sender = None
    pipeline._rules_warning_sender = sender
    try: result = pipeline._run(plan,case)
    finally:
        pipeline.notice_sender = sender
        pipeline._rules_warning_sender = None
    from dataclasses import replace
    result=replace(result,reaction_chat_id=source.chat_id,
                   reaction_targets=((source.chat_id,source.message_id),) if result.issue_number is not None else ())
    pipeline.store.close_case(case,result,0)
    text = group_notices.notice_text(result,(),pipeline.config.gh_repo)
    if text: _send_ops(pipeline,source,topic,'other',text)
    if result.issue_number:
        pipeline.rules_pending.close(topic,'CLOSED')
    return result


def _process_ops_reply(pipeline, message):
    from dataclasses import replace
    from .models import Decision
    from .store import _to_message
    from .thinking import decision_turn
    pending = pipeline.rules_pending
    if pending.is_deferred_ops_reply(message.chat_id,message.message_id):
        result=Decision('pending_review','rule',reason='rules_reply_deferred_during_effect')
        pipeline.store.finish(message,result)
        return result
    binding = pending.topic_for_ops_reply(message.chat_id,message.message_id)
    if binding is None:
        pipeline.store.finish(message,Decision('chat','rule',reason='unbound_ops_reply'))
        return Decision('chat','rule',reason='unbound_ops_reply')
    return _process_existing_topic(pipeline,message,*binding,is_ops=True)


def _process_existing_topic(pipeline,message,topic,asked,*,is_ops):
    from .models import Decision
    from .store import _to_message
    pending=pipeline.rules_pending
    if not is_ops and pending.add_source(topic,message.chat_id,message.message_id) is None:
        return Decision('pending_review','rule',case_key=topic,reason='pending_closed')
    snapshot = pending.topic_snapshot(topic)
    if snapshot is None or snapshot['status'] != 'OPEN': return Decision('pending_review','rule',case_key=topic,reason='pending_closed')
    refs = {ref:(chat_id,message_id) for ref,chat_id,message_id in snapshot['sources']}
    originals=[]
    for ref,(chat_id,message_id) in sorted(refs.items()):
        row = pipeline.store.get(chat_id,message_id)
        if row is None: raise RuntimeError('rules source missing')
        originals.append((ref,_to_message(row)))
    if not originals:
        raise RuntimeError('rules ops source missing')
    actuals=[m for _,m in originals]
    if any(m.has_image for m in actuals) and getattr(pipeline.classifier,'supports_vision',False):
        pipeline._prepared_images=pipeline.attachments.prepare_images(topic,actuals)
    try:
        return _process_ops_evaluate(pipeline,message,topic,asked,originals,snapshot,is_ops=is_ops)
    finally:
        if pipeline._prepared_images is not None:
            pipeline._prepared_images.close()
            pipeline._prepared_images=None


def _process_ops_evaluate(pipeline,message,topic,asked,originals,snapshot,*,is_ops):
    from dataclasses import replace
    from .models import Decision
    from .thinking import decision_turn
    pending=pipeline.rules_pending
    source = originals[0][1]
    lookup = {(m.chat_id,m.message_id):ref for ref,m in originals}
    model_sources = [replace(m,message_id=ref,reply_to_id=lookup.get((m.chat_id,m.reply_to_id))) for ref,m in originals]
    attempt = pending.reserve_attempt(topic,pipeline.rules_snapshot.limits['hold_max_retries'])
    if attempt is None: return _escalate(pipeline,source,topic)
    pipeline.store.finish(message,Decision('pending_review','rule',case_key=topic,reason='rules_extract_pending'))
    with decision_turn(pipeline,'rules:' + topic,topic,message.message_id):
        from . import document_reader, document_processing
        doc_sources=[(ref,m) for ref,m in originals if m.has_document]
        raw_docs=document_reader.read_documents([m for _,m in doc_sources],pipeline.attachments.downloader)
        documents=[dict(row,message_id=ref) for row,(ref,_) in zip(raw_docs,doc_sources)]
        if documents: documents=document_processing.prepare(documents,pipeline.classifier)
        unread=any(document_reader.text_intent(m) and row.get('read_status')!='read' for row,(_,m) in zip(documents,doc_sources))
        proof=document_reader.proof(documents)
        provenance={ref:m for ref,m in doc_sources}
        proof=[dict(row,message_id=provenance[row['message_id']].message_id,chat_id=provenance[row['message_id']].chat_id) for row in proof]
        pair_to_ref={(m.chat_id,m.message_id):ref for ref,m in originals}
        images={pair_to_ref[(m.chat_id,m.message_id)]:path for m,path in pipeline._prepared_images.accepted} if pipeline._prepared_images is not None else {}
        reply_target='bot_question' if is_ops else 'none'
        outcome=None if unread else evaluate_batch(pipeline.rules_snapshot,pipeline.classifier,model_sources,images=images,documents=documents,
                                                    reply_target_for=lambda _: reply_target,asked_counts=asked,allow_mixed=True)
    if outcome is None:
        pending.finish_attempt(topic,attempt)
        pending.record_decision(topic,pipeline.rules_snapshot.evaluate({},code_facts(model_sources,extraction_ok=False,reply_target=reply_target)))
        result=Decision('pending_review','rule',case_key=topic,reason='rules_hold')
        pipeline.store.finish(message,result)
        return result
    _,decision=outcome[0]
    if not pending.claim_topic(topic,snapshot):
        pending.finish_attempt(topic,attempt,success=True)
        result=Decision('pending_review','rule',case_key=topic,reason='rules_pending_changed')
        pipeline.store.finish(message,result)
        return result
    pending.record_decision(topic,decision)
    if decision.state == 'NEED_INFO':
        result=Decision('pending_review','rule',case_key=topic,reason=decision.rule)
        pipeline.store.finish(message,result)
        _question(pipeline,source,topic,decision)
        pending.release_topic(topic)
        pending.finish_attempt(topic,attempt,success=True)
        return result
    if decision.state == 'CHAT':
        result=Decision('chat','rule',case_key=topic,reason=decision.rule)
        pipeline.store.finish(message,result); pending.close(topic,'CLOSED')
        pending.release_topic(topic)
        pending.finish_attempt(topic,attempt,success=True)
        return result
    if decision.state == 'MEDIA_ONLY':
        result=Decision('pending_review','rule',case_key=topic,reason=decision.rule)
        pipeline.store.finish(message,result)
        pending.release_topic(topic)
        pending.finish_attempt(topic,attempt,success=True)
        return result
    if decision.state != 'READY': raise RuntimeError('rules decision integration incomplete')
    source_members=[m for _,m in originals if m.chat_id==source.chat_id]
    for member in source_members: _resume_source(pipeline,member)
    result=_create_single(pipeline,source,topic,decision,[m for _,m in originals],members=source_members,document_reads=proof)
    if result.decision == 'error':
        from .rules_pending_store import RulesHalt
        raise RulesHalt('unresolved ambiguous operation; Owner inspection required')
    if not pending.release_topic(topic):
        from .rules_pending_store import RulesHalt
        raise RulesHalt('pending effect claim unresolved; Owner inspection required')
    pipeline.store.finish(message,Decision('chat','rule',case_key=topic,reason='rules_reply_processed'))
    pending.finish_attempt(topic,attempt,success=True)
    return result


def _process_source_reply(pipeline,message):
    """Only an exact same-chat, repository-bound issue can be an APPEND target."""
    from dataclasses import replace
    from .decision_rules import Field
    from .models import Decision
    from .thinking import decision_turn
    binding=pipeline.rules_pending.topic_for_source_reply(message.chat_id,message.reply_to_id,pipeline.rules_snapshot.limits['pending_expiry_days'])
    if binding is not None:
        return _process_existing_topic(pipeline,message,*binding,is_ops=False)
    if message.has_image and getattr(pipeline.classifier,'supports_vision',False):
        pipeline._prepared_images=pipeline.attachments.prepare_images(f'{message.chat_id}:{message.message_id}',[message])
    try:
        return _process_source_reply_inner(pipeline,message)
    finally:
        if pipeline._prepared_images is not None:
            pipeline._prepared_images.close()
            pipeline._prepared_images=None


def _process_source_reply_inner(pipeline,message):
    from dataclasses import replace
    from .decision_rules import Field
    from .models import Decision
    from .thinking import decision_turn
    target=pipeline.store.get(message.chat_id,message.reply_to_id)
    number=target['issue_number'] if target is not None else None
    expected=f'https://github.com/{pipeline.config.gh_repo}/issues/{number}'
    if (target is None or type(number) is not int or number<=0 or target['issue_url']!=expected
            or target['decision'] not in ('issue','followup')):
        return _process_single_with_images(pipeline,message)
    topic=f'{message.chat_id}:{message.message_id}'; pending=pipeline.rules_pending
    pending.create(topic,message.chat_id,message.message_id)
    try:
        lifecycle=pipeline.github.lifecycle_snapshot(number)
        if (lifecycle.get('number')!=number or lifecycle.get('repo','').casefold()!=pipeline.config.gh_repo.casefold()
                or lifecycle.get('state') not in ('open','closed') or not lifecycle.get('observed_at')):
            raise ValueError('invalid lifecycle')
    except Exception:
        result=Decision('pending_review','rule',case_key=topic,reason='lifecycle_lookup_failed')
        pipeline.store.finish(message,result)
        return result
    if lifecycle['state']=='closed':
        result=Decision('pending_review','rule',case_key=topic,reason='closed_target_not_supported')
        pipeline.store.finish(message,result)
        return result
    attempt=pending.reserve_attempt(topic,pipeline.rules_snapshot.limits['hold_max_retries'])
    if attempt is None: return _escalate(pipeline,message,topic)
    pipeline.store.finish(message,Decision('pending_review','rule',case_key=topic,reason='rules_extract_pending'))
    with decision_turn(pipeline,'rules:' + topic,topic,message.message_id):
        from . import document_reader, document_processing
        documents=document_reader.read_documents([message],pipeline.attachments.downloader) if message.has_document else []
        if documents: documents=document_processing.prepare(documents,pipeline.classifier)
        unread=any(document_reader.text_intent(message) and row.get('read_status')!='read' for row in documents)
        images=pipeline._prepared_images.for_messages([message]) if pipeline._prepared_images is not None else {}
        outcome=None if unread else evaluate_batch(pipeline.rules_snapshot,pipeline.classifier,[message],images=images,documents=documents,reply_target_for=lambda _: 'ticket_open')
    if outcome is None:
        pending.finish_attempt(topic,attempt)
        result=Decision('pending_review','rule',case_key=topic,reason='rules_hold')
        pipeline.store.finish(message,result)
        return result
    _,decision=outcome[0]
    refs={pair:ref for ref,pair in pending.sources(topic).items()}
    present={k:Field(v.value,tuple(refs[(message.chat_id,mid)] for mid in v.source_ids)) for k,v in decision.present.items()}
    pending.record_decision(topic,replace(decision,present=present))
    if decision.state in ('ANALYZE', 'STATUS'):
        # Reply-specific read-only routing must run before lifecycle GET and
        # pending binding; until then do not send a misleading status.
        pending.finish_attempt(topic,attempt,success=True)
        result=Decision('pending_review','rule',case_key=topic,reason='rules_readonly_reply_pending')
        pipeline.store.finish(message,result)
        return result
    if decision.state == 'CHAT':
        result=Decision('chat','rule',case_key=topic,reason=decision.rule)
        pipeline.store.finish(message,result); pending.close(topic,'CLOSED'); pending.finish_attempt(topic,attempt,success=True)
        return result
    if decision.state != 'APPEND': raise RuntimeError('rules reply decision integration incomplete')
    try:
        latest=pipeline.github.lifecycle_snapshot(number)
        if (latest.get('number')!=number or latest.get('repo','').casefold()!=pipeline.config.gh_repo.casefold() or latest.get('state')!='open' or not latest.get('observed_at')):
            raise ValueError('lifecycle changed')
    except Exception:
        pending.finish_attempt(topic,attempt,success=True)
        result=Decision('pending_review','rule',case_key=topic,reason='lifecycle_lookup_failed')
        pipeline.store.finish(message,result)
        return result
    if pipeline.config.mode == 'dry-run':
        pending.finish_attempt(topic,attempt,success=True); pending.close(topic,'CLOSED')
        result=Decision('chat','rule',case_key=topic,reason='dry_run_append')
        pipeline.store.finish(message,result)
        return result
    from .pipeline import Plan
    pipeline._acknowledge(Plan(topic,[message],message,'rule','issue'))
    if not pending.reserve_comment(message.chat_id,message.message_id,pipeline.config.gh_repo,number,topic):
        return Decision('pending_review','rule',case_key=topic,reason='comment_reserved')
    from .followups import _enrichment_comment
    from .issue_format import telegram_link
    from .ticket_templates import active
    summary='; '.join(field.value for field in decision.present.values()) or message.text
    body=_enrichment_comment(summary,message.text,telegram_link(message))
    found=pipeline._images(topic,[message],live=True,document_reads=document_reader.proof(documents)) if message.has_attachment else None
    if found is not None:
        body += ''.join('\n\n!['+alt+']('+url+')' for alt,url in found.images)
        body += ''.join('\n\n['+name+']('+url+')' for name,url in found.documents)
        body += ''.join('\n\n'+note for note in found.notes)
    try: comment_id=pipeline.github.add_comment(number,body)
    except Exception: comment_id=None
    _finish_comment_proven(pipeline,message.chat_id,message.message_id,number,comment_id)
    if type(comment_id) is not int or comment_id <= 0:
        _send_ops(pipeline,message,topic,'error',active().render('group-replies.md','notice.comment_ambiguous'))
        pending.finish_attempt(topic,attempt,success=True)
        pending.close(topic,'ESCALATED')
        result=Decision('error','rule',case_key=topic,reason='comment_ambiguous')
        pipeline.store.finish(message,result)
        return result
    _map_append_proven(pipeline,[message],number,expected,target)
    if not _send_ops(pipeline,message,topic,'other',active().render('group-replies.md','followup.updated')+'\n'+expected):
        from .rules_pending_store import RulesHalt
        raise RulesHalt('comment notice unavailable; Owner inspection required')
    pending.finish_append(message.chat_id,message.message_id,topic)
    pending.close(topic,'CLOSED'); pending.finish_attempt(topic,attempt,success=True)
    return Decision('followup','rule',case_key=target['case_key'],issue_number=number,issue_url=expected,reason=decision.rule)


def _resume_source(pipeline, source):
    with pipeline.store._lock,pipeline.store.connection:
        row=pipeline.store.connection.execute('SELECT decision,github_called_at,case_key FROM messages WHERE chat_id=? AND message_id=?', (source.chat_id,source.message_id)).fetchone()
        if row is None or row['github_called_at'] is not None or row['case_key'] is not None: raise RuntimeError('rules source no longer available')
        if row['decision'] == 'pending_review':
            pipeline.store.connection.execute("UPDATE messages SET decision='classifying' WHERE chat_id=? AND message_id=?", (source.chat_id,source.message_id))
        elif row['decision'] != 'classifying': raise RuntimeError('rules source no longer available')


def _escalate(pipeline, source, topic):
    from .models import Decision
    from .ticket_templates import active
    if pipeline.config.mode != 'dry-run' and pipeline.notice_sender is None: return None
    text=active().render('group-replies.md','notice.hold_escalated',{'retry_count':str(pipeline.rules_snapshot.limits['hold_max_retries'])})
    attempted = _send_ops(pipeline,source,topic,'escalation',text)
    if attempted or pipeline.config.mode == 'dry-run':
        pipeline.rules_pending.close(topic,'ESCALATED')
    else:
        return None
    return Decision('pending_review','rule',case_key=topic,reason='rules_hold_escalated')


def sweep(pipeline, *, exclude_chat_ids=frozenset()):
    from .store import _to_message
    pending=pipeline.rules_pending
    pending.expire_due(pipeline.rules_snapshot.limits['pending_expiry_days'])
    results=[]
    for topic in pending.hold_topics():
        refs=pending.sources(topic)
        if not refs: continue
        pairs=[pair for _,pair in sorted(refs.items())]
        if pairs[0][0] in exclude_chat_ids: continue
        if topic.startswith('batch:'):
            rows=[pipeline.store.get(*pair) for pair in pairs]
            if any(row is None for row in rows): continue
            result=_process_batch(pipeline,[_to_message(row) for row in rows],topic)
            if result is not None: results.append(result)
            continue
        anchor=pipeline.store.get(*pairs[0])
        if anchor is None: continue
        comment_status=pending.comment_status(*pairs[0])
        if comment_status is not None:
            if pending.unresolved_ambiguity():
                from .rules_pending_store import RulesHalt
                raise RulesHalt('append completion unresolved; Owner inspection required')
            if comment_status!='done':
                from .ticket_templates import active
                _send_ops(pipeline,_to_message(anchor),topic,'error',active().render('group-replies.md','notice.comment_ambiguous'))
            pending.close(topic,'CLOSED' if comment_status=='done' else 'ESCALATED')
            continue
        if anchor['github_called_at'] is not None:
            # A POST was reserved: never call the model or POST again after a crash.
            from .ticket_templates import active
            _send_ops(pipeline,_to_message(anchor),topic,'error',active().render('group-replies.md','notice.error'))
            pending.close(topic,'ESCALATED')
            continue
        row=pipeline.store.get(*pairs[-1])
        if row is None: continue
        message=_to_message(row)
        result=(_process_ops_reply(pipeline,message) if message.chat_id == pipeline.config.telegram.ops_chat_id
                else _process_batch(pipeline,[message]) if pipeline.rules_snapshot.extensions
                else _process_source_reply(pipeline,message) if message.reply_to_id is not None
                else _process_single_pending(pipeline,message))
        if message.reply_to_id is not None and pipeline.rules_snapshot.extensions and result is not None and result.decision != 'pending_review':
            pending.close(topic,'CLOSED')
        if result is not None: results.append(result)
    return results


def _question(pipeline, source, topic, decision):
    from . import group_notices
    from .issue_format import telegram_link
    from .ticket_templates import active
    if pipeline.config.mode == 'dry-run' or pipeline.notice_sender is None: return
    link = telegram_link(source)
    if not link: return
    templates = active()
    text = '\n'.join(templates.render('group-replies.md',block) for block in decision.questions)
    _send_ops(pipeline,source,topic,'question',text,decision.asked_fields)


def attachment_halt_warning(pipeline, messages):
    """Best effort one-time warning; never turn an unknown result into a retry."""
    from .store import _to_message
    from .ticket_templates import active
    from .rules_pending_store import RulesHalt
    topic=None; anchor=None
    for message in messages:
        binding=pipeline.rules_pending.topic_for_source_reply(message.chat_id,message.message_id,
                            pipeline.rules_snapshot.limits['pending_expiry_days'])
        if binding:
            topic=binding[0]
            refs=pipeline.rules_pending.sources(topic)
            pair=refs.get(1)
            anchor=pipeline.store.get(*pair) if pair else None
            if anchor: break
    if not anchor: return
    sender=pipeline.notice_sender
    if sender is None: pipeline.notice_sender=getattr(pipeline,'_rules_warning_sender',None)
    try:
        _send_ops(pipeline,_to_message(anchor),topic,'error',active().render('group-replies.md','notice.attachment_ambiguous'))
    except RulesHalt:
        pass  # The notice reservation itself remains unknown and blocks restart.
    finally:
        pipeline.notice_sender=sender


def _send_ops(pipeline, source, topic, kind, body, fields=()):
    from . import group_notices
    from .issue_format import telegram_link
    from .ticket_templates import active
    if pipeline.config.mode == 'dry-run' or pipeline.notice_sender is None: return False
    link = telegram_link(source)
    if not link: return False
    templates=active()
    header = templates.render('group-replies.md','ops.header',{
        'source_chat_name':group_notices.clean_question(source.chat_title or 'không rõ nhóm'),
        'source_link':link,'topic_id':group_notices.clean_question(topic),
        'sender_name':group_notices.clean_question(source.sender_name)})
    text = header + '\n' + body
    sequence = pipeline.rules_pending.reserve_notice(topic,pipeline.config.telegram.ops_chat_id,kind,fields,
                 max_asks_per_field=pipeline.rules_snapshot.limits['max_asks_per_field'])
    if sequence is None: return False
    try: notice_id = pipeline.notice_sender(pipeline.config.telegram.ops_chat_id,None,text)
    except Exception: notice_id = None
    from .rules_pending_store import RulesHalt
    try: recorded = pipeline.rules_pending.finish_notice(topic,sequence,notice_id)
    except Exception: raise RulesHalt('notice outcome is unresolved; Owner inspection required') from None
    if not recorded or type(notice_id) is not int or notice_id <= 0:
        raise RulesHalt('notice outcome is unresolved; Owner inspection required')
    return True

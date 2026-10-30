"""Opt-in configured-model smoke. All GitHub/Telegram tools and SQLite are isolated fakes.

Run with the listener venv/user and --config /etc/tg-triage/listener.toml. Only the already
configured model may receive synthetic text under its existing consent settings. Never
opens the Telegram session or the configured live DB; never prints credentials/transcripts.
"""
import argparse
from contextlib import closing, contextmanager
from dataclasses import replace
from datetime import datetime, timezone
import json
import logging
import sys
import time
import base64
import hashlib
import re
from pathlib import Path
from unittest.mock import patch

from tg_triage.config import load_config, AggregationConfig
from tg_triage.models import IncomingMessage, TopicGroup
from tg_triage.pipeline import Pipeline
from tg_triage.__main__ import _build_classifier
from tg_triage.store import Store
from tg_triage.thinking import decision_turn
from tg_triage import classifier as classifier_module
from tg_triage import thinking_transport as transport_module
from tg_triage import thinking as thinking_module
from tg_triage.group_notices import clean_question

CASE_NAMES=('all','three_messages_one_request','three_short_independent_requests',
            'three_clear_independent_requests','ancillary_vs_third_request','image_caption_context','pos_print_context')


@contextmanager
def budget_experiment(call_seconds=None,turn_seconds=None):
    """Reviewed, bounded experiment in this fake-tool process; never changes the daemon."""
    if call_seconds is None and turn_seconds is None:
        yield
        return
    if (call_seconds,turn_seconds) not in ((20,40),(30,60)):
        raise ValueError('Unsupported paired experimental budget')
    class ExperimentalTurn(thinking_module.Turn):
        def __init__(self,*args,**kwargs):
            super().__init__(*args,**kwargs)
            self.deadline=self.started+turn_seconds

        def attempt(self,body,*,consume=True):
            super().attempt(body,consume=consume)
            return min(call_seconds,self.remaining())
    with patch.object(thinking_module,'Turn',ExperimentalTurn):
        yield


def _size(value):
    return len(value) if isinstance(value,bytes) else len(value.encode('utf-8',errors='replace')) if isinstance(value,str) else None


class DiagnosticUsage:
    def __init__(self):
        self.attempts=[]

    def record(self,provider,model,role,outcome,usage,latency_ms):
        self.attempts.append(dict(provider=provider,model=model,role=role,outcome=outcome,
            latency_ms=round(latency_ms,2),prompt_tokens=usage.get('prompt_tokens',0),
            completion_tokens=usage.get('completion_tokens',0)))


class DiagnosticJSON:
    """Opt-in transport-module proxy: retain only lengths, status and schema shapes."""
    def __init__(self):
        self.events=[]

    def __getattr__(self,name):
        return getattr(json,name)

    def loads(self,value,*args,**kwargs):
        phase='wire_envelope' if isinstance(value,bytes) else 'model_content'
        event=dict(phase=phase,input_bytes=_size(value))
        try:
            result=json.loads(value,*args,**kwargs)
        except json.JSONDecodeError as error:
            event.update(outcome='invalid_json',error_position=error.pos,error_kind=error.msg)
            self.events.append(event)
            raise
        event.update(outcome='parsed_json',value_type=type(result).__name__)
        if phase=='wire_envelope' and isinstance(result,dict):
            choices=result.get('choices')
            choice=choices[0] if isinstance(choices,list) and choices and isinstance(choices[0],dict) else {}
            message=choice.get('message',result.get('message',{}))
            event['content_bytes']=_size(message.get('content')) if isinstance(message,dict) else None
            finish=choice.get('finish_reason')
            event['finish_reason']=finish if finish in ('stop','length','content_filter','tool_calls',None) else 'other'
        elif isinstance(result,dict):
            event['groups_count']=len(result['groups']) if isinstance(result.get('groups'),list) else None
        self.events.append(event)
        return result


class FakeGitHub:
    def __init__(self,repo):
        self.repo=repo; self.created=[]; self.comments=[];self.updates=[]

    def create_issue(self,issue):
        self.created.append(issue)
        number=100+len(self.created)
        return number,f'https://github.com/{self.repo}/issues/{number}'

    def add_comment(self,number,body):
        self.comments.append((number,body))
        return 800+len(self.comments)

    def refresh_issue(self,number,expected_sha,expected_title,marker,issue,*,expected_labels):
        current=next((value for n,value in reversed(self.updates) if n==number),self.created[number-101])
        if (hashlib.sha256(current.body.encode('utf-8')).hexdigest()!=expected_sha or current.title!=expected_title
                or marker not in current.body or marker not in issue.body or set(current.labels)!=set(expected_labels)):
            raise ValueError('Fake managed snapshot conflict')
        self.updates.append((number,issue));return number


def startup_provider(cfg,store,usage=None):
    provider=_build_classifier(cfg,store)
    if usage is not None:
        provider.usage=usage
        if getattr(provider,'_fallback',None) is not None:provider._fallback.usage=usage
    return provider


class DiagnosticProvider:
    def __init__(self,provider,usage):
        self.provider=provider
        self.usage=usage
        self.groups=[]
        self.parses=[]
        self.transport_json=DiagnosticJSON()
        self.call_latency_ms=[]
        self.image_requests=[]
        self.image_group_wording=[]
        self.followup_proposals=[]

    def __getattr__(self,name):
        return getattr(self.provider,name)

    def _observe_complete(self,options):
        original_complete=transport_module.complete
        expected_images={mid:hashlib.sha256(Path(path).read_bytes()).hexdigest()
                         for mid,path in (options.get('images') or {}).items()}
        def complete(*args,**kwargs):
            payloads=[]
            for m in args[3]:
                payloads.extend(m.get('images',[]))
                if isinstance(m.get('content'),list):
                    payloads.extend(p['image_url']['url'].split(',',1)[1] for p in m['content']
                        if isinstance(p,dict) and p.get('type')=='image_url')
            hashes=[hashlib.sha256(base64.b64decode(p,validate=True)).hexdigest() for p in payloads]
            users=[m for m in args[3] if m.get('role')=='user']
            bundled=(len(users)==1 and bool(payloads) and bool(users[0].get('content')))
            text_messages=[]
            for m in args[3]:
                safe={k:v for k,v in m.items() if k!='images'}
                if isinstance(safe.get('content'),list):
                    safe['content']=[part for part in safe['content'] if part.get('type')!='image_url']
                text_messages.append(safe)
            self.image_requests.append(dict(provider=args[0],image_count=len(hashes),image_sha256=hashes,
                user_message_count=len(users),single_context_bundle=bundled,
                expected_source_sha256=expected_images,expected_images_sent=bool(hashes) and set(hashes)==set(expected_images.values()),
                text_envelope_bytes=thinking_module.request_text_bytes(dict(messages=text_messages,format=args[4]))))
            return original_complete(*args,**kwargs)
        return complete

    def analyze_followup(self,originals,reply,tickets,history,**options):
        started=time.monotonic()
        with patch.object(transport_module,'json',self.transport_json), \
             patch.object(transport_module,'complete',self._observe_complete(options)):
            result=self.provider.analyze_followup(originals,reply,tickets,history,**options)
        self.call_latency_ms.append(round((time.monotonic()-started)*1000,2))
        analysis=result.get('case_analysis') if isinstance(result,dict) else None
        self.followup_proposals.append(None if result is None else dict(
            action=result.get('action'),confidence=result.get('confidence'),
            source_roles=[dict(message_id=r.get('message_id'),role=r.get('role'),confidence=r.get('confidence'))
                          for r in result.get('source_roles',[])],
            case_analysis_present=isinstance(analysis,dict),
            key_message_ids=analysis.get('key_message_ids',[]) if isinstance(analysis,dict) else [],
            summary=clean_question(analysis.get('summary','') if isinstance(analysis,dict) else result.get('summary','')),
            error_text=clean_question(analysis.get('details',{}).get('error_text','')) if isinstance(analysis,dict) else ''))
        return result

    def classify_topics(self,messages,**options):
        original=classifier_module.parse_topic_groups
        expected_images=options.get('images') or {}
        complete=self._observe_complete(options)
        def parse(content):
            value=original(content)
            event=dict(outcome='valid_groups' if value is not None else 'invalid_output',content_bytes=_size(content))
            try:
                data=json.loads(classifier_module.strip_thinking(content))
                groups=data.get('groups') if isinstance(data,dict) else None
                event['groups_type']=type(groups).__name__
                if isinstance(groups,list):
                    fields=('message_ids','category','type','actionable','verdict','title','summary','priority','confidence','missing_info')
                    event['group_shapes']=[{k:type(g.get(k)).__name__ for k in fields} if isinstance(g,dict)
                        else dict(group_type=type(g).__name__) for g in groups[:10]]
            except (ValueError,TypeError):
                event['content_json']='invalid'
            self.parses.append(event)
            if expected_images and value is not None:
                self.image_group_wording.extend(dict(ids=g.message_ids,type=g.type,category=g.category,
                    title=clean_question(g.title),summary=clean_question(g.summary),
                    missing_questions=[clean_question(q) for q in g.missing_info]) for g in value
                    if set(g.message_ids)&set(expected_images))
            return value
        started=time.monotonic()
        # Only this standalone, fake-tool process is instrumented; daemon modules are untouched.
        with patch.object(transport_module,'json',self.transport_json), patch.object(classifier_module,'parse_topic_groups',parse), \
             patch.object(transport_module,'complete',complete):
            value=self.provider.classify_topics(messages,**options)
        self.call_latency_ms.append(round((time.monotonic()-started)*1000,2))
        self.groups.append(None if value is None else [dict(ids=g.message_ids,verdict=g.verdict,
            category=g.category,type=g.type,actionable=g.actionable,confidence=g.confidence,
            priority=g.priority,missing_count=len(g.missing_info),title_len=len(g.title),summary_len=len(g.summary)) for g in value])
        return value


def msg(mid,text,**extra):
    fields=dict(chat_id=-1009999999999,message_id=mid,text=text,sender_id=7,
        sender_name='Synthetic user',received_at=datetime.now(timezone.utc),mentions_listener=False)
    fields.update(extra)
    return IncomingMessage(**fields)


def pos_print_smoke(cfg,image_path,sources_fixture):
    if image_path is None or sources_fixture is None:
        raise ValueError('POS smoke requires reviewed image and source fixture')
    pixels=Path(image_path).read_bytes()
    if not pixels.startswith(b'\xff\xd8\xff'):raise ValueError('JPEG fixture required')
    fixture=json.loads(Path(sources_fixture).read_text(encoding='utf-8'))
    sources=fixture.get('sources',fixture.get('messages',[]))
    if [s['message_id'] for s in sources]!=[448,449,450,451,452,454]:
        raise ValueError('Unexpected POS fixture sources')
    if any(s.get('has_image') for s in sources[:5]) or not sources[-1].get('has_image'):
        raise ValueError('Unexpected POS fixture media binding')
    cfg=replace(cfg,aggregation=AggregationConfig(5,5,40))
    class Upload:
        def ensure_release(self,tag):pass
        def upload(self,tag,path,asset):return f'https://github.com/{cfg.gh_repo}/releases/download/synthetic/{asset}'
    def download(chat,mid,directory):
        if mid!=454:return None
        path=Path(directory)/'pos.jpg';path.write_bytes(pixels);return path
    with closing(Store(':memory:')) as store:
        usage=DiagnosticUsage();provider=DiagnosticProvider(startup_provider(cfg,store,usage),usage)
        gh=FakeGitHub(cfg.gh_repo);notices=[]
        p=Pipeline(cfg,store,provider,gh,downloader=download,uploader=Upload(),
            notice_sender=lambda c,m,t:notices.append((m,clean_question(t))) or 1000+m)
        for source in sources:
            extra=dict(reply_to_id=source.get('reply_to_id'),is_forward=bool(source.get('is_forward')),
                has_image=bool(source.get('has_image')),image_size=len(pixels) if source.get('has_image') else None,
                image_mime='image/jpeg' if source.get('has_image') else None)
            if source.get('received_at'):extra['received_at']=datetime.fromisoformat(source['received_at'])
            p.process(msg(source['message_id'],source['text'],**extra))
        initial=gh.created[0] if gh.created else None
        final=gh.updates[-1][1] if gh.updates else initial
        job=store.connection.execute('SELECT plan FROM followup_jobs ORDER BY id DESC LIMIT 1').fetchone()
        analysis=(json.loads(job[0]).get('case_analysis',{}) if job and job[0] else {})
        environment=analysis.get('details',{}).get('environment','').casefold()
        generated=(analysis.get('title','')+' '+analysis.get('summary','')).casefold()
        known=('app' in environment and 'web' in environment and 'có dây' in environment)
        print_scope=bool(re.search(r'\bpos\b',generated) and re.search(r'\b(?:in|print|printing)\b',generated))
        error='Nothing was sent to the printer. Try printing again.'
        error_grounded=bool(final and error.casefold() in final.body.casefold())
        image_sent=any(r['expected_images_sent'] and r['single_context_bundle'] for r in provider.image_requests)
        initial_groups=provider.groups[0] if provider.groups else None
        initial_clear=bool(initial_groups) and sum(g['verdict']=='ticket' for g in initial_groups)==1 and all(
            g['missing_count']==0 for g in initial_groups if g['verdict']=='ticket')
        valid=(len(gh.created)==1 and len(gh.updates)==1 and gh.updates[0][0]==101
            and initial_clear and known and print_scope and error_grounded and image_sent
            and final is not None and '### 🐞 Lỗi' in final.body and '![' in final.body
            and [m for m,_ in notices]==[448,454])
        return dict(case='pos_print_context',pass_=valid,actual_startup_factory=True,
            tickets=len(gh.created),refreshes=len(gh.updates),comment_count=len(gh.comments),
            reply_targets=[m for m,_ in notices],known_app_web_wired=known,visible_error_grounded=error_grounded,
            generated_pos_print_scope=print_scope,
            generated_title=clean_question(final.title) if final else None,
            generated_summary=clean_question(analysis.get('summary','')),
            generated_environment=clean_question(analysis.get('details',{}).get('environment','')),
            generated_error_text=clean_question(analysis.get('details',{}).get('error_text','')),
            visible_error_text=error if error_grounded else None,initial_groups=initial_groups,
            image_requests=provider.image_requests,fixture_image_sha256=hashlib.sha256(pixels).hexdigest(),
            diagnostics=dict(attempts=usage.attempts,transport_json=provider.transport_json.events,
                parses=provider.parses,semantic_calls_latency_ms=provider.call_latency_ms,
                followup_proposals=provider.followup_proposals),
            reason_codes=[json.loads(r[0])['reason_code'] for r in store.connection.execute('SELECT record FROM thinking_decisions')])


def run(config_path,case='all',image_path=None,sources_fixture=None):
    if case not in CASE_NAMES:
        raise ValueError('Unsupported smoke case')
    # Schema-only identity placeholders are sufficient: no Telegram/GitHub client is created.
    cfg=load_config(config_path,validate_only=True)
    cfg=replace(cfg,mode='live',thinking_enabled=True,aggregation=AggregationConfig(5,3,40))
    results=[]
    if case=='pos_print_context':results.append(pos_print_smoke(cfg,image_path,sources_fixture))
    if case=='image_caption_context' and image_path is None:
        raise ValueError('Image path required')
    # Clear work requires issue; abbreviated requests test partition/reply even when details
    # legitimately require clarification. Labels are fixed before the provider run.
    for name,texts,expected,clear in [
        ('three_messages_one_request',['Nút lưu báo cáo bị lỗi khi bấm.','Ở cùng màn hình báo cáo, bấm lưu không có phản hồi.','Nhờ team sửa lỗi nút lưu báo cáo vừa nêu.'],1,True),
        ('three_short_independent_requests',['Nút lưu báo cáo bị lỗi, nhờ sửa.','Yêu cầu khác: thêm bộ lọc ngày cho danh sách khách hàng.','Yêu cầu độc lập: sửa stop opt out trong automation.'],3,False),
        ('three_clear_independent_requests',['Ở màn hình báo cáo, bấm Lưu không có phản ứng; nhờ sửa để lưu báo cáo thành công.',
            'Yêu cầu riêng: ở danh sách khách hàng, thêm bộ lọc từ ngày đến ngày theo ngày tạo để chỉ hiện khách trong khoảng đã chọn.',
            'Lỗi độc lập ở automation: đã bật Stop opt out nhưng hệ thống vẫn gửi tin cho khách opt out; cần ngừng gửi cho các khách này.'],3,True)]:
        if case not in ('all',name):
            continue
        with closing(Store(':memory:')) as store:
            gh=FakeGitHub(cfg.gh_repo); notices=[]
            usage=DiagnosticUsage()
            provider=DiagnosticProvider(startup_provider(cfg,store,usage),usage)
            p=Pipeline(cfg,store,provider,gh,notice_sender=lambda c,m,t:notices.append(m) or 1000+m)
            for i,text in enumerate(texts,1):
                p.process(msg(i,text))
            records=[json.loads(r[0]) for r in store.connection.execute('SELECT record FROM thinking_decisions')]
            expected_replies=[1] if expected==1 else [1,2,3]
            decisions=[store.get(-1009999999999,i)['decision'] for i in range(1,4)]
            proposals=provider.groups[-1] if provider.groups else None
            valid=(len(gh.created)==expected and notices==expected_replies
                   and all(r['status']=='verified' and r['budget']['model_calls_used']<=2 for r in records)
                   and all(d=='issue' if clear else d in ('issue','needs_human') for d in decisions)
                   and proposals is not None and len(proposals)==expected
                   and all(g['missing_count']>0 for g in proposals if not clear and g['verdict']=='needs_human'))
            results.append(dict(case=name,pass_=valid,tickets=len(gh.created),reply_targets=notices,
                model_attempts=sum(r['budget']['model_calls_used'] for r in records),
                record_statuses=[r['status'] for r in records],reason_codes=[r['reason_code'] for r in records],
                group_proposals=provider.groups,expected_tickets=expected,expected_clear_issue=clear,
                diagnostics=dict(attempts=usage.attempts,transport_json=provider.transport_json.events,
                    parses=provider.parses,semantic_calls_latency_ms=provider.call_latency_ms),
                source_decisions=[dict(id=i,decision=store.get(-1009999999999,i)['decision'],
                    reason=store.get(-1009999999999,i)['reason'],confidence=store.get(-1009999999999,i)['confidence']) for i in range(1,4)]))
    if case in ('all','ancillary_vs_third_request'):
        with closing(Store(':memory:')) as store:
            provider=startup_provider(cfg,store)
            p=Pipeline(cfg,store,provider)
            with decision_turn(p,'synthetic-role','synthetic-root',904):
                value=provider.analyze_source_roles([dict(ordinal=0,message_id=901,text='nên thêm ký tự cho tên biến'),
                    dict(ordinal=1,message_id=903,text='sửa stop opt out trong automation')],
                    [msg(902,'ảnh ở trên minh họa thêm thôi'),msg(904,'Yêu cầu khác: sửa trang thanh toán không lưu được')])
            roles={r['message_id']:r['role'] for r in (value or {}).get('source_roles',[])}
            results.append(dict(case='ancillary_vs_third_request',pass_=roles=={902:'ancillary',904:'request'},roles=roles))
    if image_path is not None and case in ('all','image_caption_context'):
        pixels=Path(image_path).read_bytes()
        if not pixels.startswith(b'\xff\xd8\xff'):
            raise ValueError('JPEG fixture required')
        sources=(json.loads(Path(sources_fixture).read_text(encoding='utf-8'))['sources'] if sources_fixture else [
            dict(message_id=442,text='Sau này mình cho chế độ nầy thì mình sẽ design đổi bảng nầy',has_image=True),
            dict(message_id=443,text=('Câu hỏi thông tin ASP.NET: JWT và refresh token hoạt động như thế nào? '
                'Trả lời: access token có hạn; refresh token cấp token mới. ')*30),
            dict(message_id=444,text='dạ được anh',has_image=False)])
        if (len(sources)!=3 or {s['message_id'] for s in sources}!={442,443,444}
                or not sources[0].get('has_image') or any(s.get('has_image') for s in sources[1:])):
            raise ValueError('Unexpected fixture source shape')
        class Upload:
            def ensure_release(self,tag):pass
            def upload(self,tag,path,asset):return f'https://github.com/{cfg.gh_repo}/releases/download/synthetic/{asset}'
        def download(chat,mid,directory):
            if mid!=442:return None
            target=Path(directory)/'source.jpg';target.write_bytes(pixels);return target
        with closing(Store(':memory:')) as store:
            usage=DiagnosticUsage();provider=DiagnosticProvider(startup_provider(cfg,store,usage),usage)
            gh=FakeGitHub(cfg.gh_repo);notices=[]
            p=Pipeline(cfg,store,provider,gh,downloader=download,uploader=Upload(),
                notice_sender=lambda c,m,t:notices.append((m,clean_question(t))) or 1000+m)
            for source in sources:
                extra=dict(reply_to_id=source.get('reply_to_id'),is_forward=bool(source.get('is_forward')),
                    has_image=bool(source.get('has_image')),image_size=len(pixels) if source.get('has_image') else None,
                    image_mime='image/jpeg' if source.get('has_image') else None)
                if source.get('received_at'):extra['received_at']=datetime.fromisoformat(source['received_at'])
                p.process(msg(source['message_id'],source['text'],**extra))
            wording=provider.image_group_wording
            generated=' '.join(g['title']+' '+g['summary']+' '+' '.join(g['missing_questions']) for g in wording)
            lower=generated.casefold()
            grounded=('salon' in lower and ('visibility' in lower or 'hiển thị' in lower)
                      and ('chat only' in lower or 'chỉ chat' in lower or 'chỉ trò chuyện' in lower))
            valid=(len(gh.created)==1 and [n[0] for n in notices]==[442] and grounded
                and store.get(-1009999999999,442)['decision']=='issue'
                and all(store.get(-1009999999999,i)['decision'] in ('chat','noise') for i in (443,444))
                and any(r['expected_images_sent'] and r['single_context_bundle'] for r in provider.image_requests)
                and bool(wording) and all(set(g['ids'])=={442} and g['category'] in ('task','feedback')
                    and not g['missing_questions'] for g in wording))
            results.append(dict(case='image_caption_context',pass_=valid,tickets=len(gh.created),
                source_decisions={i:store.get(-1009999999999,i)['decision'] for i in (442,443,444)},
                reply_targets=[n[0] for n in notices],generated_image_groups=wording,grounded_panel_mode=grounded,
                image_requests=provider.image_requests,attempts=usage.attempts,
                diagnostics=dict(transport_json=provider.transport_json.events,parses=provider.parses,
                    semantic_calls_latency_ms=provider.call_latency_ms),
                fixture_image_sha256=hashlib.sha256(pixels).hexdigest()))
    return dict(status='PASS' if all(r['pass_'] for r in results) else 'FAIL',
        kind='small synthetic configured-model smoke, not accuracy benchmark',cases=results,
        actual_startup_factory=True,
        model=dict(provider=cfg.model.provider,name=cfg.model.name,max_output=cfg.model.max_output,
            configured_timeout_seconds=cfg.model.timeout_seconds,semantic_call_limit_seconds=20,turn_limit_seconds=40),
        real_github_writes=0,real_telegram_messages=0,live_database_opened=False)


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--config',required=True)
    parser.add_argument('--case',choices=CASE_NAMES,default='all')
    parser.add_argument('--experiment-call-seconds',type=int,choices=(20,30))
    parser.add_argument('--experiment-turn-seconds',type=int,choices=(40,60))
    parser.add_argument('--image-path')
    parser.add_argument('--sources-fixture')
    args=parser.parse_args()
    logging.disable(logging.CRITICAL)
    try:
        with budget_experiment(args.experiment_call_seconds,args.experiment_turn_seconds):
            report=run(args.config,args.case,args.image_path,args.sources_fixture)
        if args.experiment_call_seconds is not None:
            report['experimental_budget']=dict(call_seconds=args.experiment_call_seconds,
                turn_seconds=args.experiment_turn_seconds,production_runtime_changed=False)
            report['model']['semantic_call_limit_seconds']=args.experiment_call_seconds
            report['model']['turn_limit_seconds']=args.experiment_turn_seconds
    except Exception as error:
        report=dict(status='FAIL',error_type=type(error).__name__,real_github_writes=0,real_telegram_messages=0)
    print(json.dumps(report,ensure_ascii=False))
    sys.exit(0 if report['status']=='PASS' else 1)

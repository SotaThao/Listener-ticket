"""Opt-in real configured model, with fake GitHub/Telegram and memory SQLite only.

Never opens the live DB/session. Fixtures are supplied explicitly, not bundled.
"""
import argparse
from contextlib import closing
from dataclasses import replace
import importlib.util
import json
import logging
from pathlib import Path
from unittest.mock import patch
from datetime import timedelta
from tg_triage.models import CaseAnalysis

_spec=importlib.util.spec_from_file_location('_thinking_smoke',Path(__file__).with_name('telegram-thinking-model-smoke.py'))
old=importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(old)


class Provider(old.DiagnosticProvider):
    def __init__(self,provider,usage):
        super().__init__(provider,usage)
        self.ingress=[]

    def analyze_ingress(self,sources,candidates,**options):
        with patch.object(old.transport_module,'complete',self._observe_complete(options)):
            result=self.provider.analyze_ingress(sources,candidates,**options)
        self.ingress.append(None if result is None else [dict(
            action=g['action'],ids=g['message_ids'],candidate_id=g['candidate_id'],
            type=g['analysis'].classification.type if g['analysis'].classification else None,
            title=old.clean_question(g['analysis'].classification.title) if g['analysis'].classification else '',
            summary=old.clean_question(g['analysis'].classification.summary) if g['analysis'].classification else '',
            details={k:v for k,v in g['analysis'].details},missing=list(g['analysis'].missing_info)) for g in result])
        return result


    def analyze_followup(self,*args,**options):
        result=super().analyze_followup(*args,**options)
        if result is not None and self.followup_proposals[-1] is not None:
            self.followup_proposals[-1]['lifecycle']=result.get('lifecycle')
        return result


class GitHub(old.FakeGitHub):
    def __init__(self,repo):
        super().__init__(repo)
        self.states={}

    def lifecycle_snapshot(self,number):
        return dict(number=number,repo=self.repo,state=self.states.get(number,'open'),
                    observed_at='2026-10-16T00:00:00+00:00',updated_at='2026-10-16T00:00:00Z')

    def verify_created_issue(self,number,issue):
        if not 101<=number<101+len(self.created) or self.created[number-101]!=issue:
            raise ValueError('Fake readback mismatch')
        return number


def cfg_for(path):
    cfg=old.load_config(path,validate_only=True)
    return replace(cfg,mode='live',thinking_enabled=True,autonomous_enabled=True,topic_memory_enabled=True)


def run_case(cfg,name,messages,images=None,reply_photo=None,lifecycle=False,seed_old=False):
    images=images or {}
    downloaded_image_ids=[]
    class Upload:
        def ensure_release(self,tag): pass
        def upload(self,tag,path,asset):
            return f'https://github.com/{cfg.gh_repo}/releases/download/synthetic/{asset}'
    def download(chat,mid,directory):
        pixels=images.get(mid)
        if pixels is None:return None
        downloaded_image_ids.append(mid)
        path=Path(directory)/f'{mid}.jpg';path.write_bytes(pixels);return path
    with closing(old.Store(':memory:')) as store:
        usage=old.DiagnosticUsage()
        provider=Provider(old.startup_provider(cfg,store,usage),usage)
        gh=GitHub(cfg.gh_repo);notices=[]
        def notice(chat,mid,text):
            notices.append((mid,old.clean_question(text)))
            return 9000+len(notices)
        pipeline=old.Pipeline(cfg,store,provider,gh,notice_sender=notice,downloader=download,uploader=Upload())
        historical_before={}
        if seed_old:
            class PendingSeed:
                def analyze_ingress(self,sources,candidates,**options):
                    ids=[m.message_id for m in sources]
                    return [dict(message_ids=ids,action='CLARIFY',candidate_id=None,confidence=.9,
                        question='Anh/Chị bổ sung thông tin giúp Em nhé?',
                        analysis=CaseAnalysis('needs_human',None,tuple(ids),actionable=False))]
            pipeline.classifier=PendingSeed(); pipeline.notice_sender=None
            for mid,text,has_image in ((457,'Login đúng pass mà không login được',False),
                    (458,'Sao tính tiền',False),(461,'Invalid username or password',True)):
                pipeline.process(old.msg(mid,text,has_image=has_image,is_forward=True,
                    received_at=messages[0].received_at-timedelta(hours=3)))
                historical_before[mid]=dict(store.get(messages[0].chat_id,mid))
            pipeline.classifier=provider; pipeline.notice_sender=notice
        for m in messages:store.enqueue(m)
        pipeline.process(messages[0])
        initial_count=len(gh.created)
        if reply_photo:
            reply=replace(reply_photo,reply_to_id=9001)
            pipeline.process(reply)
        if lifecycle and gh.created:
            gh.states[101]='closed'
            pipeline.process(old.msg(502,'Hôm qua đã kiểm tra POS in được trên app và web; hôm nay lỗi lại không in được ticket.',reply_to_id=9001))
            if len(gh.created)>1:
                gh.states[102]='open'
                pipeline.process(old.msg(503,'Bổ sung cho lần lỗi hôm nay: máy in có dây, lỗi xảy ra trên app và web.',reply_to_id=9000+len(notices)))
        final=gh.updates[-1][1] if gh.updates else (gh.created[-1] if gh.created else None)
        drafts=[g for turn in provider.ingress if turn for g in turn]
        image_sent=any(r['expected_images_sent'] and r['single_context_bundle'] for r in provider.image_requests)
        result=dict(case=name,tickets=len(gh.created),initial_tickets=initial_count,
            reply_targets=[m for m,_ in notices],notices=[t for _,t in notices],comments=[n for n,_ in gh.comments],refreshes=[n for n,_ in gh.updates],
            generated_drafts=drafts,followup_proposals=provider.followup_proposals,
            image_requests=provider.image_requests,attempts=usage.attempts,
            downloaded_image_source_ids=sorted(set(downloaded_image_ids)),
            reason_codes=[json.loads(r[0]).get('reason_code') for r in store.connection.execute('SELECT record FROM thinking_decisions')])
        historical_unchanged=all(dict(store.get(messages[0].chat_id,mid))==snapshot for mid,snapshot in historical_before.items())
        if seed_old:
            result['historical_sources_unchanged']=historical_unchanged
        if name=='three_to_one':valid=len(gh.created)==1 and result['reply_targets']==[1]
        elif name=='three_to_three':valid=len(gh.created)==3 and result['reply_targets']==[1,2,3]
        elif name=='image46':
            generated=' '.join(g['title']+' '+g['summary']+' '+str(g['details']) for g in drafts if 442 in g['ids']).casefold()
            valid=(len(gh.created)==1 and result['reply_targets']==[442] and image_sent
                and 'salon' in generated and ('chat only' in generated or 'chỉ chat' in generated or 'chỉ trò chuyện' in generated)
                and all(store.get(messages[0].chat_id,i)['decision'] in ('chat','noise') for i in (443,444))
                and all(g['type']=='task' and g['missing']==[] for g in drafts if 442 in g['ids']))
        elif name=='pos47':
            job=store.connection.execute('SELECT plan FROM followup_jobs ORDER BY id DESC LIMIT 1').fetchone()
            analysis=(json.loads(job[0]).get('case_analysis') or {}) if job and job[0] else {}
            detail_values=analysis.get('details',{})
            if isinstance(detail_values,list):detail_values=dict(detail_values)
            environment=str(detail_values.get('environment','')).casefold()
            error='Nothing was sent to the printer. Try printing again.'
            original=store.get(messages[0].chat_id,448)
            context_ids=[i for i in range(448,453) if store.get(messages[0].chat_id,i)['case_key']==original['case_key'] and store.get(messages[0].chat_id,i)['decision'] not in ('chat','noise')]
            generated_error=str(detail_values.get('error_text',''))
            valid=(len(gh.created)==1 and bool(gh.updates) and result['reply_targets']==[448,454]
                and image_sent and final is not None and error.casefold() in final.body.casefold()
                and 'app' in environment and 'web' in environment and 'có dây' in environment
                and set((448,449,450,451))<=set(context_ids)
                and error.casefold() in generated_error.casefold()
                and all(f'https://t.me/c/9999999999/{i}' in final.body for i in (448,449,450,451)))
            result['context_ids']=context_ids
            result['generated_error']=old.clean_question(generated_error)
            result['generated_environment']=old.clean_question(environment)
            result['error_grounded']=bool(final and error.casefold() in final.body.casefold())
        elif name=='closed_recurrence':
            episode=store.connection.execute('SELECT issue_number FROM followup_episodes').fetchone()
            valid=(len(gh.created)==2 and episode is not None and episode[0]==102
                and '/issues/101' in gh.created[1].body and 502 in result['reply_targets']
                and (any(n==102 for n,_ in gh.comments) or any(n==102 for n,_ in gh.updates)))
        elif name in ('booking_incident','booking_with_independent_login'):
            booking=next((g for g in drafts if 463 in g['ids']),None)
            generated=' '.join((booking['title'],booking['summary'],str(booking['details']))) if booking else ''
            related=store.get(messages[0].chat_id,463)
            members=[i for i in (457,458,461,463,464,465,466,467)
                if store.get(messages[0].chat_id,i) and store.get(messages[0].chat_id,i)['case_key']==related['case_key']]
            expected_tickets=2 if name=='booking_with_independent_login' else 1
            valid=(len(gh.created)==expected_tickets and 463 in result['reply_targets'] and image_sent
                and set(members)=={463,464,465,466} and booking is not None
                and booking['action']=='NEW' and booking['candidate_id'] is None
                and 'common_validation_error' in generated.casefold()
                and 'voice' in generated.casefold() and 'web' in generated.casefold()
                and ('booking' in generated.casefold() or 'đặt lịch' in generated.casefold())
                and not any(word in generated.casefold() for word in ('invalid username','login','đăng nhập'))
                and historical_unchanged
                and (result['reply_targets']==[463] if expected_tickets==1 else sorted(result['reply_targets'])==[463,467]))
            result['booking_source_ids']=members
        elif name=='emotion_alone':
            valid=len(gh.created)==0 and not notices and all(g['action']=='CHAT' for g in drafts)
        else:raise ValueError('Unknown case')
        result['pass_']=bool(valid)
        return result


def run(args):
    cfg=cfg_for(args.config)
    results=[]
    if args.case in ('all','grouping'):
        results.append(run_case(cfg,'three_to_one',[
            old.msg(1,'Nút Lưu màn hình báo cáo không phản ứng khi bấm, nhờ team sửa để lưu được.'),
            old.msg(2,'Vẫn cùng lỗi nút Lưu đó: bấm không có phản hồi.'),
            old.msg(3,'Yêu cầu là sửa lỗi nút Lưu báo cáo vừa nêu.')]))
        results.append(run_case(cfg,'three_to_three',[
            old.msg(1,'Nút Lưu báo cáo không phản ứng; nhờ sửa để lưu thành công.'),
            old.msg(2,'Yêu cầu riêng: thêm bộ lọc từ ngày đến ngày cho danh sách khách hàng.'),
            old.msg(3,'Lỗi độc lập: Stop opt out đang bật nhưng automation vẫn gửi tin; cần ngừng gửi cho khách opt out.')]))
    if args.case in ('all','image46'):
        if not args.image46:raise ValueError('Reviewed image46 fixture required')
        pixels=Path(args.image46).read_bytes()
        results.append(run_case(cfg,'image46',[
            old.msg(442,'Sau này mình cho chế độ nầy thì mình sẽ design đổi bảng nầy',has_image=True,image_size=len(pixels),image_mime='image/jpeg',is_forward=True),
            old.msg(443,'Câu hỏi thông tin ASP.NET: JWT và refresh token hoạt động như thế nào? Trả lời: access token có hạn; refresh token cấp token mới.',is_forward=True),
            old.msg(444,'dạ được anh',is_forward=True)],{442:pixels}))
    if args.case in ('all','pos47'):
        if not args.image47:raise ValueError('Reviewed image47 fixture required')
        pixels=Path(args.image47).read_bytes()
        texts=['hôm nay là 1 ngày gặp vấn đề nữa POS không print được ticket và receipt ai phụ trách giúp anh xem tại sao',
            'Anh đang sử dụng app hay web anh?','cả 2','vừa app và web app anh dùng print có dây','Dạ để em check']
        results.append(run_case(cfg,'pos47',[old.msg(i,t,is_forward=True) for i,t in enumerate(texts,448)],
            {454:pixels},old.msg(454,'Đây là hình ảnh',has_image=True,image_size=len(pixels),image_mime='image/jpeg')))
    if args.case in ('all','lifecycle'):
        results.append(run_case(cfg,'closed_recurrence',[
            old.msg(501,'POS không in được ticket trên app và web dù máy in có dây; nhờ sửa để in được.')],lifecycle=True))
    if args.case=='booking_incident':
        if not args.image_booking:raise ValueError('Reviewed booking image fixture required')
        pixels=Path(args.image_booking).read_bytes()
        pixels_by_id={463:pixels}
        if args.image_old_login:
            # Explicitly supplied synthetic sign-in error, never a real account image.
            pixels_by_id[461]=Path(args.image_old_login).read_bytes()
        when=old.datetime(2026,10,16,7,54,16,tzinfo=old.timezone.utc)
        sources=([old.msg(463,'',has_image=True,image_size=len(pixels),image_mime='image/jpeg',is_forward=True,received_at=when)]+
            [old.msg(mid,text,is_forward=True,received_at=when) for mid,text in
             ((464,'Kiểu nầy chết luôn'),(465,'AI voice không gửi link booking'),(466,'Khách vào web không book được luôn'))])
        results.append(run_case(cfg,'booking_incident',sources,pixels_by_id,seed_old=True))
        results.append(run_case(cfg,'booking_with_independent_login',sources+[
            old.msg(467,'Yêu cầu riêng, độc lập với sự cố đặt lịch: đăng nhập app quản trị bằng đúng mật khẩu vẫn không vào được.',
                    is_forward=True,received_at=when)],pixels_by_id,seed_old=True))
        results.append(run_case(cfg,'emotion_alone',[old.msg(464,'Kiểu nầy chết luôn',is_forward=True)],seed_old=True))
    return dict(status='PASS' if all(r['pass_'] for r in results) else 'FAIL',cases=results,
        kind='configured-model isolated small corpus, not accuracy benchmark',real_github_writes=0,
        real_telegram_messages=0,live_database_opened=False,actual_startup_factory=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--config',required=True)
    parser.add_argument('--case',choices=('all','grouping','image46','pos47','lifecycle','booking_incident'),default='all')
    parser.add_argument('--image46');parser.add_argument('--image47');parser.add_argument('--image-booking')
    parser.add_argument('--image-old-login',help='Optional SAFE SYNTHETIC sign-in error fixture; no credentials')
    args=parser.parse_args()
    logging.disable(logging.CRITICAL)
    try:
        report=run(args)
    except Exception as error:
        report=dict(status='ERROR',exception=type(error).__name__,real_github_writes=0,real_telegram_messages=0,live_database_opened=False)
    print(json.dumps(report,ensure_ascii=False,indent=2))
    raise SystemExit(0 if report['status']=='PASS' else 1)

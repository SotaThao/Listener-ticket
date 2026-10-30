"""Configured-provider regression with synthetic chat text and fake side effects only.

Direct Pipeline calls are immediate; the live-worker clock contract is tested separately.
Never opens a Telegram session or live SQLite database.
"""
import argparse
from contextlib import closing
from dataclasses import replace
from datetime import timedelta
import importlib.util
import json
import logging
from pathlib import Path

spec=importlib.util.spec_from_file_location('_autonomous_smoke',Path(__file__).with_name('telegram-autonomous-model-smoke.py'))
smoke=importlib.util.module_from_spec(spec);spec.loader.exec_module(smoke)
old=smoke.old


def run(config_path):
    cfg=smoke.cfg_for(config_path);reports=[]
    with closing(old.Store(':memory:')) as store:
        usage=old.DiagnosticUsage();provider=smoke.Provider(old.startup_provider(cfg,store,usage),usage)
        gh=smoke.GitHub(cfg.gh_repo);notices=[]
        def send(chat,mid,text):notices.append((mid,old.clean_question(text)));return 9000+len(notices)
        p=old.Pipeline(cfg,store,provider,gh,notice_sender=send)
        roots=[]
        for ordinal,(start,count,text) in enumerate(((1,7,'POS không in ticket/receipt trên app và web với máy in có dây.'),(101,1,'Thiết kế bảng Salon information & visibility cho Chat only.'),(201,6,'Automation vẫn gửi tin khi Stop opt out bật.'))):
            ids=list(range(start,start+count))
            roots.append(dict(candidate_id='c'+str(ordinal+1),pending=False,exact_reply=False,split=False,
                pending_questions=[],current_scopes=[dict(ordinal=0,issue_number=44+ordinal,source_ids=ids,human_bindings={},unresolved_ids=[],scope_status='known')],
                messages=[old.msg(i,text+' Bằng chứng trao đổi trước đó. '*12) for i in ids],key='synthetic:'+str(start),
                source=dict(root_key='synthetic:'+str(start),case_key='synthetic:'+str(start),issue_number=44+ordinal)))
        store.autonomy_candidates=lambda *args:roots
        inputs=[old.msg(457,'Login đúng pass mà không login được'),old.msg(458,'Sao tính tiền'),
            old.msg(461,'Ảnh bổ sung cho lỗi đăng nhập: màn hình Sign in to NEXORA báo Invalid username or password.',reply_to_id=457)]
        for m in inputs:store.enqueue(m)
        p.process(inputs[0])
        login_case=store.get(inputs[0].chat_id,457)['case_key']
        valid=(len(gh.created)==1 and any(mid==457 and 'tạo ticket:' in text for mid,text in notices)
            and store.get(inputs[0].chat_id,461)['case_key']==login_case
            and store.get(inputs[0].chat_id,458)['case_key']!=login_case
            and 'invalid username or password' in gh.created[0].body.casefold()
            and any(g['type']=='bug' and g['missing']==[] and 457 in g['ids'] and 461 in g['ids'] for t in provider.ingress if t for g in t)
            and any(a['outcome']=='ok' for a in usage.attempts))
        reports.append(dict(case='login_late_caption_with_history_pressure',pass_=bool(valid),tickets=len(gh.created),reply_targets=[m for m,_ in notices],attempts=usage.attempts,generated=provider.ingress))
    with closing(old.Store(':memory:')) as store:
        usage=old.DiagnosticUsage();provider=smoke.Provider(old.startup_provider(cfg,store,usage),usage)
        gh=smoke.GitHub(cfg.gh_repo);notices=[]
        def send(chat,mid,text):notices.append((mid,old.clean_question(text)));return 9000+len(notices)
        p=old.Pipeline(cfg,store,provider,gh,notice_sender=send)
        first=old.msg(601,'Màn hình này cần xử lý');p.process(first)
        p.process(replace(old.msg(602,'Màn hình Sign in to NEXORA. Login đúng pass mà không login được; báo Invalid username or password.',reply_to_id=9001),received_at=first.received_at+timedelta(seconds=90)))
        valid=(len(gh.created)==1 and len(notices)==2 and [m for m,_ in notices]==[601,601]
            and 'tạo ticket:' in notices[-1][1] and 'invalid username or password' in gh.created[0].body.casefold())
        reports.append(dict(case='pending_reply_after_90_seconds',pass_=bool(valid),tickets=len(gh.created),reply_targets=[m for m,_ in notices],attempts=usage.attempts,generated=provider.ingress))
    return dict(status='PASS' if all(r['pass_'] for r in reports) else 'FAIL',cases=reports,
        real_github_writes=0,real_telegram_messages=0,live_database_opened=False,
        kind='Synthetic timeline/direct Pipeline; no real delay or login credentials; small regression corpus')


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--config',required=True);args=parser.parse_args()
    logging.disable(logging.CRITICAL)
    try:report=run(args.config)
    except Exception as error:report=dict(status='ERROR',exception=type(error).__name__)
    print(json.dumps(report,ensure_ascii=False,indent=2));raise SystemExit(0 if report['status']=='PASS' else 1)

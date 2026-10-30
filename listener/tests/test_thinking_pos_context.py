"""POS intake budget regression through default production prompts and provider paths."""
import json
import unittest
from dataclasses import asdict,replace
from datetime import timedelta
from unittest.mock import patch

from tg_triage.classifier import TOPIC_SCHEMA,load_prompt
from tg_triage.__main__ import _build_classifier
from tg_triage.pipeline import Pipeline
from tg_triage.provider import build_provider
from tg_triage.store import Store
from tg_triage.thinking import pack_messages,request_text_bytes
from tests.helpers import config,message,topic,NOW
from tests.test_followups import GitHub


POS_TEXTS=(
    'hôm nay là 1 ngày gặp vấn đề nữa POS không print được. ticket và reciept ai phụ trách giúp anh xem tại sao',
    'Anh đang xử dụng app hay web anh?',
    'cả 2',
    'vưa app và web app anh dung print có dây',
    'Dạ để em check')


def unpack(packet):
    if 'source_columns' not in packet:
        return packet['sources']
    result=[]
    for values in packet['sources']:
        row=dict(zip(packet['source_columns'],values))
        metadata=dict(packet['source_defaults'],**row.pop('metadata'))
        result.append(dict(metadata,**row))
    return result


class POSContextTests(unittest.TestCase):
    def sources(self,n=5):
        return [message(448+i,POS_TEXTS[i] if i<5 else 'Thông tin tiếp theo',
            sender_id=1603321775,sender_name='Thảo Trương',is_forward=True,
            received_at=NOW+timedelta(seconds=i*9),mentions_listener=False,
            reply_to_id=448 if i==6 else None) for i in range(n)]

    def test_exact_five_pos_messages_use_default_hosted_pipeline_one_bug_first_reply(self):
        cfg=config('live',window=2,max_messages=5)
        cfg=replace(cfg,model=replace(cfg.model,provider='deepseek',name='deepseek-flash',think=False,
            cloud_consent=True,fallback='none'))
        store=Store(':memory:',clock=lambda:NOW);self.addCleanup(store.close)
        gh=GitHub();sent=[]
        provider=_build_classifier(cfg,store)
        p=Pipeline(cfg,store,provider,gh,
            notice_sender=lambda c,m,t:sent.append((m,t)) or 1000+m)
        group=topic(tuple(range(448,453)),category='bug',type='bug',title='POS không in ticket và receipt',
            summary='Không in trên cả app và web, sử dụng máy in có dây.',actionable=True,
            details={'environment':'App và web; máy in có dây','actual':'Không in ticket và receipt'})
        with patch('tg_triage.ai_transport.read_key',return_value='fake'), \
             patch.object(provider,'_cloud_messages',wraps=provider._cloud_messages) as cloud_messages, \
             patch('tg_triage.thinking_transport.complete',return_value={'groups':[asdict(group)]}) as request:
            for source in self.sources():p.process(source)
        self.assertEqual(request.call_count,1)
        messages=request.call_args.args[3]
        self.assertEqual([m['role'] for m in messages],['system','user'])
        packet=json.loads(cloud_messages.call_args.args[0][1]['content'])
        self.assertEqual([r['text'] for r in unpack(packet)],list(POS_TEXTS))
        sent_packet=json.loads(messages[1]['content'])
        self.assertEqual(unpack(sent_packet)[0]['sender_id'],1603321775)
        self.assertEqual(len(gh.calls),1);self.assertEqual([m for m,_ in sent],[448])
        self.assertIn('máy in có dây',gh.calls[0].body)
        record=json.loads(store.connection.execute('SELECT record FROM thinking_decisions').fetchone()[0])
        self.assertEqual(record['budget']['model_calls_used'],1)
        self.assertNotEqual(record['reason_code'],'context_budget_exhausted')

    def test_previous_startup_default_prompt_injection_exhausted_five_message_budget(self):
        cfg=config('live',window=2,max_messages=5)
        cfg=replace(cfg,model=replace(cfg.model,provider='deepseek',name='deepseek-flash',think=False,
            cloud_consent=True,fallback='none'))
        store=Store(':memory:',clock=lambda:NOW);self.addCleanup(store.close)
        previous=build_provider(cfg,topics_prompt=load_prompt('topics.md'))
        p=Pipeline(cfg,store,previous,GitHub())
        with patch('tg_triage.thinking_transport.complete') as request:
            for source in self.sources():p.process(source)
        request.assert_not_called()
        record=json.loads(store.connection.execute('SELECT record FROM thinking_decisions').fetchone()[0])
        self.assertEqual(record['budget']['model_calls_used'],0)
        self.assertEqual(record['reason_code'],'context_budget_exhausted')

    def test_startup_validates_active_prompts_without_changing_legacy_override_behavior(self):
        store=Store(':memory:');self.addCleanup(store.close)
        for enabled,names in ((True,['topics.md','thinking-topics.md']),(False,['topics.md'])):
            with self.subTest(enabled=enabled), patch('tg_triage.__main__.load_prompt') as load:
                provider=_build_classifier(replace(config(),thinking_enabled=enabled),store)
                self.assertFalse(provider._topics_override)
                self.assertEqual([c.args[0] for c in load.call_args_list],names)
        with patch('tg_triage.__main__.load_prompt',side_effect=FileNotFoundError('missing')):
            with self.assertRaises(FileNotFoundError):_build_classifier(config(),store)
        with patch('tg_triage.__main__.load_prompt',side_effect=['installed legacy',FileNotFoundError('thinking missing')]):
            with self.assertRaises(FileNotFoundError):_build_classifier(config(),store)

    def test_six_and_twenty_sources_fit_without_losing_mandatory_metadata(self):
        for n in (6,20):
            with self.subTest(count=n):
                sources=self.sources(n)
                rendered=pack_messages(load_prompt('thinking-topics.md'),sources,{},TOPIC_SCHEMA)
                self.assertLessEqual(request_text_bytes(dict(messages=rendered,format=TOPIC_SCHEMA)),8000)
                packet=json.loads(rendered[1]['content']);rows=unpack(packet)
                self.assertEqual(packet['source_ids'],[m.message_id for m in sources])
                for original,row in zip(sources,rows):
                    self.assertEqual(row['message_id'],original.message_id)
                    self.assertEqual(row['id_ref'],f'[{original.message_id}]')
                    self.assertEqual(row['sender_id'],original.sender_id)
                    self.assertEqual(row['sender_name'],original.sender_name)
                    self.assertEqual(row['received_at'],original.received_at.isoformat())
                    self.assertEqual(row['reply_to_id'],original.reply_to_id)
                    self.assertEqual(row['is_forward'],original.is_forward)
                    self.assertEqual(row['has_image'],False)
                    self.assertEqual(row['has_document'],False)
                    self.assertEqual(row['pixels_available'],False)
                    self.assertEqual(row['text'],original.text)
                    self.assertFalse(row['text_truncated'])

    def test_structured_cloud_redaction_preserves_identity_numbers_and_removes_text_contacts(self):
        cfg=config();cfg=replace(cfg,model=replace(cfg.model,provider='deepseek',name='deepseek-flash',
            think=False,cloud_consent=True,cloud_image_consent=True))
        provider=build_provider(cfg)
        sources=self.sources(20)
        sources[0]=replace(sources[0],message_id=1234567800,text='POS lỗi; gọi 0912345678 hoặc pos@example.com')
        sources[6]=replace(sources[6],reply_to_id=1234567800)
        rendered=pack_messages(load_prompt('thinking-topics.md'),sources,{},TOPIC_SCHEMA)
        wire=provider._cloud_messages(rendered)
        packet=json.loads(wire[1]['content']);rows=unpack(packet)
        self.assertEqual(rows[0]['sender_id'],1603321775)
        self.assertEqual(rows[6]['reply_to_id'],1234567800)
        self.assertEqual(packet['source_ids'],[1234567800]+list(range(449,468)))
        self.assertEqual(rows[0]['id_ref'],'[1234567800]')
        self.assertIn('[phone]',rows[0]['text']);self.assertIn('[email]',rows[0]['text'])
        self.assertNotIn('0912345678',wire[1]['content']);self.assertNotIn('pos@example.com',wire[1]['content'])
        raw='{"phone":0912345678}'
        self.assertIn('[phone]',provider._cloud_messages([dict(role='user',content=raw)])[0]['content'])
        imitation=json.dumps({'source_ids':[1603321775],'sources':[{'phone':1603321775}]})
        self.assertIn('"phone": [phone]',provider._cloud_messages([dict(role='user',content=imitation)])[0]['content'])


if __name__=='__main__':unittest.main()

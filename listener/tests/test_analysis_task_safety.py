"""Task projection safety; fixtures do not establish real-model visual correctness."""
from unittest import TestCase
from tg_triage.analysis_task import valid_task,valid_payload,render


def task(**values):
    base=dict(title='Điều chỉnh hiển thị',work='Giữ nội dung trong vùng giao diện.',
              acceptance=['Nội dung hiển thị đầy đủ.'])
    return dict(base,**values)


class TaskProjectionSafetyTests(TestCase):
    def test_complete_max_fields_fit_three_lines_without_truncation(self):
        value=task(title='A'*90,work='B'*160,acceptance=['C'*90,'D'*90])
        self.assertTrue(valid_task(value))
        report=render(value)
        self.assertEqual(3,len(report.splitlines()))
        self.assertLessEqual(len(report),500)
        for field in (value['title'],value['work'],*value['acceptance']):
            self.assertIn(field,report)

    def test_sanitization_expansion_cannot_publish_partial_field(self):
        value=task(title='x'*80+' a.co')
        self.assertLessEqual(len(value['title']),90)
        self.assertFalse(valid_task(value))
        with self.assertRaises(ValueError):
            render(value)

    def test_duplicate_criteria_after_sanitization_rejected(self):
        self.assertFalse(valid_task(task(acceptance=['Open one.example','Open two.example'])))
        self.assertFalse(valid_task(task(acceptance=['One',' one '])))

    def test_task_cannot_fall_back_to_legacy_or_publish_choices(self):
        base=dict(action='ANALYZE',candidate_id=None,answer='Legacy body')
        for value in (None,{},False,task(title='\u200b')):
            self.assertFalse(valid_payload(dict(base,task=value)))
        self.assertTrue(valid_payload(base))
        self.assertTrue(valid_payload(dict(base,answer='',task=task())))
        self.assertFalse(valid_payload(dict(base,task=task(),options=['A','B'],needs_choice=True)))
        self.assertFalse(valid_payload(dict(base,task=task(),action='NEW')))

    def test_injected_lines_are_single_line_fields(self):
        value=task(work='Do this\n2. Extra line')
        self.assertTrue(valid_task(value))
        report=render(value)
        self.assertEqual(3,len(report.splitlines()))
        self.assertIn('Do this 2. Extra line',report)
    def test_work_over_hard_cap_is_rejected_without_truncation(self):
        self.assertTrue(valid_task(task(work='X'*160)))
        for size in (161,167):
            value=task(work='X'*size)
            self.assertFalse(valid_task(value))
            with self.assertRaises(ValueError):
                render(value)



class CloudIngressLocalViewTests(TestCase):
    def test_cloud_failure_or_no_consent_keeps_local_full_contact_excerpt(self):
        import json
        from dataclasses import replace
        from unittest.mock import patch
        from tests.helpers import config,message,FakeHTTP
        from tests.test_document_content_contract import new_wire
        from tg_triage.provider import CloudProvider
        text='Email support@example.invalid in the public footer.'
        for consent in (False,True):
            with self.subTest(consent=consent):
                cfg=config()
                cfg=replace(cfg,model=replace(cfg.model,provider='openai',name='test',
                    cloud_consent=consent,fallback='ollama',base_url='https://api.openai.com/v1'))
                http=FakeHTTP({'message':{'content':json.dumps(new_wire([100]))}})
                provider=CloudProvider(cfg,opener=http)
                with patch.object(provider,'_ask_cloud',return_value=None):
                    self.assertIsNotNone(provider.analyze_ingress([message(100,text=text)],[]))
                body=json.loads(http.calls[0][0].data)
                self.assertNotIn('_local_messages',body)
                packet=json.loads(body['messages'][1]['content'])
                self.assertEqual(text,packet['sources'][0][packet['source_columns'].index('text')])


class ReviewCurrentPriorityTests(TestCase):
    def test_semantic_review_reclaims_optional_history_before_current_scope(self):
        import json
        from datetime import timedelta
        from tests.helpers import config,message,FakeHTTP,NOW
        from tests.test_document_content_contract import new_wire
        from tg_triage.classifier import Classifier
        pages=['/section-'+str(i) for i in range(7)]
        text='Change this shared component. '+'Requirement detail. '*14+' Apply to '+','.join(pages)
        sources=[message(100,text=text,has_image=True)] + [message(i,text='Separate current component request.',has_document=True,document_name='long-document-name-'+str(i)+'.pdf',document_mime='application/pdf',document_size=1234) for i in range(101,108)]
        first={'groups':new_wire([100])['groups']+new_wire(list(range(101,108)))['groups']}
        http=FakeHTTP({'message':{'content':json.dumps(first)}},
                      {'message':{'content':json.dumps({'reviews':[{'group_index':0,'verdict':'approve'},{'group_index':1,'verdict':'approve'}]})}})
        history=[message(i,text='Old unrelated context. '*100,received_at=NOW-timedelta(minutes=1)) for i in range(1,9)]
        provider=Classifier(config().ollama,opener=http)
        self.assertIsNotNone(provider.analyze_ingress(sources,[],read_context=history))
        self.assertEqual(2,len(http.calls))
        for request,_ in http.calls:
            body=json.loads(request.data)
            packet=json.loads(body['messages'][1]['content'])
            index=packet['source_columns'].index('text')
            self.assertEqual(text,packet['sources'][0][index])
            for page in pages:self.assertIn(page,packet['sources'][0][index])
            self.assertEqual(list(range(100,108)),packet['NEW_SOURCE_IDS'])
            if len(packet['READ_ONLY_CONTEXT'])<len(history):self.assertTrue(packet['read_context_truncated'])

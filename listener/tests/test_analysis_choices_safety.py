"""Focused sanitizer and cached-source choice safety regressions."""
from unittest import TestCase
from unittest.mock import patch
from tests.helpers import message
from tg_triage.analysis_choices import selection_group
from tg_triage.analysis_choices import valid_options, render


class AnalysisChoicesSafetyTests(TestCase):
    def test_options_that_collapse_after_redaction_are_rejected(self):
        for options in (["X https://one.example/path", "X https://two.example/path"],
                        ["Contact one@example.com", "Contact two@example.com"],
                        ["Open one.example", "Open two.example"],
                        ["Open tg://one", "Open tg://two"],
                        ["\u200b", "Visible"]):
            self.assertFalse(valid_options(options))

    def test_renderer_reserves_complete_options_and_footer(self):
        choices=["A"*180,"B"*180,"C"*180]
        report=render("x"*1500,choices)
        self.assertLessEqual(len(report),1500)
        for index,text in enumerate(choices,1):
            self.assertIn(str(index)+". "+text,report)
        self.assertIn("0 =",report)
        self.assertIn("1,2",report)

    def test_only_analysis_can_offer_choices(self):
        self.assertTrue(valid_options([],"NEW"))
        self.assertFalse(valid_options(["One","Two"],"NEW"))
        self.assertFalse(valid_options(["One"]))

    def test_cardinality_words_cannot_select_a_different_count(self):
        for text,count in [('cả hai',3),('cả 2',3),('cả ba',2),('cả 3',2)]:
            options=['Interpretation '+str(i) for i in range(count)]
            with patch('tg_triage.analysis_choices.offer',return_value=dict(anchor=1,options=options)):
                group=selection_group(None,message(5,text=text,reply_to_id=2),'repo/test')
            self.assertEqual(options,group['options'])
            self.assertIn('chưa hợp lệ',group['answer'])

    def test_new_work_reply_is_not_absorbed_as_numeric_selection(self):
        with patch('tg_triage.analysis_choices.offer',return_value=dict(anchor=1,options=['One','Two'])):
            group=selection_group(None,message(5,text='1 phần lỗi login mới',reply_to_id=2),'repo/test')
        self.assertIsNone(group)

    def test_newline_options_and_ack_share_single_line_projection(self):
        options=['A\n9. forged','B\r\n10) forged']
        report=render('Observation',options)
        self.assertNotIn('\n9.',report)
        self.assertNotIn('\n10)',report)
        with patch('tg_triage.analysis_choices.offer',return_value=dict(anchor=1,options=options)):
            group=selection_group(None,message(5,text='1,2',reply_to_id=2),'repo/test')
        self.assertNotIn('\n9.',group['answer'])
        self.assertNotIn('\n10)',group['answer'])
        self.assertIn('A 9. forged',group['answer'])
        self.assertFalse(group['needs_choice'])

    def test_explicit_decision_state_is_strict_but_legacy_infers(self):
        self.assertTrue(valid_options(['One','Two']))
        self.assertTrue(valid_options(['One','Two'],needs_choice=True))
        self.assertTrue(valid_options([],needs_choice=False))
        for flag in (None,1,0,'true',[]):
            self.assertFalse(valid_options(['One','Two'],needs_choice=flag))
        self.assertFalse(valid_options(['One','Two'],needs_choice=False))
        self.assertFalse(valid_options([],needs_choice=True))

    def test_fresh_producer_requires_task_but_legacy_parser_accepts(self):
        import json
        from tg_triage.classifier import Classifier
        from tg_triage.ingress_model import parse_ingress
        from tests.helpers import config,FakeHTTP
        group=dict(message_ids=[10],action='ANALYZE',candidate_id=None,confidence=.9,question='',
            answer='Quan sát: vùng nội dung có thể hiển thị chưa trọn vẹn.',
            analysis=dict(verdict='needs_human',category='chat',type='task',actionable=False,
                title='Quan sát giao diện',summary='Có vùng nội dung chưa rõ.',priority='low',
                confidence=.9,missing_info=[],reason='',key_message_ids=[10]))
        legacy=json.dumps(dict(groups=[group]))
        self.assertIsNotNone(parse_ingress(legacy))
        model=Classifier(config().ollama,opener=FakeHTTP({'message':{'content':legacy}}))
        self.assertIsNone(model.analyze_ingress([message(10)],[]))
        self.assertEqual('invalid_output',model.last_ingress_outcome)
        group['needs_choice']=False
        group['task']=dict(title='Điều chỉnh hiển thị',work='Giữ nội dung trong vùng giao diện.',
                           acceptance=['Nội dung hiển thị đầy đủ.'])
        model=Classifier(config().ollama,opener=FakeHTTP({'message':{'content':json.dumps(dict(groups=[group]))}}))
        self.assertIsNotNone(model.analyze_ingress([message(10)],[]))

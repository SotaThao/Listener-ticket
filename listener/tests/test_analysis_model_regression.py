"""Regressions from rejected hosted informational replies, no network."""
import json
import re
from unittest import TestCase
from tg_triage.ingress_model import INGRESS_SCHEMA, parse_ingress
from tg_triage.group_notices import analysis_text


def captured_group():
    return dict(message_ids=[2],action='ANALYZE',candidate_id=None,confidence=.62,
        question='',answer='',analysis=dict(category='task',type='task',actionable=True,
        verdict='needs_human',title='Yêu cầu tự động phân tích danh sách vấn đề trong nhóm',
        summary='Người dùng yêu cầu hệ thống tự phân tích và liệt kê các vấn đề trong nhóm.',
        priority='medium',confidence=.55,missing_info=['Phạm vi nhóm/kênh cần phân tích'],
        reason='Yêu cầu mang tính phân tích/tính năng',key_message_ids=[2,1]))


class HostedAnalysisRegressionTests(TestCase):
    def test_captured_empty_answer_remains_invalid(self):
        group=captured_group()
        self.assertIsNone(parse_ingress(json.dumps(dict(groups=[group]))))
        group['answer']='Quan sát: có nội dung quảng cáo; cần kiểm tra quy tắc nhóm.'
        self.assertIsNotNone(parse_ingress(json.dumps(dict(groups=[group]))))

    def test_structured_schema_requires_task(self):
        rule=INGRESS_SCHEMA['properties']['groups']['items']['allOf'][0]
        self.assertEqual('ANALYZE',rule['if']['properties']['action']['const'])
        self.assertIn('task',rule['then']['required'])
        schema=INGRESS_SCHEMA['properties']['groups']['items']['properties']['task']
        for field in ('title','work'):
            for value in ('',' \t\n','\u2003'):
                self.assertIsNone(re.search(schema['properties'][field]['pattern'],value))
        self.assertEqual({'title','work','acceptance'},set(schema['required']))

    def test_nonanalysis_legacy_shape_without_answer_remains_valid(self):
        group=captured_group();group['action']='CLARIFY';group.pop('answer')
        self.assertIsNotNone(parse_ingress(json.dumps(dict(groups=[group]))))

    def test_scope_prefix_never_asserts_unavailable_pixels(self):
        report=analysis_text('Quan sát: ảnh có nội dung quảng cáo.')
        self.assertIn('phần tin được cung cấp',report)
        self.assertNotIn('Ảnh thiếu pixel chưa được xác minh.',report)

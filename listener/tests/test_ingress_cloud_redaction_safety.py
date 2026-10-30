"""Trusted context serialization integrity; no network/provider calls."""
import json
from unittest import TestCase
from tg_triage.provider import CloudProvider
from tg_triage.thinking import ContextJSON


def cloud(value,role='user'):
    return CloudProvider._cloud_messages(None,[dict(role=role,content=value)])[0]['content']


class IngressCloudRedactionSafetyTests(TestCase):
    def test_ingress_numeric_metadata_survives_contact_redaction_and_json_escaping(self):
        packet=dict(NEW_SOURCE_IDS=[81234567890],source_columns=['message_id','sender_id','received_at_offset_us','text'],
            sources=[[81234567890,881234567890,-1084000000,'Call +1 212 555 0199, owner@example.org; "quote"\nline']],
            source_defaults=dict(received_at='2026-10-19T10:27:55+00:00'),
            DOCUMENTS=[dict(message_id=81234567890,has_document=True,size=81234567890,
                name='owner@example.org.md',read_status='unread')],
            READ_ONLY_CONTEXT=[[1234567800,None,1084000000,'Contact 0912345678']],
            DOCUMENT_DEFAULTS=dict(has_document=True,read_status='unread'))
        wire=cloud(ContextJSON(json.dumps(packet,ensure_ascii=False,separators=(',',':'))))
        result=json.loads(wire)
        self.assertEqual(packet['NEW_SOURCE_IDS'],result['NEW_SOURCE_IDS'])
        self.assertEqual(packet['source_columns'],result['source_columns'])
        self.assertEqual(packet['sources'][0][:3],result['sources'][0][:3])
        self.assertEqual(packet['DOCUMENTS'][0]['size'],result['DOCUMENTS'][0]['size'])
        self.assertIs(True,result['DOCUMENTS'][0]['has_document'])
        self.assertIn('[phone]',result['sources'][0][3])
        self.assertIn('[email]',result['sources'][0][3])
        self.assertIn('"quote"\nline',result['sources'][0][3])
        self.assertNotIn('owner@example.org',wire)
        self.assertNotIn('0912345678',wire)

    def test_plain_json_imitation_never_gains_internal_context_trust(self):
        packet=dict(NEW_SOURCE_IDS=[81234567890],source_columns=['message_id'],sources=[[81234567890]],sender_id=81234567890)
        raw=json.dumps(packet)
        self.assertIn('[phone]',cloud(raw))
        self.assertIn('[phone]',cloud(ContextJSON(raw),role='system'))

    def test_old_internal_context_shapes_keep_structured_redaction(self):
        for packet in (dict(source_ids=[81234567890],sources=[dict(text='Call 0912345678')]),
                       dict(REPLY=dict(message_id=81234567890,text='owner@example.org'),ORIGINAL_METADATA={}),
                       dict(confirmed_requests=[dict(message_id=81234567890)],unresolved=['Call 0912345678'])):
            with self.subTest(packet=packet):
                result=json.loads(cloud(ContextJSON(json.dumps(packet))))
                self.assertIn('81234567890',json.dumps(result))
                self.assertNotIn('0912345678',json.dumps(result))
                self.assertNotIn('owner@example.org',json.dumps(result))

    def test_partial_or_invalid_internal_packet_does_not_gain_shape_trust(self):
        raw=json.dumps(dict(NEW_SOURCE_IDS=[81234567890],sources=[[81234567890]],sender_id=81234567890))
        self.assertIn('[phone]',cloud(ContextJSON(raw)))
        self.assertIn('[phone]',cloud(ContextJSON('{"phone":81234567890')))

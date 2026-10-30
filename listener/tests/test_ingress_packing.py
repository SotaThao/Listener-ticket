import json
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from tg_triage.classifier import Classifier
from tg_triage.pipeline import Pipeline
from tg_triage.thinking import request_text_bytes
from tests.helpers import config, message, classification, FakeHTTP, FakeGitHub, JPG


def candidate(key, count, *, exact=False, start=1, pressure=0):
    ids=list(range(start,start+count))
    return dict(candidate_id=key,pending=False,exact_reply=exact,split=False,
        reply_source_ids=[900] if exact else [],
        current_scopes=[dict(ordinal=0,issue_number=44,source_ids=ids,human_bindings={},
            unresolved_ids=[],scope_status='known')],pending_questions=['Known question '+('x'*pressure)] if pressure else [],
        messages=[message(i,text='Previously discussed interface context. '*20) for i in ids])


def envelope(mid, candidate_id=None):
    wire=dict(vars(classification()),category='bug',actionable=True,verdict='ticket',
        key_message_ids=[mid],missing_info=[],reason='reported failure')
    return {'message':{'content':json.dumps(dict(groups=[dict(message_ids=[mid],action='CONTINUE' if candidate_id else 'NEW',
        candidate_id=candidate_id,confidence=.95,question='',analysis=wire)]))}}


class IngressPackingTests(TestCase):
    def test_realistic_three_historical_roots_do_not_starve_new_login(self):
        http=FakeHTTP(envelope(457)); model=Classifier(config().ollama,opener=http)
        text='Login đúng pass mà không login được'
        model.analyze_ingress([message(457,text=text)],
            [candidate('c1',7),candidate('c2',1,start=101),candidate('c3',6,start=201)])
        self.assertEqual(1,len(http.calls))
        body=json.loads(http.calls[0][0].data); packet=json.loads(body['messages'][1]['content'])
        self.assertEqual(text,packet['sources'][0][3]); self.assertFalse(packet['sources'][0][4])
        self.assertLessEqual(request_text_bytes(body),8000)

    def test_exact_old_candidate_keeps_ids_bindings_and_pixels_when_optional_evicted(self):
        with TemporaryDirectory() as folder:
            image=Path(folder)/'synthetic.jpg'; image.write_bytes(JPG)
            exact=candidate('opaque-6',2,exact=True,start=601)
            exact['messages'][0]=message(601,text='Caption for existing screenshot',has_image=True)
            exact['current_scopes'][0]['human_bindings']={'601':0}
            choices=[candidate('opaque-'+str(i),2,start=i*10,pressure=1000) for i in range(1,6)]+[exact]
            http=FakeHTTP(envelope(900,'opaque-6')); model=Classifier(config().ollama,opener=http)
            answer=model.analyze_ingress([message(900,reply_to_id=601,text='Additional evidence for the old issue')],choices,
                images={601:image,50:image})
            self.assertIsNotNone(answer); body=json.loads(http.calls[0][0].data)
            packet=json.loads(body['messages'][1]['content'])
            retained=next(c for c in packet['candidates'] if c['candidate_id']=='opaque-6')
            self.assertEqual([601,602],[s[0] for s in retained['sources']])
            self.assertEqual({'601':0},retained['current_scopes'][0]['human_bindings'])
            self.assertEqual(601,packet['sources'][0][1])
            self.assertTrue(packet['candidate_context_truncated']); self.assertTrue(packet['omitted_candidate_ids'])
            self.assertEqual([601],[entry['message_id'] for entry in packet['images_order']])
            self.assertEqual(1,len(body['messages'][1]['images']))

    def test_omitted_candidate_cannot_be_selected_by_model(self):
        http=FakeHTTP(envelope(900,'old-1')); model=Classifier(config().ollama,opener=http)
        answer=model.analyze_ingress([message(900)],[candidate('old-1',1,pressure=7000)])
        self.assertIsNone(answer); self.assertEqual('invalid_output',model.last_ingress_outcome)
        self.assertEqual(['old-1'],model.last_ingress_context['omitted_candidate_ids'])

    def test_mandatory_exact_context_overflow_retains_authority_and_makes_no_call(self):
        http=FakeHTTP(); model=Classifier(config().ollama,opener=http)
        model.analyze_ingress([message(900,reply_to_id=601)],
            [candidate('old-exact',2,start=601,exact=True,pressure=9000)])
        self.assertEqual('context_budget_exhausted',model.last_ingress_outcome)
        self.assertFalse(http.calls)
        self.assertEqual(['old-exact'],model.last_ingress_context['retained_candidate_ids'])
        self.assertFalse(model.last_ingress_context['omitted_candidate_ids'])

    def test_pipeline_real_classifier_with_historical_candidate_pressure_creates_login(self):
        http=FakeHTTP(envelope(457)); model=Classifier(config().ollama,opener=http)
        from tg_triage.store import Store
        store=Store(':memory:'); self.addCleanup(store.close)
        pipeline=Pipeline(replace(config('live'),autonomous_enabled=True),store,model,FakeGitHub())
        roots=[candidate('x',7),candidate('y',1,start=101),candidate('z',6,start=201)]
        for i,c in enumerate(roots):
            c.update(source=dict(root_key='root'+str(i),case_key='case'+str(i),issue_number=44+i),key='case'+str(i))
        store.autonomy_candidates=lambda *args:roots
        result=pipeline.process(message(457,text='Login đúng pass mà không login được',mentions_listener=False))
        self.assertEqual('issue',result.decision); self.assertEqual(1,len(http.calls))
        self.assertEqual(1,len(pipeline.github.calls))

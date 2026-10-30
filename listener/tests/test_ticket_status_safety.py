"""Host STATUS proofs; no claims of real-model intent understanding."""
from dataclasses import replace
from unittest import TestCase
from tg_triage import ticket_status
from tg_triage.models import Decision
from tg_triage.pipeline import Pipeline
from tg_triage.store import Store
from tests.helpers import config,message,FakeGitHub
from tests.test_autonomous import Router,route


class TicketStatusSafetyTests(TestCase):
    def build(self,router,sender=None):
        store=Store(':memory:');self.addCleanup(store.close)
        return Pipeline(replace(config('live',window=2),autonomous_enabled=True),store,router,FakeGitHub(),notice_sender=sender)

    def test_status_notice_chain_fails_closed_instead_of_false_no_ticket(self):
        sent=[]
        p=self.build(Router([route([1])],[route([10],action='STATUS')]),lambda c,m,t:sent.append(t) or (900+len(sent)))
        p.process(message(1))
        p.process(message(10,reply_to_id=1))
        snap=ticket_status.resolve(p.store,[message(20,reply_to_id=902)],p.config.gh_repo)
        self.assertEqual('unavailable',snap['state'])
        self.assertEqual(1,len(p.github.calls))

    def test_foreign_repo_url_cannot_be_claimed_as_created(self):
        p=self.build(Router([route([1])]))
        p.process(message(1))
        with p.store.connection:
            p.store.connection.execute("UPDATE messages SET issue_url='https://github.com/other/repo/issues/101' WHERE message_id=1")
        snap=ticket_status.resolve(p.store,[message(10,reply_to_id=1)],p.config.gh_repo)
        self.assertEqual('unavailable',snap['state'])
        self.assertEqual([],snap['issues'])

    def test_multiple_questions_never_borrow_first_target(self):
        p=self.build(Router())
        self.assertEqual('unavailable',ticket_status.resolve(p.store,[message(10,reply_to_id=1),message(11,reply_to_id=2)],p.config.gh_repo)['state'])

    def test_extra_model_target_is_rejected_before_effect(self):
        g=route([10],action='STATUS');g['issue_number']=999
        p=self.build(Router([g]))
        self.assertEqual('pending_review',p.process(message(10)).decision)
        self.assertEqual([],p.github.calls)

    def test_compact_wire_detail_schema_keeps_exact_key_and_type_sets(self):
        import re
        from tg_triage.ingress_model import INGRESS_SCHEMA,INGRESS_ANALYSIS_SCHEMA
        original=INGRESS_ANALYSIS_SCHEMA['properties']['details']['properties']
        wire=INGRESS_SCHEMA['properties']['groups']['items']['properties']['analysis']['properties']['details']
        self.assertFalse(wire['additionalProperties'])
        for name,leaf in original.items():
            matches=[schema for pattern,schema in wire['patternProperties'].items() if re.search(pattern,name)]
            self.assertEqual([leaf],matches)
        for name in ('issue_number','xexpected','expected_x','expected\n'):
            self.assertFalse(any(re.search(pattern,name) for pattern in wire['patternProperties']))

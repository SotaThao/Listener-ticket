"""Byte-level snapshots captured from the original ticket renderers."""

import itertools
import json
import unittest
from pathlib import Path
from types import SimpleNamespace

from tg_triage.group_notices import notice_text
from tg_triage.issue_format import format_issue
from tests.helpers import NOW, classification, message

FIXTURE = Path(__file__).parent / "fixtures" / "template_golden.json"
IMAGE = (("Ảnh", "https://example.invalid/image.png"),)
DOCUMENT = (("", "https://example.invalid/document.pdf"),)


def outputs():
    result = {}
    def save(key, issue):
        result[key] = dict(title=issue.title, body=issue.body.replace(
            NOW.astimezone().strftime("%d/%m %H:%M"), "<TIME>"), labels=list(issue.labels))

    for steps, evidence, notes, questions in itertools.product(
            (False, True), range(5), (False, True), (False, True)):
        details = dict(environment="Android", steps=["Mở", "Nhấn"] if steps else [],
                       expected="Đúng", actual="Sai", error_text="Lỗi" if evidence in (1, 4) else "")
        key = f"bug/{steps}/{evidence}/{notes}/{questions}"
        save(key, format_issue(message(), classification(), "model", False, details=details,
             images=IMAGE if evidence in (2, 4) else (), documents=DOCUMENT if evidence in (3, 4) else (),
             notes=("Ghi chú",) if notes else (), clarify=["Bạn thấy gì?"] if questions else []))
    for flags in itertools.product((False, True), repeat=7):
        problem, story, proposal, alternatives, acceptance, context, media = flags
        details = dict(problem="Vấn đề" if problem else "", user_story="Câu chuyện" if story else "",
                       proposal="Đề xuất" if proposal else "", alternatives="Khác" if alternatives else "",
                       acceptance=["Xong"] if acceptance else [], context="Bối cảnh" if context else "")
        save("feedback/" + ''.join(map(lambda x: str(int(x)), flags)),
             format_issue(message(), classification(type="feedback"), "model", False, details=details,
                          images=IMAGE if media else (), documents=DOCUMENT if media else ()))
    for flags in itertools.product((False, True), repeat=7):
        scope_in, scope_out, reason, due, links, done, media = flags
        details = dict(work="Thực hiện", scope_in="Trong" if scope_in else "", scope_out="Ngoài" if scope_out else "",
                       reason_context="Lý do" if reason else "", due="Mai" if due else "",
                       links=["liên kết"] if links else [], done=["Xong"] if done else [])
        save("task/" + ''.join(map(lambda x: str(int(x)), flags)),
             format_issue(message(), classification(type="task"), "model", False, details=details,
                          images=IMAGE if media else (), documents=DOCUMENT if media else ()))
    for tier, typed, details in itertools.product(("manual", "rule", "model"), (False, True), (None, {})):
        save(f"human/{tier}/{typed}/{details is None}", format_issue(
            message(), classification() if typed else None, tier, True, details=details, clarify=[],
            text=("Tiêu đề", "Tóm tắt"), images=IMAGE, documents=DOCUMENT, notes=("Ghi chú",)))
    messages = [message(i, sender_name=name, text=text, **extra) for i, name, text, extra in (
        (1, "", "", {"has_image": True}), (2, "Hai", "", {"has_document": True}),
        (3, "Ba", "Nội dung", {}), *[(i, f"Người {i}", "Tin", {}) for i in range(4, 13)])]
    save("many", format_issue(messages[0], classification(), "model", False,
         case_messages=messages, key_message_ids=[2], details={}, documents=DOCUMENT))
    save("invalid-confidence", format_issue(message(), classification(confidence=2), "model", True, clarify=[]))
    for decision, link, questions in itertools.product(
            ("issue", "needs_human", "error", "rate_limited", "context_pending", "chat"),
            (False, True), (False, True)):
        result[f"notice/{decision}/{link}/{questions}"] = notice_text(
            SimpleNamespace(decision=decision, issue_number=42 if link else None),
            ["Bạn muốn gì?"] if questions else [], "sandbox/triage")
    # Exercise the original follow-up pipeline using its fake GitHub transport.
    from tg_triage.classifier import Classifier
    from tests.helpers import FakeHTTP
    from tests.test_followups import answer
    from tests.test_followup_enrichment import EnrichmentTests
    helper = EnrichmentTests()
    try:
        store, pipeline, api, *_ = helper.setup_case()
        pipeline.classifier = Classifier(pipeline.config.ollama, prompt="unused", opener=FakeHTTP(
            {"message": {"content": json.dumps(answer("update"))}}))
        helper.photo(pipeline)
        result["comment/enrichment"] = api.comments[0]
    finally:
        helper.doCleanups()
    from tests.test_followups import FollowupTests
    helper = FollowupTests()
    try:
        store, model, github, sent, pipeline = helper.setup_case()
        helper.reply(pipeline)
        result["comment/scope_correction"] = github.comments[0][1]
        result["append/source"] = github.calls[1].body.replace(
            NOW.astimezone().strftime("%d/%m %H:%M"), "<TIME>")
        result["reply/split"] = sent[-1][2]
    finally:
        helper.doCleanups()
    from tests.test_lifecycle import LifecycleTests
    helper = LifecycleTests()
    try:
        store, model, github, sent, pipeline = helper.setup_lifecycle()
        helper.recurrence(pipeline, model)
        result["append/recurrence"] = github.calls[-1].body.replace(
            NOW.astimezone().strftime("%d/%m %H:%M"), "<TIME>")
        result["reply/recurrence"] = sent[-1][2]
    finally:
        helper.doCleanups()
    from tests.test_autonomous import AutonomousTests, Router, route
    helper = AutonomousTests()
    try:
        pipeline = helper.build(Router([route([1])], [route([2])]))
        pipeline.process(message(1))
        pipeline.process(message(2, text="Yêu cầu khác: đổi bảng tuyển dụng", reply_to_id=1))
        result["append/related"] = pipeline.github.calls[1].body.replace(
            NOW.astimezone().strftime("%d/%m %H:%M"), "<TIME>")
    finally:
        helper.doCleanups()
    return result


def additional_outputs():
    """Exercise the original public renderers and fake-transport follow-up paths."""
    result = {}
    def save(key, issue):
        result[key] = dict(title=issue.title, body=issue.body.replace(
            NOW.astimezone().strftime('%d/%m %H:%M'), '<TIME>'), labels=list(issue.labels))

    for tier in ('manual', 'rule'):
        save('extra/provenance/' + tier, format_issue(message(), classification(), tier, False, details={}))
    messages = [message(i, text=f'Source {i}', sender_name=f'Sender {i}') for i in range(1, 13)]
    save('extra/hidden', format_issue(messages[0], classification(), 'model', False,
                                     case_messages=messages, details={}))
    for key, source in (
        ('image_only', message(text='', has_image=True)),
        ('document_only', message(text='', has_document=True)),
        ('unknown_sender', message(sender_name='')),
    ):
        save('extra/' + key, format_issue(source, classification(), 'model', False, details={}))
    save('extra/unlabeled_document', format_issue(message(), classification(), 'model', False,
                                                   details={}, documents=DOCUMENT))

    from tests.test_followups import FollowupTests, answer
    helper = FollowupTests()
    try:
        store, model, github, sent, pipeline = helper.setup_case()
        helper.reply(pipeline)
        result['extra/followup/split'] = sent[-1][2]
        pipeline.process(message(11, text='đây là ticket 2', reply_to_id=2, sender_id=7))
        result['extra/comment/scope_confirmation'] = github.comments[-1][1]
    finally:
        helper.doCleanups()
    helper = FollowupTests()
    try:
        store, model, github, sent, pipeline = helper.setup_case()
        model.value = answer('update')
        helper.reply(pipeline, text='Additional information')
        result['extra/followup/updated'] = sent[-1][2]
    finally:
        helper.doCleanups()
    from tests.test_lifecycle import LifecycleTests
    helper = LifecycleTests()
    try:
        store, model, github, sent, pipeline = helper.setup_lifecycle()
        helper.recurrence(pipeline, model)
        result['extra/followup/recurrence'] = sent[-1][2]
    finally:
        helper.doCleanups()
    return result


class TemplateGoldenTests(unittest.TestCase):
    def test_outputs_match_original_renderers(self):
        expected = json.loads(FIXTURE.read_text(encoding="utf-8"))
        actual = outputs()
        self.assertEqual(actual, {key: expected[key] for key in actual})

    def test_additional_original_outputs(self):
        expected = json.loads(FIXTURE.read_text(encoding="utf-8"))
        actual = additional_outputs()
        self.assertEqual(actual, {key: expected[key] for key in actual})


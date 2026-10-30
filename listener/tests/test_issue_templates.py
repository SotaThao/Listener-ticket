"""Offline acceptance tests for Assistant definition 12.15 (cases 43–52)."""

import json
import unittest
from contextlib import closing
from datetime import datetime, timezone

from tg_triage.classifier import CASE_SCHEMA, Classifier, parse_case_analysis
from tg_triage.issue_format import MAX_CLARIFY, format_issue
from tg_triage.ticket_templates import active

BUG_QUESTIONS = tuple(active().render("bug.md", "question." + key) for key in ("environment", "steps", "expected_actual", "evidence"))
DOD_EVIDENCE_LINE = "- [ ] Đính kèm evidence kết quả (ảnh/link/log) khi đóng issue"
NO_DETAIL_LINE = "(chưa có thông tin)"
from tg_triage.models import IncomingMessage
from tg_triage.pipeline import Pipeline
from tg_triage.store import Store
from tests.helpers import FakeGitHub, FakeHTTP, config, message


BUG_DETAILS = dict(environment="Nexora build 42 · Android 14 · Chrome · Đăng nhập · member",
                   steps=["Mở màn hình đăng nhập", "Nhấn Đăng nhập"],
                   expected="Vào trang chủ", actual="Không đăng nhập được", error_text="Lỗi E42")
FEEDBACK_DETAILS = dict(problem="Khó tìm giao dịch", user_story="Là kế toán, tôi muốn lọc giao dịch để đối soát",
                        acceptance=["Given danh sách giao dịch, When chọn ngày, Then chỉ hiện giao dịch ngày đó"])
TASK_DETAILS = dict(work="Thêm bộ lọc ngày", done=["Bộ lọc áp dụng đúng khoảng ngày"])
IMAGE = ("Ảnh 1", "https://example.invalid/screenshot.png")
DOCUMENT = ("log.txt", "https://example.invalid/log.txt")


def case_payload(category="bug", details=None, **extra):
    """Every required field has the exact shape declared by CASE_SCHEMA."""
    payload = dict(category=category, type="feature" if category == "feedback" else category,
                   actionable=True, title="Yêu cầu từ Telegram", summary="Nội dung cần xử lý.",
                   priority="medium", confidence=0.9, missing_info=[], reason="Yêu cầu rõ",
                   key_message_ids=[1])
    if details is not None:
        payload["details"] = details
    payload.update(extra)
    return payload


def topic_payload(category="bug", details=None, **extra):
    payload = case_payload(category, details, **extra)
    payload.pop("key_message_ids")
    payload.update(topic="Yêu cầu", message_ids=[1])
    return {"groups": [payload]}


def render(category="bug", details=None, **kwargs):
    analysis = parse_case_analysis(json.dumps(case_payload(category, details), ensure_ascii=False))
    return format_issue(message(), analysis.classification, "model", False,
                        details=analysis.details, **kwargs)


def rendered_examples():
    """Stable fixtures printed by the reviewer without invoking any external service."""
    msg = IncomingMessage(-123, 1, datetime(2026, 10, 13, 8, tzinfo=timezone.utc),
                          "Người thử nghiệm", "Nhờ xử lý yêu cầu này.")
    result = {}
    for category, details in (("bug", BUG_DETAILS), ("feedback", FEEDBACK_DETAILS), ("task", TASK_DETAILS)):
        analysis = parse_case_analysis(json.dumps(case_payload(category, details), ensure_ascii=False))
        result[category] = format_issue(msg, analysis.classification, "model", False,
                                       details=analysis.details, images=(IMAGE,), documents=(DOCUMENT,)).body
    return result


class IssueTemplateTests(unittest.TestCase):
    def test_43_bug_complete_details_and_image(self):
        issue = render(details=BUG_DETAILS, images=(IMAGE,))
        self.assertIn("### 🐞 Lỗi", issue.body)
        self.assertIn("### 🖼 Minh họa / Evidence", issue.body)
        for label in ("Môi trường", "Các bước tái hiện", "Mong đợi", "Thực tế"):
            self.assertIn(f"**{label}:**", issue.body)
        self.assertIn("1. Mở màn hình đăng nhập\n2. Nhấn Đăng nhập", issue.body)
        self.assertIn("Lỗi E42", issue.body)
        self.assertIn(f"![{IMAGE[0]}]({IMAGE[1]})", issue.body)
        self.assertLess(issue.body.index("Minh họa / Evidence"), issue.body.index(IMAGE[1]))
        self.assertLess(issue.body.index(IMAGE[1]), issue.body.index("<details>"))
        self.assertNotIn("Cần làm rõ", issue.body)
        for word in ("severity", "mức độ", "priority"):
            self.assertNotIn(word, issue.body.lower())
            self.assertNotIn(word, issue.labels)

    def test_44_empty_bug_fields_auto_questions_and_same_labels(self):
        details = dict(BUG_DETAILS, steps=[], error_text="")
        issue = render(details=details)
        self.assertIn("**Các bước tái hiện:** " + NO_DETAIL_LINE, issue.body)
        self.assertIn("### 🖼 Minh họa / Evidence\n" + NO_DETAIL_LINE, issue.body)
        self.assertIn(BUG_QUESTIONS[1], issue.body)
        self.assertIn(BUG_QUESTIONS[3], issue.body)
        self.assertEqual(issue.labels, ("bug", "triage", "source:telegram"))
        self.assertLess(issue.body.index("Minh họa / Evidence"), issue.body.index("Cần làm rõ"))
        self.assertLess(issue.body.index("Cần làm rõ"), issue.body.index("<details>"))

    def test_45_feedback_required_optional_and_draft_checklist(self):
        body = render("feedback", FEEDBACK_DETAILS).body
        self.assertIn("### 💡 Vấn đề / nhu cầu", body)
        self.assertIn("**User story:**", body)
        self.assertIn("- [ ] Given danh sách giao dịch", body)
        self.assertIn("### ✅ Tiêu chí chấp nhận (nháp)", body)
        for label in ("Đề xuất", "Phương án khác"):
            self.assertNotIn(f"**{label}:**", body)
        self.assertNotIn("### 🧭 Ngữ cảnh", body)
        empty = render("feedback", {}).body
        self.assertEqual(empty.count(NO_DETAIL_LINE), 1)   # empty labels (user story...) are hidden
        self.assertNotIn("**User story:**", empty)
        self.assertNotIn("### ✅", empty)
        self.assertIn("### ❓ Cần làm rõ", empty)

    def test_45_feedback_attachments_stay_in_context(self):
        body = render("feedback", FEEDBACK_DETAILS, images=(IMAGE,), documents=(DOCUMENT,)).body
        self.assertLess(body.index("### 🧭 Ngữ cảnh"), body.index(IMAGE[1]))
        self.assertLess(body.index(DOCUMENT[1]), body.index("<details>"))
        self.assertEqual(body.count(IMAGE[1]), 1)
        self.assertEqual(body.count(DOCUMENT[1]), 1)

    def test_46_task_evidence_and_fixed_dod_without_model_done(self):
        for details in (TASK_DETAILS, {"work": "Thêm bộ lọc ngày"}, {}):
            with self.subTest(details=details):
                issue = render("task", details, images=(IMAGE,), documents=(DOCUMENT,))
                body = issue.body
                self.assertIn("### 🖼 Minh họa / Evidence (đầu vào)", body)
                self.assertLess(body.index("Evidence (đầu vào)"), body.index(IMAGE[1]))
                self.assertLess(body.index(DOCUMENT[1]), body.index("### 🏁 Xong khi"))
                self.assertIn(DOD_EVIDENCE_LINE, body)
                self.assertEqual(body.count(DOD_EVIDENCE_LINE), 1)
                self.assertLess(body.index(DOD_EVIDENCE_LINE), body.index("<details>"))
                self.assertEqual(issue.labels, ("task", "triage", "source:telegram"))

    def test_46_task_optional_fields_and_final_dod_line(self):
        body = render("task", dict(TASK_DETAILS, reason_context="Đối soát cuối tháng", scope_in="Bộ lọc",
                                  scope_out="Xuất file", links=["https://example.invalid/mockup"], due="Ngày 20/10")).body
        for text in ("**Lý do:** Đối soát cuối tháng",
                     "**Phạm vi:** Làm — Bộ lọc · Không làm — Xuất file",
                     "**Hạn / người được nhắc:** Ngày 20/10"):
            self.assertIn(text, body)
        self.assertIn("- https:\u200b//example.invalid/mockup", body)
        self.assertIn("- [ ] Bộ lọc áp dụng đúng khoảng ngày\n" + DOD_EVIDENCE_LINE, body)
        empty = render("task", {}).body
        for heading in ("**Lý do:**", "**Phạm vi:**", "Evidence (đầu vào)", "**Hạn / người"):
            self.assertNotIn(heading, empty)

    def test_47_all_detail_values_are_cleaned_and_redacted(self):
        hostile = "@user https://evil.invalid <img> --> me@example.invalid 0901234567 [kèm ảnh]"
        keys = CASE_SCHEMA["properties"]["details"]["properties"]
        details = {key: [hostile] if value["type"] == "array" else hostile for key, value in keys.items()}
        for category in ("bug", "feedback", "task"):
            with self.subTest(category=category):
                body = render(category, details).body
                for raw in ("@user", "://evil", "<img>", "me@example.invalid", "0901234567", "kèm ảnh"):
                    self.assertNotIn(raw, body)
                self.assertEqual(body.count("-->"), 1)
                self.assertIn("@\u200buser", body)
                self.assertIn("&lt;img&gt;", body)
                self.assertIn("[email đã ẩn]", body)
                self.assertIn("[số đã ẩn]", body)

    def test_48_malformed_details_never_break_issue_creation(self):
        for details in ("bad", 42, None, [], {"steps": "wrong", "expected": 42, "actual": None,
                                             "environment": {}, "unknown": "ignored"}):
            with self.subTest(details=details):
                payload = topic_payload(details=details)
                payload["groups"][0]["details"] = details
                http = FakeHTTP({"message": {"content": json.dumps(payload)}})
                github = FakeGitHub()
                with closing(Store(":memory:")) as store:
                    result = Pipeline(config("live"), store, Classifier(config().ollama, opener=http), github).process(message())
                self.assertEqual(result.decision, "issue")
                self.assertEqual(len(http.calls), 1)
                self.assertEqual(github.calls[0].labels, ("bug", "triage", "source:telegram"))

    def test_48_limits_wrong_items_unknown_keys_and_immutable_mapping(self):
        details = {"environment": "E" * 800, "steps": ["S" * 400] * 20 + [42, None],
                   "user_story": "OTHER CATEGORY", "unknown": "IGNORED"}
        analysis = parse_case_analysis(json.dumps(case_payload(details=details)))
        with self.assertRaises(TypeError):
            analysis.details["environment"] = "changed"
        self.assertIsInstance(dict(analysis.details)["steps"], tuple)
        self.assertNotIn("unknown", dict(analysis.details))
        body = render(details=details).body
        self.assertIn("E" * 500, body)
        self.assertNotIn("E" * 501, body)
        self.assertIn("10. " + "S" * 200, body)
        self.assertNotIn("11. ", body)
        self.assertNotIn("S" * 201, body)
        mixed = render(details={"steps": [1, "Valid step", None, {}]}).body
        self.assertIn("1. Valid step", mixed)

    def test_48_limits_apply_after_redaction(self):
        body = render(details={"environment": "a" * 495 + " me@example.invalid"}).body
        self.assertNotIn("me@", body)
        self.assertNotIn("example.invalid", body)

    def test_49_other_category_keys_never_render(self):
        for category, details in (("bug", {"user_story": "FORBIDDEN", "work": "FORBIDDEN"}),
                                  ("feedback", {"environment": "FORBIDDEN", "done": ["FORBIDDEN"]}),
                                  ("task", {"problem": "FORBIDDEN", "error_text": "FORBIDDEN"})):
            with self.subTest(category=category):
                self.assertNotIn("FORBIDDEN", render(category, details).body)

    def test_50_untyped_body_is_byte_for_byte_unchanged(self):
        kwargs = dict(clarify=["Thông tin nào?"], text=("Chưa rõ", "Nhờ đọc lại"),
                      images=(IMAGE,), documents=(DOCUMENT,), notes=("Lỗi tải tệp",))
        old = format_issue(message(), None, "model", True, **kwargs)
        new = format_issue(message(), None, "model", True, details=BUG_DETAILS, **kwargs)
        self.assertEqual(old, new)
        self.assertTrue(new.title.startswith("[cần phân loại]"))
        self.assertNotIn("Ảnh đính kèm", new.body)
        self.assertLess(new.body.index("### 🖼 Minh họa / Evidence"), new.body.index("<details>"))

    def test_51_model_questions_first_deduplicated_and_bounded(self):
        model = ["Thông tin nào?", "Thông tin nào?", BUG_QUESTIONS[1]]
        body = render(details={}, clarify=model).body
        block = body.split("### ❓ Cần làm rõ\n")[1].split("\n\n")[0]
        lines = block.splitlines()
        self.assertEqual(len(lines), MAX_CLARIFY)
        self.assertEqual(len(set(lines)), MAX_CLARIFY)
        self.assertEqual(lines[:2], ["- Thông tin nào?", "- " + BUG_QUESTIONS[1]])
        self.assertEqual(body.count(BUG_QUESTIONS[1]), 1)
        many = render(details={}, clarify=[f"Câu hỏi {i}" for i in range(20)]).body
        self.assertEqual(many.split("### ❓ Cần làm rõ\n")[1].split("\n\n")[0].count("- "), MAX_CLARIFY)
        self.assertNotIn(BUG_QUESTIONS[0], many)

    def test_52_legacy_call_and_quotes_attachments_notes_preserved(self):
        analysis = parse_case_analysis(json.dumps(case_payload()))
        kwargs = dict(images=(IMAGE,), documents=(DOCUMENT,), notes=("Lỗi tải tệp",))
        old = format_issue(message(), analysis.classification, "model", False, **kwargs)
        explicit_none = format_issue(message(), analysis.classification, "model", False, details=None, **kwargs)
        new = format_issue(message(), analysis.classification, "model", False, details={}, **kwargs)
        self.assertEqual(old, explicit_none)
        def quote(body):   # the collapsed original-message block, up to and including the footer
            return body[body.index("<details>"):]
        self.assertEqual(quote(old.body), quote(new.body))
        for attachment in (f"![{IMAGE[0]}]({IMAGE[1]})", f"- 📎 [{DOCUMENT[0]}]({DOCUMENT[1]})"):
            self.assertIn(attachment, old.body)
            self.assertIn(attachment, new.body)
        # Layout 12.16: the note sits right under the image and document, above the original messages.
        for body in (old.body, new.body):
            self.assertGreater(body.index("Lỗi tải tệp"), body.index(DOCUMENT[1]))
            self.assertLess(body.index("Lỗi tải tệp"), body.index("<details>"))
        self.assertEqual(old.labels, new.labels)

    def test_typed_needs_human_has_template_and_original_labels(self):
        analysis = parse_case_analysis(json.dumps(case_payload(details=BUG_DETAILS)))
        issue = format_issue(message(), analysis.classification, "rule", True, details=analysis.details,
                             clarify=["Xác nhận giúp"], text=("Lỗi", "Tóm tắt"))
        self.assertIn("**Môi trường:**", issue.body)
        self.assertEqual(issue.labels, ("bug", "triage", "needs-human", "source:telegram"))
        self.assertLess(issue.body.index("Minh họa / Evidence"), issue.body.index("Cần làm rõ"))

    def test_pipeline_passes_single_and_topic_details_including_command(self):
        for topics, command, missing in ((False, False, []), (True, False, []),
                                          (True, True, []), (True, False, ["Xác nhận?"])):
            with self.subTest(topics=topics, command=command, missing=missing):
                payload = (topic_payload if topics else case_payload)(details=BUG_DETAILS, missing_info=missing)
                http = FakeHTTP({"message": {"content": json.dumps(payload)}})
                client = Classifier(config().ollama, opener=http)
                if not topics:
                    # Exercise the supported single-case interface with the real parser/request.
                    class SingleCaseClient:
                        analyze_case = client.analyze_case
                    client = SingleCaseClient()
                github = FakeGitHub()
                with closing(Store(":memory:")) as store:
                    result = Pipeline(config("live"), store, client, github).process(
                        message(text="/bug Lỗi đăng nhập" if command else "Đăng nhập bị lỗi",
                                has_document=command, document_name="mockup.pdf" if command else None))
                self.assertIn(BUG_DETAILS["environment"], github.calls[0].body)
                self.assertEqual(result.decision, "needs_human" if missing else "issue")
                self.assertEqual(len(http.calls), 1)


if __name__ == "__main__":
    unittest.main()

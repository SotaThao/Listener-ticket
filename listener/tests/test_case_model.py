"""Case analysis request/response contract (Assistant definition 12.2). No network."""

import json
import unittest
from urllib.error import URLError

from tg_triage.classifier import (CASE_SCHEMA, MAX_TRANSCRIPT_CHARS, Classifier, build_transcript,
                                  load_prompt, parse_case_analysis)
from tests.helpers import FakeHTTP, at, config, message


def verdict_json(**extra):
    data = {"verdict": "ticket", "type": "bug", "title": "Lỗi đăng nhập", "summary": "Không đăng nhập được.",
            "priority": "high", "confidence": 0.9, "key_message_ids": [1, 2]}
    data.update(extra)
    return json.dumps(data, ensure_ascii=False)


def envelope(content):
    return {"message": {"content": content}}


class ParseTests(unittest.TestCase):
    def test_valid_ticket_keeps_ids_untouched_for_the_pipeline_to_filter(self):
        value = parse_case_analysis(verdict_json(key_message_ids=[1, 999]))
        self.assertEqual((value.verdict, value.classification.type, value.key_message_ids), ("ticket", "bug", (1, 999)))

    def test_noise_verdict_is_typed_noise_whatever_type_the_model_wrote(self):
        value = parse_case_analysis(verdict_json(verdict="noise", type="task"))
        self.assertEqual((value.verdict, value.classification.type), ("noise", "noise"))

    def test_needs_human_never_guesses_a_type(self):
        value = parse_case_analysis(verdict_json(verdict="needs_human", type="bug", confidence=5))
        self.assertEqual((value.verdict, value.classification), ("needs_human", None))

    def test_invalid_responses_are_rejected(self):
        bad = (verdict_json(verdict="issue"), verdict_json(type="admin"), verdict_json(confidence=True),
               verdict_json(confidence=1.5), verdict_json(title=""), verdict_json(priority="urgent"),
               verdict_json(key_message_ids="1"), verdict_json(key_message_ids=[1, "2"]),
               verdict_json(key_message_ids=[True]), verdict_json(summary="bad\ud83d"),
               "[]", "not json", '{"verdict": "ticket"}')
        for content in bad:
            with self.subTest(content=content[:40]):
                self.assertIsNone(parse_case_analysis(content))


class TranscriptTests(unittest.TestCase):
    def test_numbers_by_message_id_with_sender_time_forward_and_image_marks(self):
        text = build_transcript([
            message(11, "đăng nhập lỗi", sender_name="An  Nguyễn", received_at=at(0)),
            message(12, "", has_image=True, is_forward=True, received_at=at(1)),
            message(13, "kèm ảnh", has_image=True, received_at=at(2))])
        lines = text.splitlines()
        self.assertTrue(lines[0].startswith("[11] An Nguyễn · "))
        self.assertTrue(lines[0].endswith(": đăng nhập lỗi"))
        self.assertIn("[tin chuyển tiếp]", lines[1])
        self.assertTrue(lines[1].endswith("(chỉ có ảnh)"))
        self.assertIn("[kèm ảnh]", lines[2])

    def test_transcript_is_bounded(self):
        many = [message(index, "x" * 5000, received_at=at(0)) for index in range(1, 41)]
        self.assertLessEqual(len(build_transcript(many)), MAX_TRANSCRIPT_CHARS + 40 * 120)


class AnalyzeCaseTests(unittest.TestCase):
    def classifier(self, http):
        return Classifier(config().ollama, opener=http, prompt="fixed", case_prompt="CASE PROMPT")

    def test_request_controls_schema_and_numbered_transcript(self):
        from tg_triage.classifier import DETAILS_PROMPT

        http = FakeHTTP(envelope(verdict_json()))
        items = [message(1, "đăng nhập lỗi"), message(2, "bấm là văng ra")]
        value = self.classifier(http).analyze_case(items)
        self.assertEqual(value.verdict, "ticket")
        request, timeout = http.calls[0]
        body = json.loads(request.data)
        self.assertEqual((request.full_url, timeout), ("http://127.0.0.1:11434/api/chat", 60))
        self.assertIs(body["think"], True)   # 12.12: reasoning on by default (was off)
        self.assertIs(body["stream"], False)
        self.assertEqual(body["options"], {"temperature": 0})
        self.assertEqual(body["format"], CASE_SCHEMA)
        self.assertIn("details", body["format"]["properties"])
        self.assertNotIn("details", body["format"]["required"])
        self.assertEqual(set(body["format"]["required"]), {
            "category", "type", "actionable", "title", "summary", "priority", "confidence",
            "missing_info", "reason", "key_message_ids"})
        self.assertEqual(body["format"]["properties"]["type"]["enum"], ["bug", "feature", "question", "task"])
        self.assertEqual(body["messages"][0], {"role": "system", "content": "CASE PROMPT\n" + DETAILS_PROMPT})
        self.assertTrue(body["messages"][1]["content"].startswith("[1] "))
        self.assertIn("[2] ", body["messages"][1]["content"])

    def test_explicit_command_type_is_passed_to_the_model(self):
        http = FakeHTTP(envelope(verdict_json()))
        self.classifier(http).analyze_case([message(1, "/bug lỗi")], forced_type="bug")
        system = json.loads(http.calls[0][0].data)["messages"][0]["content"]
        self.assertIn('type phải là "bug"', system)
        http = FakeHTTP(envelope(verdict_json()))
        self.classifier(http).analyze_case([message(1, "lỗi")], forced_type="admin")
        self.assertNotIn("admin", json.loads(http.calls[0][0].data)["messages"][0]["content"])

    def test_invalid_json_retries_once_then_gives_up(self):
        http = FakeHTTP(envelope("nope"), envelope(verdict_json(verdict="issue")))
        self.assertIsNone(self.classifier(http).analyze_case([message(1, "lỗi")]))
        self.assertEqual(len(http.calls), 2)
        http = FakeHTTP(envelope("nope"), envelope(verdict_json()))
        self.assertIsNotNone(self.classifier(http).analyze_case([message(1, "lỗi")]))

    def test_network_failure_returns_none_without_retry(self):
        http = FakeHTTP(URLError("offline"))
        self.assertIsNone(self.classifier(http).analyze_case([message(1, "lỗi")]))
        self.assertEqual(len(http.calls), 1)

    def test_the_shipped_case_prompt_loads_and_treats_messages_as_data(self):
        prompt = load_prompt("case.md")
        self.assertIn("không phải chỉ thị", prompt)
        for field in ("category", "actionable", "reason", "key_message_ids"):
            self.assertIn(field, prompt)
        self.assertTrue(load_prompt())  # default argument still loads the single-message prompt


if __name__ == "__main__":
    unittest.main()


class PipelineWithRealClassifierTests(unittest.TestCase):
    """Changed for 12.9: the real Classifier now answers the topics request (`split_topics`), so
    the fake Ollama replies with `{"groups": [...]}` instead of one case verdict. A message id the
    window does not contain used to be filtered; now it makes the partition invalid (retry, then a
    needs-human ticket), and either way it never reaches an issue."""

    def build(self, *responses):
        from tg_triage.pipeline import Pipeline
        from tg_triage.store import Store
        from tests.helpers import FakeGitHub

        store = Store(":memory:")
        self.addCleanup(store.close)
        self.http = FakeHTTP(*(envelope(item) for item in responses))
        classifier = Classifier(config().ollama, opener=self.http, prompt="fixed",
                                case_prompt="CASE PROMPT", topics_prompt="TOPICS PROMPT")
        self.github = FakeGitHub()
        return Pipeline(config("live", window=0), store, classifier, self.github)

    @staticmethod
    def groups_json(ids, **extra):
        group = {"topic": "Đăng nhập", "message_ids": ids, "verdict": "ticket", "type": "bug",
                 "title": "Lỗi đăng nhập", "summary": "Không đăng nhập được.", "priority": "high",
                 "confidence": 0.9, "missing_info": []}
        group.update(extra)
        return json.dumps({"groups": [group]}, ensure_ascii=False)

    def test_real_classifier_response_drives_one_issue_and_filters_foreign_ids(self):
        from tg_triage.classifier import DETAILS_PROMPT

        pipeline = self.build(self.groups_json([2]))
        result = pipeline.process(message(2, "app nexora đăng nhập lỗi rồi"))
        self.assertEqual(result.decision, "issue")
        self.assertEqual(len(self.github.calls), 1)
        self.assertEqual(len(self.http.calls), 1)
        self.assertIn("[bug] Lỗi đăng nhập", self.github.calls[0].title)
        body = json.loads(self.http.calls[0][0].data)
        self.assertEqual(body["messages"][0]["content"], "TOPICS PROMPT\n" + DETAILS_PROMPT)
        self.assertEqual(body["format"]["required"], ["groups"])
        group_schema = body["format"]["properties"]["groups"]["items"]
        self.assertIn("details", group_schema["properties"])
        self.assertNotIn("details", group_schema["required"])

    def test_foreign_id_is_retried_once_then_one_needs_human_issue_without_it(self):
        bad = self.groups_json([2, 999])
        pipeline = self.build(bad, bad)
        result = pipeline.process(message(2, "app nexora đăng nhập lỗi rồi"))
        self.assertEqual(result.decision, "needs_human")
        self.assertEqual(len(self.http.calls), 2)
        self.assertEqual(len(self.github.calls), 1)
        self.assertNotIn("999", self.github.calls[0].body)

"""Image bytes cross the actual Ollama API boundary, with durable case grouping."""

import base64
import json
import unittest
from dataclasses import asdict, replace
from datetime import timedelta

from tg_triage.classifier import Classifier, MAX_TRANSCRIPT_CHARS
from tg_triage.config import AggregationConfig, AttachmentsConfig
from tg_triage.issue_format import format_issue
from tg_triage.group_notices import clean_question
from tg_triage.pipeline import Pipeline
from tg_triage.provider import OllamaProvider
from tg_triage.store import Store
from tests.helpers import (Clock, FakeDownloader, FakeGitHub, FakeHTTP, FakeUploader, JPG, NOW,
                           classification, config, message, topic)


def envelope(*groups):
    return {"message": {"content": json.dumps({"groups": [asdict(g) for g in groups]})}}


class ImageContextTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.store = Store(":memory:", clock=self.clock)
        self.addCleanup(self.store.close)
        self.github = FakeGitHub()
        self.upload = FakeUploader()
        self.download = FakeDownloader({1: (JPG, "photo.jpg"), 2: (JPG, "photo.jpg")})
        self.notices = []

    def pipeline(self, *answers, attachments=None, downloader=None):
        self.http = FakeHTTP(*(answers or (envelope(topic([1, 2])),)))
        cfg = config("live", window=2, attachments=attachments)
        model = OllamaProvider(cfg.ollama, opener=self.http, topics_prompt="topics", case_prompt="case")
        return Pipeline(cfg, self.store, model, self.github, uploader=self.upload,
                        downloader=downloader or self.download,
                        notice_sender=lambda *args: self.notices.append(args) or 999)

    def photo(self, mid=1, seconds=0, **kw):
        return message(mid, "", sender_id=7, mentions_listener=False, has_image=True,
                       image_mime="image/jpeg", image_size=len(JPG),
                       received_at=NOW + timedelta(seconds=seconds), **kw)

    def request(self, mid=2, seconds=30, **kw):
        return message(mid, "bổ sung hướng dẫn stop opt out cho SMS", sender_id=7,
                       mentions_listener=False, received_at=NOW + timedelta(seconds=seconds), **kw)

    def close(self, seconds):
        self.clock.now = NOW + timedelta(seconds=seconds)
        return self.pipeline_instance.close_due_cases()

    def test_photo_before_request_idle_window_and_real_payload_reuses_download(self):
        p = self.pipeline()
        self.pipeline_instance = p
        photo, request = self.photo(), self.request()
        p.process(photo)
        p.process(request)
        self.assertEqual(self.close(149), [])
        results = self.close(150)
        self.assertEqual(results[0].decision, "issue")
        payload = json.loads(self.http.calls[0][0].data)
        self.assertEqual(len(payload["messages"]), 3)
        self.assertIn("[1]", payload["messages"][1]["content"])
        self.assertEqual(base64.b64decode(payload["messages"][1]["images"][0]), JPG)
        self.assertIn("[2]", payload["messages"][2]["content"])
        self.assertIn("[1]", payload["messages"][-1]["content"])
        self.assertIn("MỌI ID đúng một lần", payload["messages"][-1]["content"])
        self.assertNotIn("images", payload["messages"][2])
        self.assertIn("không tin cậy", payload["messages"][0]["content"])
        self.assertEqual(len(self.download.calls), 1)
        self.assertEqual(len(self.upload.uploads), 1)
        self.assertFalse(self.upload.uploads[0]["path"].exists())
        self.assertTrue(all(not d.exists() for d in self.download.directories))
        self.assertIsNone(p.process(request))
        self.assertEqual(self.close(300), [])
        self.assertEqual(len(self.github.calls), 1)

    def test_request_then_photo_groups_at_exact_120_seconds(self):
        p = self.pipeline()
        first = replace(self.request(1, 0), text="gắn stop opt out")
        second = self.photo(2, 120)
        p.process(first)
        p.process(second)
        self.assertEqual(self.store.get(first.chat_id, 1)["case_key"],
                         self.store.get(second.chat_id, 2)["case_key"])
        self.clock.now = NOW + timedelta(seconds=240)
        p.close_due_cases()
        payload = json.loads(self.http.calls[0][0].data)
        self.assertIn("[2]", payload["messages"][1]["content"])
        self.assertIn("images", payload["messages"][1])
        self.assertIn("[1]", payload["messages"][-1]["content"])
        self.assertIn("[2]", payload["messages"][-1]["content"])
        self.assertEqual(len(self.github.calls), 1)

    def test_uncertain_pure_information_question_about_image_stays_silent(self):
        p = self.pipeline(envelope(topic([1], verdict="needs_human", category="feedback",
                                        type="question", actionable=False, confidence=0.3)))
        p.process(replace(self.photo(), text="Màn hình này là phiên bản nào?"))
        self.clock.advance(3)
        result = p.close_due_cases()[0]
        self.assertEqual((result.decision, result.reason), ("chat", "question_no_ticket"))
        self.assertEqual((self.github.calls, self.notices), ([], []))

    def test_image_information_question_does_not_swallow_independent_task(self):
        p = self.pipeline(envelope(
            topic([1], verdict="needs_human", category="feedback", type="question",
                  actionable=False, confidence=0.3),
            topic([2], category="task", type="task", title="Bổ sung hướng dẫn SMS")))
        p.process(replace(self.photo(), text="Màn hình này là phiên bản nào?"))
        p.process(self.request())
        self.clock.advance(3)
        result = p.close_due_cases()[0]
        first = self.store.get(self.photo().chat_id, 1)
        second = self.store.get(self.photo().chat_id, 2)
        self.assertEqual((first["decision"], first["reason"], first["issue_number"]),
                         ("chat", "question_no_ticket", None))
        self.assertEqual((second["decision"], second["reason"]), ("issue", "auto_task"))
        self.assertEqual(len(self.github.calls), 1)
        self.assertEqual(self.github.calls[0].labels, ("task", "triage", "source:telegram"))
        self.assertNotIn("Màn hình này là phiên bản nào", self.github.calls[0].body)
        self.assertEqual([notice[1] for notice in self.notices], [2])
        self.assertTrue(self.store.reconcile(result.case_key).balanced)

    def test_after_120_seconds_different_sender_chat_and_forwards_are_separate(self):
        for second in (self.request(seconds=121), replace(self.request(), sender_id=8),
                       replace(self.request(), chat_id=-888),
                       replace(self.request(), sender_id=8, is_forward=True)):
            with self.subTest(second=second):
                store = Store(":memory:", clock=self.clock)
                self.addCleanup(store.close)
                p = self.pipeline()
                p.store = store
                p.process(self.photo())
                p.process(second)
                self.assertNotEqual(store.get(self.photo().chat_id, 1)["case_key"],
                                    store.get(second.chat_id, 2)["case_key"])

    def test_explicit_reply_joins_across_senders_and_time(self):
        p = self.pipeline()
        p.process(self.photo())
        second = replace(self.request(seconds=200), sender_id=8, reply_to_id=1)
        p.process(second)
        self.assertEqual(self.store.get(second.chat_id, 1)["case_key"],
                         self.store.get(second.chat_id, 2)["case_key"])

    def test_missing_download_still_classifies_known_text_without_sending_pixels(self):
        p = self.pipeline(downloader=FakeDownloader())
        p.process(self.photo())
        p.process(self.request())
        self.clock.now = NOW + timedelta(seconds=150)
        result = p.close_due_cases()[0]
        self.assertEqual(result.decision, "issue")
        self.assertEqual(len(self.http.calls), 1)
        payload=json.loads(self.http.calls[0][0].data)
        self.assertFalse(any('images' in m for m in payload['messages']))
        self.assertIn('pixels unavailable',payload['messages'][0]['content'])
        self.assertEqual(len(self.notices), 1)
        self.assertEqual(self.upload.uploads,[])

    def test_model_timeout_and_ambiguous_partition_need_owner(self):
        for answers in ((TimeoutError(), TimeoutError()),
                        (envelope(topic([1], verdict="noise", category="chat"), topic([2])),)):
            with self.subTest(answers=answers):
                self.clock=Clock()
                self.store=Store(':memory:',clock=self.clock);self.addCleanup(self.store.close)
                self.github=FakeGitHub();self.notices=[]
                p = self.pipeline(*answers)
                p.process(self.photo())
                p.process(self.request())
                self.clock.now = NOW + timedelta(seconds=150)
                result = p.close_due_cases()[0]
                if isinstance(answers[0],BaseException):
                    self.assertEqual(result.decision,'pending_review')
                    self.assertEqual(self.github.calls,[])
                    self.assertEqual([n[1] for n in self.notices],[2])
                    self.assertEqual(self.store.get(self.photo().chat_id,2)['reason'],'vision_partition_unresolved')
                else:
                    self.assertEqual(result.decision,'issue')
                    self.assertEqual(len(self.github.calls),1)
                    self.assertNotIn('(chỉ có ảnh)',self.github.calls[0].body)
                    self.assertEqual([n[1] for n in self.notices],[2])
                self.assertTrue(all(not d.exists() for d in self.download.directories))

    def test_lone_photo_never_creates_spurious_issue(self):
        p = self.pipeline()
        p.process(self.photo())
        self.clock.now = NOW + timedelta(seconds=120)
        self.assertEqual(p.close_due_cases()[0].decision, "pending_review")
        self.assertEqual(self.github.calls, [])
        self.assertEqual(self.http.calls, [])
        self.assertTrue(all(not d.exists() for d in self.download.directories))

    def test_type_size_count_and_download_timeout_fail_closed_and_cleanup(self):
        cases = [(replace(self.photo(), image_mime="application/pdf"), None, None),
                 (replace(self.photo(), image_size=1000), AttachmentsConfig(max_bytes=80), None),
                 (self.photo(), AttachmentsConfig(max_bytes=10), None),
                 (self.photo(), AttachmentsConfig(max_per_case=0), None),
                 (self.photo(), None, FakeDownloader({1: (b"not image", "image.jpg")})),
                 (self.photo(), None, FakeDownloader(error=TimeoutError()))]
        for photo, settings, downloader in cases:
            with self.subTest(settings=settings, photo=photo):
                self.store = Store(":memory:", clock=self.clock)
                self.addCleanup(self.store.close)
                p = self.pipeline(attachments=settings, downloader=downloader)
                p.process(photo)
                p.process(self.request())
                self.clock.now = NOW + timedelta(seconds=150)
                self.assertEqual(p.close_due_cases()[0].decision, "issue")
                self.assertEqual(len(self.http.calls),1)
                payload=json.loads(self.http.calls[0][0].data)
                self.assertFalse(any('images' in m for m in payload['messages']))
                self.assertIn('pixels unavailable',payload['messages'][0]['content'])
                self.assertIsNone(p._prepared_images)

    def test_case_api_has_real_image_and_id(self):
        http = FakeHTTP({"message": {"content": json.dumps({"verdict": "needs_human",
                        "missing_info": ["Cần làm gì?"], "key_message_ids": [1]})}})
        model = Classifier(config().ollama, opener=http, case_prompt="case")
        processor = self.pipeline().attachments
        prepared = processor.prepare_images("fake", [self.photo()])
        try:
            result = model.analyze_case([self.photo()], images=prepared.for_messages([self.photo()]))
            self.assertEqual(result.verdict, "needs_human")
            payload = json.loads(http.calls[0][0].data)
            self.assertIn("[1]", payload["messages"][1]["content"])
            self.assertEqual(base64.b64decode(payload["messages"][1]["images"][0]), JPG)
        finally:
            prepared.close()
        self.assertFalse(prepared.directory.exists())

    def test_default_window_is_two_minutes(self):
        self.assertEqual(AggregationConfig().window_minutes, 2)

    def test_per_image_message_payload_retains_total_transcript_budget(self):
        model = Classifier(config().ollama, case_prompt="case")
        prepared = self.pipeline().attachments.prepare_images("fake", [self.photo()])
        try:
            messages = [replace(self.request(i), text="a" * 1500) for i in range(1, 41)]
            messages[0] = replace(messages[0], has_image=True)
            payload = model._messages("system", messages, prepared.for_messages(messages))
            self.assertLessEqual(sum(len(m["content"]) for m in payload[1:]), MAX_TRANSCRIPT_CHARS)
            self.assertTrue(all(f"[{i}]" in payload[-1]["content"] for i in range(1, 41)))
        finally:
            prepared.close()

    def test_final_full_case_payload_contains_all_ids_and_reply_edges(self):
        p = self.pipeline()
        p.process(self.photo())
        p.process(self.request(reply_to_id=1))
        self.clock.now = NOW + timedelta(seconds=150)
        self.assertEqual(p.close_due_cases()[0].decision, "issue")
        payload = json.loads(self.http.calls[0][0].data)["messages"]
        self.assertIn("[1]", payload[-1]["content"])
        self.assertIn("[2] [trả lời 1]", payload[-1]["content"])
        self.assertIn("1, 2", payload[-1]["content"])
        self.assertNotIn("images", payload[-1])
        self.assertIn("không suy ra", payload[0]["content"])

    def test_chunk_boundary_cannot_separate_image_and_request(self):
        p = self.pipeline()
        p.config = replace(p.config, aggregation=AggregationConfig(2, 20, 1))
        p.process(self.photo())
        p.process(self.request(seconds=98))
        self.clock.now = NOW + timedelta(seconds=218)
        result = p.close_due_cases()[0]
        self.assertEqual(result.decision, "pending_review")
        self.assertEqual(self.http.calls, [])
        self.assertEqual(len(self.github.calls), 0)
        self.assertEqual(len(self.upload.uploads), 0)
        self.assertEqual(len(self.notices), 1)
        self.assertEqual(self.notices[0][1],2)
        self.assertTrue(self.store.reconcile(result.case_key).balanced)

    def test_captioned_image_reply_cannot_be_dropped_as_chat_beside_ticket(self):
        p = self.pipeline(envelope(topic([1], verdict="noise", category="chat"), topic([2])))
        p.process(replace(self.photo(), text="ảnh minh họa"))
        p.process(self.request(reply_to_id=1))
        self.clock.now = NOW + timedelta(seconds=150)
        result = p.close_due_cases()[0]
        self.assertEqual(result.decision, "needs_human")
        self.assertEqual(len(self.github.calls), 1)
        self.assertEqual(len(self.upload.uploads), 1)
        self.assertEqual(len(self.notices), 1)

    def test_valid_whole_case_vision_uncertainty_preserves_questions_and_image(self):
        question = "Owner xác nhận cần bổ sung STOP ở mẫu SMS nào?"
        p = self.pipeline(envelope(topic([1, 2], verdict="needs_human", category="feedback",
                          actionable=False, title="Cần xác nhận yêu cầu SMS",
                          summary="Cần xác nhận phạm vi thay đổi mẫu SMS.", missing_info=(question,))))
        request = self.request()
        p.process(self.photo())
        p.process(request)
        self.clock.now = NOW + timedelta(seconds=150)
        result = p.close_due_cases()[0]
        self.assertEqual(result.decision, "needs_human")
        self.assertIn("Cần xác nhận yêu cầu SMS", self.github.calls[0].title)
        self.assertIn("Cần xác nhận phạm vi thay đổi mẫu SMS.", self.github.calls[0].body)
        self.assertIn(question, self.github.calls[0].body)
        self.assertIn(question, self.notices[0][2])
        self.assertEqual(len(self.upload.uploads), 1)
        self.assertEqual(len(self.download.calls), 1)
        self.assertIsNone(p.process(request))
        self.assertEqual(p.close_due_cases(), [])
        self.assertEqual(len(self.github.calls), 1)
        self.assertEqual(len(self.notices), 1)

    def test_vision_generated_parenthesized_phone_redacted_from_issue_and_notice(self):
        for phone in ("+1 (202) 555-0199", "(202) 555 0199", "+1(202)5550199"):
            with self.subTest(phone=phone):
                issue = format_issue(self.request(), classification(title=phone, summary=phone),
                                     "model", False, clarify=(phone,))
                self.assertNotIn("555", issue.title + issue.body)
                self.assertNotIn("555", clean_question(phone))
                self.assertIn("[số đã ẩn]", issue.title)


if __name__ == "__main__":
    unittest.main()

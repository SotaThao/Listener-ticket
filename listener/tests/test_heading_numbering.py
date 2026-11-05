"""Ticket section headings are numbered by code, consecutively over the shown sections."""

import re
import unittest

from tg_triage.issue_format import format_issue
from tg_triage.ticket_templates import active
from tests.helpers import classification, message

IMAGE = (("Ảnh", "https://example.invalid/image.png"),)


def headings(body):
    return re.findall(r"^### .*$", body, re.M)


def numbers(body):
    return [int(m[1]) for m in (re.match(r"### (\d+)\. ", h) for h in headings(body)) if m]


class HeadingNumberingTests(unittest.TestCase):
    def assertConsecutive(self, body):
        found = headings(body)
        self.assertEqual(numbers(body), list(range(1, len(found) + 1)), found)

    def test_bug_numbering_stays_consecutive(self):
        full = dict(environment="Android", steps=["Mở"], expected="Đúng", actual="Sai")
        for details, images, clarify in ((full, IMAGE, ["Hỏi?"]), (full, (), []), ({}, (), ["Hỏi?"])):
            body = format_issue(message(), classification(), "model", False, details=details,
                                images=images, clarify=clarify).body
            self.assertConsecutive(body)
        body = format_issue(message(), classification(), "model", False, details=full).body
        self.assertEqual(headings(body)[:2], ["### 1. Tóm tắt", "### 2. Lỗi"])

    def test_feedback_without_acceptance_or_context(self):
        body = format_issue(message(), classification(type="feedback"), "model", False,
                            details=dict(problem="Vấn đề")).body
        self.assertEqual(headings(body), ["### 1. Tóm tắt", "### 2. Vấn đề / nhu cầu", "### 3. Cần làm rõ"])
        body = format_issue(message(), classification(type="feedback"), "model", False,
                            details=dict(problem="Vấn đề", user_story="Story")).body
        self.assertConsecutive(body)

    def test_task_without_evidence_skips_no_number(self):
        body = format_issue(message(), classification(type="task"), "model", False,
                            details=dict(work="Làm")).body
        self.assertEqual(headings(body), ["### 1. Tóm tắt", "### 2. Việc cần làm", "### 3. Xong khi"])
        with_evidence = format_issue(message(), classification(type="task"), "model", False,
                                     details=dict(work="Làm"), images=IMAGE).body
        self.assertEqual(headings(with_evidence)[2:], ["### 3. Minh họa / Evidence (đầu vào)", "### 4. Xong khi"])

    def test_needs_human_with_and_without_evidence(self):
        with_evidence = format_issue(message(), None, "model", True, clarify=["Hỏi?"], images=IMAGE).body
        self.assertEqual(headings(with_evidence),
                         ["### 1. Tóm tắt", "### 2. Minh họa / Evidence", "### 3. Cần làm rõ"])
        without = format_issue(message(), None, "model", True, clarify=["Hỏi?"]).body
        self.assertEqual(headings(without), ["### 1. Tóm tắt", "### 2. Cần làm rõ"])

    def test_user_content_headings_are_never_numbered(self):
        text = "### Tóm tắt\n### Lỗi"
        body = format_issue(message(text=text), classification(), "model", False,
                            details=dict(actual=text), clarify=[text]).body
        self.assertConsecutive(body)
        self.assertEqual(len(headings(body)), len(numbers(body)))   # user text adds no unnumbered heading
        # renderer level: raw value lines that look like headings are emitted exactly as given
        raw = "x\n### Tóm tắt\n### Cần làm rõ"
        out = active().render("needs-human.md", "body",
                              dict(summary=raw, questions=["### Lỗi"], original="o", footer="f"))
        self.assertIn("### 1. Tóm tắt\nx\n### Tóm tắt\n### Cần làm rõ\n", out)
        self.assertIn("- ### Lỗi", out)
        self.assertEqual(numbers(out), [1, 2])

    def test_comments_are_not_numbered(self):
        out = active().render("comments.md", "enrichment", dict(summary="s", reply_quote="q"))
        self.assertNotRegex(out, r"### \d+\.")

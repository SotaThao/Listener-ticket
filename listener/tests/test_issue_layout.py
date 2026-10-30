"""Offline acceptance tests for Assistant definition 12.16 "Bố cục issue dễ đọc" (cases 53-60)."""

import json
import re
import unittest
from datetime import timedelta

from tg_triage.classifier import parse_case_analysis
from tg_triage.issue_format import format_issue, line_safe
from tests.helpers import NOW, classification, message
from tests.test_issue_templates import (BUG_DETAILS, DOCUMENT, FEEDBACK_DETAILS, IMAGE, TASK_DETAILS,
                                        case_payload)

SUMMARY = "### 📝 Tóm tắt"
CLARIFY = "### ❓ Cần làm rõ"
TIME = re.compile(r"\d{2}/\d{2} \d{2}:\d{2}")


def typed(category, details, messages=None, **kwargs):
    analysis = parse_case_analysis(json.dumps(case_payload(category, details), ensure_ascii=False))
    messages = messages or [message()]
    return format_issue(messages[0], analysis.classification, "model", False, details=analysis.details,
                        case_messages=messages, **kwargs)


def untyped(messages=None, **kwargs):
    messages = messages or [message()]
    kwargs.setdefault("clarify", ["Thông tin nào?"])
    return format_issue(messages[0], None, "model", True, case_messages=messages, **kwargs)


def headings(body):
    return [line for line in body.split("\n") if line.startswith("### ")]


def group(count, senders=None, **extra):
    senders = senders or ["Người thử nghiệm"] * count
    return [message(index, f"nội dung số {index}", sender_name=senders[index - 1],
                    received_at=NOW + timedelta(minutes=index), **extra) for index in range(1, count + 1)]


def display(moment):
    return moment.astimezone().strftime("%d/%m %H:%M")


class IssueLayoutTests(unittest.TestCase):
    def test_53_there_is_no_case_line(self):
        for issue in (typed("bug", BUG_DETAILS, group(3)), typed("feedback", FEEDBACK_DETAILS, group(3)),
                      typed("task", TASK_DETAILS, group(3)), untyped(group(3)),
                      format_issue(group(3)[0], classification(), "model", False, case_messages=group(3))):
            self.assertNotIn("Case:", issue.body)
            self.assertNotIn("trong khoảng", issue.body)
            self.assertNotIn("**Tin ", issue.body)

    def test_54_heading_order_per_issue_type(self):
        full_bug = typed("bug", BUG_DETAILS, images=(IMAGE,))
        self.assertEqual(headings(full_bug.body), [SUMMARY, "### 🐞 Lỗi", "### 🖼 Minh họa / Evidence"])
        bug = typed("bug", {}, images=(IMAGE,), documents=(DOCUMENT,))
        self.assertEqual(headings(bug.body), [SUMMARY, "### 🐞 Lỗi", "### 🖼 Minh họa / Evidence", CLARIFY])
        feedback_details = dict(FEEDBACK_DETAILS, proposal="Thêm bộ lọc", alternatives="Xuất Excel",
                                context="Đối soát cuối tháng")
        feedback = typed("feedback", feedback_details, images=(IMAGE,))
        self.assertEqual(headings(feedback.body), [SUMMARY, "### 💡 Vấn đề / nhu cầu",
                                                   "### ✅ Tiêu chí chấp nhận (nháp)", "### 🧭 Ngữ cảnh"])
        self.assertEqual(headings(typed("feedback", dict(problem="Khó tìm", user_story="Là kế toán...")).body),
                         [SUMMARY, "### 💡 Vấn đề / nhu cầu"])   # empty acceptance / context are hidden
        task = typed("task", dict(TASK_DETAILS, links=["https://example.invalid/mockup"]), images=(IMAGE,))
        self.assertEqual(headings(task.body), [SUMMARY, "### 🛠 Việc cần làm",
                                               "### 🖼 Minh họa / Evidence (đầu vào)", "### 🏁 Xong khi"])
        self.assertEqual(headings(typed("task", {"work": "Thêm bộ lọc"}).body),
                         [SUMMARY, "### 🛠 Việc cần làm", "### 🏁 Xong khi"])
        self.assertEqual(headings(typed("task", {}).body),
                         [SUMMARY, "### 🛠 Việc cần làm", "### 🏁 Xong khi", CLARIFY])
        self.assertEqual(headings(untyped().body), [SUMMARY, CLARIFY])
        self.assertEqual(headings(untyped(images=(IMAGE,), documents=(DOCUMENT,)).body),
                         [SUMMARY, "### 🖼 Minh họa / Evidence", CLARIFY])
        self.assertEqual(headings(format_issue(message(), classification(), "model", False).body), [SUMMARY])

    def test_54_labels_are_bold_and_on_the_same_line_as_their_content(self):
        bug = typed("bug", BUG_DETAILS).body
        self.assertIn("**Môi trường:** Nexora build 42", bug)
        self.assertIn("**Các bước tái hiện:**\n1. Mở màn hình đăng nhập\n2. Nhấn Đăng nhập\n\n**Mong đợi:** Vào", bug)
        self.assertIn("**Thực tế:** Không đăng nhập được", bug)
        task = typed("task", dict(TASK_DETAILS, scope_in="Bộ lọc", scope_out="Xuất file", reason_context="Đối soát",
                                  due="Ngày 20/10")).body
        self.assertIn("**Phạm vi:** Làm — Bộ lọc · Không làm — Xuất file", task)
        self.assertIn("**Lý do:** Đối soát", task)
        self.assertIn("**Hạn / người được nhắc:** Ngày 20/10", task)
        self.assertIn("\n### 🏁 Xong khi\n- [ ] Bộ lọc áp dụng đúng khoảng ngày\n- [ ] Đính kèm evidence", task)

    def test_54_documents_are_listed_with_a_paperclip_and_notes_sit_under_them(self):
        body = typed("bug", BUG_DETAILS, images=(IMAGE,), documents=(DOCUMENT,), notes=("Tài liệu không tải được: X",)).body
        self.assertIn(f"- 📎 [{DOCUMENT[0]}]({DOCUMENT[1]})", body)
        self.assertNotIn("Ảnh đính kèm", body)
        self.assertNotIn("Tài liệu đính kèm", body)
        self.assertLess(body.index(IMAGE[1]), body.index(DOCUMENT[1]))
        self.assertLess(body.index(DOCUMENT[1]), body.index("Tài liệu không tải được: X"))
        self.assertLess(body.index("Tài liệu không tải được: X"), body.index("<details>"))

    def test_54_notes_sit_inside_the_task_evidence_and_feedback_context_sections(self):
        note = "Ảnh đính kèm không tải được: OSError"
        task = typed("task", TASK_DETAILS, images=(IMAGE,), documents=(DOCUMENT,), notes=(note,)).body
        section = task.split("### 🖼 Minh họa / Evidence (đầu vào)\n")[1].split("\n### ")[0]
        self.assertIn(note, section)
        self.assertIn(DOCUMENT[1], section)
        self.assertLess(section.index(DOCUMENT[1]), section.index(note))
        self.assertLess(task.index(note), task.index("### 🏁 Xong khi"))
        only_note = typed("task", TASK_DETAILS, notes=(note,)).body       # a note alone still opens the section
        self.assertIn("### 🖼 Minh họa / Evidence (đầu vào)\n" + note, only_note)
        feedback = typed("feedback", FEEDBACK_DETAILS, images=(IMAGE,), documents=(DOCUMENT,), notes=(note,)).body
        context = feedback.split("### 🧭 Ngữ cảnh\n")[1].split("<details>")[0]
        self.assertIn(note, context)
        self.assertLess(context.index(DOCUMENT[1]), context.index(note))
        self.assertIn("### 🧭 Ngữ cảnh\n" + note, typed("feedback", FEEDBACK_DETAILS, notes=(note,)).body)

    def test_55_original_messages_are_collapsed_with_a_blank_line_after_summary(self):
        messages = group(3, ["Lan", "Lan", "Minh"])
        body = typed("bug", BUG_DETAILS, messages, key_message_ids=[2]).body
        self.assertEqual(body.count("<details>"), 1)
        self.assertEqual(body.count("</details>"), 1)
        lines = body.split("\n")
        start = next(index for index, line in enumerate(lines) if line.startswith("<details>"))
        self.assertEqual(lines[start], f"<details><summary>💬 Tin gốc (3 tin) — Lan, Minh, {display(NOW + timedelta(minutes=1))}</summary>")
        self.assertEqual(lines[start + 1], "")                        # blank line after </summary>
        headers = [line for line in lines if line.startswith("**") and "[mở trên Telegram]" in line]
        self.assertEqual(len(headers), 3)
        for header, item in zip(headers, messages):
            self.assertTrue(header.startswith(f"**{item.sender_name} · {display(item.received_at)}**"), header)
            self.assertIn("[mở trên Telegram](https://t.me/c/1234567890/", header)
        self.assertEqual([line.endswith(" · tin chính") for line in headers], [False, True, False])
        end = lines.index("</details>")
        self.assertEqual(lines[end - 1], "")                          # a quote cannot swallow the closing tag
        self.assertLess(body.index("</details>"), body.index("---\n<sub>"))
        self.assertTrue(body.endswith("tạo tự động bởi tg-triage</sub>"))
        self.assertIn("\n> nội dung số 1\n", body)

    def test_55_time_is_dd_mm_hh_mm_in_the_local_zone(self):
        body = typed("task", TASK_DETAILS, group(2)).body
        expected = display(NOW + timedelta(minutes=1))
        self.assertIn(f"— Người thử nghiệm, {expected}</summary>", body)
        self.assertEqual(body.count(expected), 2)    # summary line + the first message header
        self.assertIsNone(re.search(r"\d{4}-\d{2}-\d{2}T", body))

    def test_55_overflow_line_stays_and_the_title_counts_every_message(self):
        body = typed("bug", BUG_DETAILS, group(13)).body
        self.assertIn("💬 Tin gốc (13 tin)", body)
        self.assertIn("(còn 3 tin khác trong case, xem nhóm Telegram)", body)
        self.assertLess(body.index("(còn 3 tin khác"), body.index("</details>"))

    def test_56_messages_without_text_say_what_they_carry(self):
        photo = message(1, "", has_image=True, received_at=NOW)
        paper = message(2, "", has_document=True, received_at=NOW + timedelta(minutes=1))
        body = untyped([photo, paper]).body
        self.assertIn("> (chỉ có ảnh)", body)
        self.assertIn("> (chỉ có tài liệu)", body)
        with_text = untyped([message(1, "có chữ", has_image=True)]).body
        self.assertNotIn("(chỉ có ảnh)", with_text)
        self.assertIn("> có chữ", with_text)

    def test_57_no_missing_link_wording(self):
        for issue in (typed("bug", BUG_DETAILS, [message(chat_id=-123)]), untyped([message(chat_id=-123)])):
            self.assertNotIn("không có link", issue.body)
            self.assertNotIn("mở trên Telegram", issue.body)
            self.assertIn("**Người thử nghiệm · ", issue.body)

    def test_58_html_that_could_break_the_block_is_escaped(self):
        hostile = "</details> <summary>x</summary> --> <sub>y</sub> <details open>"
        messages = group(2, [hostile, hostile])
        messages[0] = message(1, hostile, sender_name=hostile, received_at=NOW)
        details = dict(BUG_DETAILS, environment=hostile, actual=hostile, error_text=hostile)
        for issue in (typed("bug", details, messages), untyped(messages, clarify=[hostile], text=(hostile, hostile)),
                      format_issue(messages[0], classification(summary=hostile), "model", False,
                                   case_messages=messages)):
            body = issue.body
            self.assertEqual(body.count("</details>"), 1)
            self.assertEqual(body.count("<details>"), 1)
            self.assertEqual(body.count("<summary>"), 1)
            self.assertEqual(body.count("</summary>"), 1)
            self.assertEqual(body.count("<sub>"), 1)
            self.assertEqual(body.count("</sub>"), 1)
            self.assertEqual(body.count("-->"), 1)     # only the code-written marker
            self.assertEqual(body.count("<details"), 1)
            self.assertIn("&lt;/details&gt;", body)
            self.assertIn("&lt;summary&gt;", body)
            self.assertIn("--&gt;", body)

    def test_59_three_or_more_senders_are_shortened_to_two_names_and_a_count(self):
        def summary_line(senders, shuffled=False):
            messages = group(len(senders), senders)
            body = typed("bug", BUG_DETAILS, messages[::-1] if shuffled else messages).body
            return next(line for line in body.split("\n") if line.startswith("<details>"))
        # Chi, An, Bình is NOT alphabetical: the order is the order in time, not sorted by name.
        first = display(NOW + timedelta(minutes=1))
        self.assertIn(f"Tin gốc (3 tin) — Chi, An +1, {first}</summary>", summary_line(["Chi", "An", "Bình"]))
        self.assertIn(f"Tin gốc (3 tin) — Chi, An +1, {first}</summary>",
                      summary_line(["Chi", "An", "Bình"], shuffled=True))   # input order does not matter
        self.assertIn("(5 tin) — Dũng, Chi +2, ", summary_line(["Dũng", "Chi", "An", "Bình", "Dũng"]))
        self.assertIn("(3 tin) — Chi, An, ", summary_line(["Chi", "An", "Chi"]))     # repeated names count once
        self.assertIn("(2 tin) — An, ", summary_line(["An", "An"]))
        self.assertNotIn("+", summary_line(["An", "An"]).split("—")[1])

    def test_60_titles_and_labels_are_unchanged(self):
        for category, details, label in (("bug", BUG_DETAILS, "bug"), ("feedback", FEEDBACK_DETAILS, "feature"),
                                         ("task", TASK_DETAILS, "task")):
            issue = typed(category, details, group(3))
            self.assertEqual(issue.title, f"[{label}] Yêu cầu từ Telegram")
            self.assertEqual(issue.labels, (label, "triage", "source:telegram"))
        legacy = format_issue(message(), classification(), "model", False)
        self.assertEqual((legacy.title, legacy.labels), ("[bug] Lỗi đăng nhập", ("bug", "triage", "source:telegram")))
        human = untyped(text=("Chưa rõ", "Nhờ xem"))
        self.assertEqual((human.title, human.labels),
                         ("[cần phân loại] Chưa rõ", ("needs-human", "source:telegram")))
        with_type = format_issue(message(), classification(), "rule", True, clarify=["?"], text=("Lỗi", "x"))
        self.assertEqual((with_type.title, with_type.labels),
                         ("[bug] Lỗi", ("bug", "triage", "needs-human", "source:telegram")))
        self.assertTrue(legacy.body.startswith("<!-- tg:-1001234567890:1 -->\n"))


# Raw model/user line -> what the issue must contain once block syntax is neutralized (after sanitize).
BLOCK_SYNTAX = (
    ("### x", r"\### x"), ("# x", r"\# x"), ("---", r"\-\-\-"), ("===", r"\=\=\="),
    ("1. x", r"1\. x"), ("1) x", r"1\) x"), ("> x", "&gt; x"), ("- x", r"\- x"), ("+ x", r"\+ x"),
    ("| x", r"\| x"), ("~~~ x", r"\~\~\~ x"), ("- - -", r"\- \- \-"), ("- [ ] x", r"\- \[ \] x"),
    ("-", r"\-"), ("#", r"\#"),
)
# detail key -> text that must precede the escaped value on its line ("" = the value starts the line)
LINE_START = {
    "bug": {"error_text": "", "expected": "**Mong đợi:** ", "actual": "**Thực tế:** ",
            "environment": "**Môi trường:** "},
    "feedback": {"problem": "", "context": "", "user_story": "**User story:** ", "proposal": "**Đề xuất:** "},
    "task": {"work": "", "reason_context": "**Lý do:** ", "scope_in": "**Phạm vi:** Làm — ",
             "due": "**Hạn / người được nhắc:** "},
}


def before_original(body):
    return body.split("<details>")[0]


class LineStartSyntaxTests(unittest.TestCase):
    """12.16: text that starts a Markdown line must stay literal (no heading, list, quote, rule)."""

    def test_61_summary_that_looks_like_block_syntax_is_literal(self):
        for raw, escaped in BLOCK_SYNTAX:
            with self.subTest(raw=raw):
                legacy = format_issue(message(), classification(summary=raw), "model", False)
                human = untyped(text=("Tiêu đề", raw))
                for issue, expected in ((legacy, [SUMMARY]), (human, [SUMMARY, CLARIFY])):
                    lines = before_original(issue.body).split("\n")
                    self.assertEqual(lines[lines.index(SUMMARY) + 1], escaped)
                    self.assertNotIn(raw, lines)
                    self.assertEqual(headings(issue.body), expected)
        analysis = parse_case_analysis(json.dumps(case_payload("bug", BUG_DETAILS, summary="### Lỗi giả"),
                                                  ensure_ascii=False))
        body = format_issue(message(), analysis.classification, "model", False, details=analysis.details).body
        self.assertEqual(headings(body), [SUMMARY, "### 🐞 Lỗi", "### 🖼 Minh họa / Evidence"])
        self.assertIn(SUMMARY + "\n\\### Lỗi giả\n", body)

    def test_62_detail_values_that_look_like_block_syntax_are_literal(self):
        for category, keys in LINE_START.items():
            for key, prefix in keys.items():
                benign = {key: "ổn"}
                if category == "feedback" and key != "problem":
                    benign["problem"] = "Khó tìm"
                for raw, escaped in BLOCK_SYNTAX:
                    with self.subTest(category=category, key=key, raw=raw):
                        details = dict(benign, **{key: raw})
                        body = before_original(typed(category, details).body)
                        lines = body.split("\n")
                        self.assertIn(prefix + escaped, lines if prefix == "" else
                                      [line[:len(prefix) + len(escaped)] for line in lines])
                        self.assertNotIn(raw, lines)
                        self.assertEqual(headings(body), headings(before_original(typed(category, benign).body)))

    def test_62_list_items_and_links_that_look_like_block_syntax_are_literal(self):
        for raw, escaped in BLOCK_SYNTAX:
            with self.subTest(raw=raw):
                bug = typed("bug", dict(BUG_DETAILS, steps=[raw])).body
                self.assertIn(f"**Các bước tái hiện:**\n1. {escaped}\n\n", bug)
                self.assertEqual(headings(bug), [SUMMARY, "### 🐞 Lỗi", "### 🖼 Minh họa / Evidence"])
                feedback = typed("feedback", dict(FEEDBACK_DETAILS, acceptance=[raw])).body
                self.assertIn(f"\n- [ ] {escaped}\n", feedback)
                self.assertEqual(headings(feedback), [SUMMARY, "### 💡 Vấn đề / nhu cầu",
                                                      "### ✅ Tiêu chí chấp nhận (nháp)"])
                task = typed("task", dict(TASK_DETAILS, done=[raw], links=[raw])).body
                self.assertIn(f"\n- [ ] {escaped}\n- [ ] Đính kèm evidence", task)
                self.assertIn(f"### 🖼 Minh họa / Evidence (đầu vào)\n- {escaped}\n", task)
                self.assertEqual(headings(task), [SUMMARY, "### 🛠 Việc cần làm",
                                                  "### 🖼 Minh họa / Evidence (đầu vào)", "### 🏁 Xong khi"])

    def test_62_clarify_questions_that_look_like_block_syntax_are_literal(self):
        for raw, escaped in BLOCK_SYNTAX:
            with self.subTest(raw=raw):
                for body in (untyped(clarify=[raw]).body, typed("bug", BUG_DETAILS, clarify=[raw]).body):
                    self.assertIn(f"{CLARIFY}\n- {escaped}\n", body)
                    self.assertEqual(headings(body)[-1], CLARIFY)

    def test_62_original_message_lines_are_literal_inside_the_quote(self):
        raw_lines = [raw for raw, _ in BLOCK_SYNTAX] + ["    indented code"]
        body = untyped([message(1, "\n".join(raw_lines))]).body
        quoted = body.split("</summary>\n\n")[1].split("\n\n</details>")[0].split("\n")[1:]
        expected = [escaped for _, escaped in BLOCK_SYNTAX] + ["&#32;   indented code"]
        self.assertEqual(quoted, ["> " + line for line in expected])
        self.assertEqual(headings(body), [SUMMARY, CLARIFY])

    def test_62_ordinary_text_is_not_changed_and_the_helper_is_idempotent(self):
        for line in ("Đăng nhập lỗi", "-5 độ", "3.14 là pi", "a - b", "1.2.3.4", "2026-10-12 09:00"):
            self.assertIn("> " + line, untyped([message(1, line)]).body)
            self.assertEqual(line_safe(line), line)
        for _, escaped in BLOCK_SYNTAX:
            self.assertEqual(line_safe(escaped), escaped)


if __name__ == "__main__":
    unittest.main()

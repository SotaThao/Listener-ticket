"""Contact data is redacted in issues; member-administration talk is judged by the model, not by code.

Offline: fake model, fake GitHub, in-memory store. Raw contact data stays in SQLite (internal).

12.12 changed this module: the keyword guard `rules.is_admin_chat` is gone, so `IsAdminChatTests` and the
reason `admin_chat` are gone with it. What keeps administrative talk out of GitHub now is (a) the
model's chat verdict and (b) the safety lock "no tag and no command => never an issue".
"""

import unittest
from pathlib import Path

from tg_triage.issue_format import EMAIL_PLACEHOLDER, PHONE_PLACEHOLDER, format_issue, redact, sanitize
from tests.helpers import NOW, FakeCaseModel, FakeTopicModel, classification, message, ticket, topic
from tests.test_cases import CHAT, CaseBase, msg
from tests.test_topics import TopicBase

PROMPTS = Path(__file__).resolve().parents[1] / "prompts"
EMAIL = "Linh.nc@vlinkgroup.net"
ADMIN = ("Invite a vào repo đi", EMAIL, "e gửi invite rồi a")


def issue_for(text, **fields):
    return format_issue(message(1, text), classification(**fields), "model", False)


def chat(ids, **extra):
    """What the model says about plain talk: category chat, verdict noise."""
    fields = dict(verdict="noise", type="task", category="chat", title="Trao đổi hành chính")
    fields.update(extra)
    return topic(ids, **fields)


class RedactionTests(unittest.TestCase):
    def test_a_email_is_hidden_in_title_summary_quote_sender_and_clarify(self):
        item = message(1, f"Liên hệ {EMAIL} nhé", sender_name="linh_nc@vlink.vn")
        value = classification(title=f"Gửi cho {EMAIL}", summary=f"Mail {EMAIL} và a_b@x.io")
        issue = format_issue(item, value, "model", False)
        everything = issue.title + issue.body
        self.assertNotIn("vlinkgroup", everything)
        self.assertNotIn("linh_nc", everything)
        self.assertNotIn("a_b@", everything)
        self.assertIn(EMAIL_PLACEHOLDER, issue.title)
        self.assertEqual(issue.body.count(EMAIL_PLACEHOLDER), 5)   # summary x2, quote, sender x2 (<summary> line + message header)
        needs = format_issue(item, None, "model", True, clarify=(f"Hỏi lại {EMAIL}?",),
                             text=(EMAIL, EMAIL))
        self.assertNotIn("vlinkgroup", needs.title + needs.body)
        self.assertIn("### ❓ Cần làm rõ\n- Hỏi lại " + EMAIL_PLACEHOLDER, needs.body)

    def test_b_phone_numbers_are_hidden(self):
        for number in ("0901234567", "+84 901 234 567", "090-123-4567", "090.123.4567"):
            with self.subTest(number=number):
                issue = issue_for(f"gọi em {number} nha", title=f"Gọi {number}", summary=f"SĐT {number}")
                self.assertNotIn(number, issue.title + issue.body)
                self.assertNotIn("901", issue.title + issue.body)
                self.assertIn(PHONE_PLACEHOLDER, issue.title)
                self.assertEqual(issue.body.count(PHONE_PLACEHOLDER), 2)   # summary + quote

    def test_redaction_leaves_other_numbers_alone(self):
        for text in ("12345678", "123456789012", "2026-10-12 09:00", "phiên bản 1.2.3.4"):
            self.assertEqual(redact(sanitize(text, inline=True)), sanitize(text, inline=True), text)

    def test_the_code_written_timestamp_and_link_are_not_redacted(self):
        body = issue_for("lỗi in hoá đơn").body
        self.assertIn(NOW.astimezone().strftime("%d/%m %H:%M"), body)
        self.assertIn("](https://t.me/c/1234567890/1)", body)   # 10-digit chat id in a link

    def test_a_long_message_cut_for_the_quote_never_leaves_half_an_address(self):
        item = message(1, "x" * 3990 + " " + EMAIL)
        body = format_issue(item, classification(), "model", False).body
        self.assertNotIn("vlinkgroup", body)
        self.assertNotIn("Linh.nc", body)


class AdminTalkPipelineTests(TopicBase):
    """Untagged administrative talk: the model says chat, or the no-tag lock stops a wrong ticket.
    `tagged` = 1-based indexes of the messages that tag the listener; None = every message does."""

    def run_window(self, texts, *answers, mode="live", tagged=None):
        pipeline = self.pipeline(FakeTopicModel(*answers), mode=mode)
        for index, text in enumerate(texts, 1):
            pipeline.process(msg(index, index, text=text,
                                 mentions_listener=tagged is None or index in tagged))
        self.close(pipeline)

    def test_c_untagged_admin_group_the_model_calls_chat_is_chat_with_the_models_reason(self):
        self.run_window(ADMIN, (chat([1, 2, 3], reason="Trao đổi mời thành viên, không có việc cho team."),),
                        tagged=())
        self.assertEqual(self.github.calls, [])
        self.assertEqual(self.decisions(1, 2, 3), ["chat"] * 3)
        self.assertEqual([self.row(i)["reason"] for i in (1, 2, 3)], ["model_chat"] * 3)
        self.assertEqual({self.row(i)["model_reason"] for i in (1, 2, 3)},
                         {"Trao đổi mời thành viên, không có việc cho team."})
        self.assertEqual(self.store.get_case(f"{CHAT}:1")["status"], "chat")
        tally = self.reconciliation()
        self.assertEqual((tally["received"], tally["issue"], tally["chat"], tally["balanced"]), (3, 0, 3, 1))

    # 12.13 changed this test (was: "untagged admin group is never a ticket even when the model says
    # ticket"). The no-tag lock is gone: a confident task is now an automatic issue (`auto_task`);
    # only a model that is NOT sure (needs_human) waits in pending_review (`pending_untagged`).
    def test_c_untagged_admin_group_the_model_calls_a_task_is_an_automatic_issue(self):
        self.run_window(ADMIN, (topic([1, 2, 3], verdict="ticket", type="task", category="task",
                                      title="Mời thành viên"),), tagged=())
        self.assertEqual(len(self.github.calls), 1)
        self.assertEqual(self.decisions(1, 2, 3), ["issue"] * 3)
        self.assertEqual([self.row(i)["reason"] for i in (1, 2, 3)], ["auto_task"] * 3)

    def test_c_untagged_admin_group_the_model_is_not_sure_about_asks_for_clarification(self):
        self.run_window(ADMIN, (topic([1, 2, 3], verdict="needs_human", type="task", category="task",
                                      title="Mời thành viên"),), tagged=())
        self.assertEqual(len(self.github.calls), 1)
        self.assertIn("needs-human", self.github.calls[0].labels)
        self.assertEqual(self.decisions(1, 2, 3), ["needs_human"] * 3)
        self.assertEqual([self.row(i)["reason"] for i in (1, 2, 3)], ["auto_needs_human"] * 3)
        self.assertEqual(self.numbers(1, 2, 3), [101] * 3)
        tally = self.reconciliation()
        self.assertEqual((tally["received"], tally["issue"], tally["pending_review"],
                          tally["balanced"]), (3, 0, 0, 1))

    def test_c_the_original_text_stays_in_the_database(self):
        self.run_window(ADMIN, (topic([1, 2, 3], type="task"),), tagged=())
        self.assertEqual(self.row(2)["text"], EMAIL)

    # 12.13 changed this test (was: dry-run also sets an untagged task aside). A confident task is an issue
    # without a tag, so dry-run records dry_run_issue; an unsure one still waits for review.
    def test_c_dry_run_records_the_automatic_issue_and_the_unsure_group(self):
        self.run_window(ADMIN, (topic([1, 2, 3], type="task"),), mode="dry-run", tagged=())
        self.assertEqual(self.decisions(1, 2, 3), ["dry_run_issue"] * 3)
        self.setUp()
        self.run_window(ADMIN, (topic([1, 2, 3], type="task", confidence=0.4),), mode="dry-run", tagged=())
        self.assertEqual(self.decisions(1, 2, 3), ["dry_run_needs_human"] * 3)

    def test_d_a_bug_command_with_an_email_is_still_a_ticket_with_the_email_hidden(self):
        self.run_window((f"/bug không in được hoá đơn, báo {EMAIL}",),
                        (topic([1], type="bug", title="Không in được hoá đơn"),))
        self.assertEqual(len(self.github.calls), 1)
        issue = self.github.calls[0]
        self.assertEqual(issue.labels[0], "bug")
        self.assertTrue(issue.title.startswith("[bug]"))
        self.assertNotIn("vlinkgroup", issue.title + issue.body)
        self.assertIn(EMAIL_PLACEHOLDER, issue.body)

    def test_d_a_ticket_made_by_the_model_hides_email_and_phone_everywhere(self):
        self.run_window((f"không in được hoá đơn, gọi em 0901234567 hoặc mail {EMAIL}",),
                        (topic([1], title=f"Lỗi in, liên hệ {EMAIL}", summary="Gọi 0901234567 khi xong."),))
        self.assertEqual(len(self.github.calls), 1)
        issue = self.github.calls[0]
        everything = issue.title + issue.body
        for secret in ("vlinkgroup", "Linh.nc", "0901234567", "901234"):
            self.assertNotIn(secret, everything)
        self.assertIn(EMAIL_PLACEHOLDER, everything)
        self.assertIn(PHONE_PLACEHOLDER, everything)

    def test_d_a_command_keeps_an_otherwise_admin_group_a_ticket(self):
        self.run_window(("Invite a vào repo đi", "/task thêm Linh vào repo"),
                        (topic([1, 2], type="task", title="Thêm Linh vào repo"),))
        self.assertEqual(len(self.github.calls), 1)
        self.assertEqual(self.decisions(1, 2), ["issue", "issue"])

    def test_e_a_real_bug_group_is_still_a_ticket(self):
        self.run_window(("không đăng nhập được app", "mình dùng iPhone"), (topic([1, 2]),))
        self.assertEqual(len(self.github.calls), 1)
        self.assertEqual(self.decisions(1, 2), ["issue", "issue"])

    def test_a_bug_group_that_also_mentions_an_email_is_still_a_ticket_and_redacted(self):
        self.run_window((f"không đăng nhập được app, mail {EMAIL}",), (topic([1]),))
        self.assertEqual(len(self.github.calls), 1)
        self.assertNotIn("vlinkgroup", self.github.calls[0].body)

    def test_a_mixed_window_keeps_the_ticket_group_and_sets_only_the_chat_group_aside(self):
        self.run_window(("Invite a vào repo đi", "không đăng nhập được app", EMAIL),
                        (chat([1, 3]), topic([2])), tagged=(2,))
        self.assertEqual(len(self.github.calls), 1)
        self.assertEqual(self.decisions(1, 2, 3), ["chat", "issue", "chat"])
        self.assertEqual(self.row(1)["reason"], "model_chat")


class AdminTalkSingleCaseModelTests(CaseBase):
    """A classifier without `split_topics` judges the whole window; the same lock applies there."""

    def close(self, pipeline, texts, tagged=True):
        for index, text in enumerate(texts, 1):
            pipeline.process(msg(index, index, text=text, mentions_listener=tagged))
        self.clock.advance(60)
        pipeline.close_due_cases()

    # 12.13 changed this test (was: an untagged window a case model calls a ticket creates no issue).
    def test_untagged_admin_window_a_case_model_calls_a_task_is_an_automatic_issue(self):
        pipeline = self.pipeline(model=FakeCaseModel(ticket([1, 2, 3], type="task")))
        self.close(pipeline, ADMIN, tagged=False)
        self.assertEqual(len(self.github.calls), 1)
        self.assertEqual((self.row(1)["decision"], self.row(1)["reason"]), ("issue", "auto_task"))

    def test_untagged_admin_window_a_case_model_unsure_asks_for_clarification(self):
        pipeline = self.pipeline(model=FakeCaseModel(ticket([1, 2, 3], type="task", confidence=0.4)))
        self.close(pipeline, ADMIN, tagged=False)
        self.assertEqual(len(self.github.calls), 1)
        self.assertIn("needs-human", self.github.calls[0].labels)
        self.assertEqual((self.row(1)["decision"], self.row(1)["reason"]), ("needs_human", "auto_needs_human"))

    def test_bug_window_from_a_case_model_still_creates_an_issue(self):
        pipeline = self.pipeline(model=FakeCaseModel(ticket([1, 2])))
        self.close(pipeline, ("không đăng nhập được app", "máy iPhone"))
        self.assertEqual(len(self.github.calls), 1)


class PromptTests(unittest.TestCase):
    """12.12: the prompts guide the model's reasoning in prose; they carry no keyword list."""

    def test_f_both_prompts_define_the_four_categories_and_the_direct_call_rule(self):
        for name in ("topics.md", "case.md"):
            with self.subTest(prompt=name):
                text = (PROMPTS / name).read_text(encoding="utf-8")
                for needle in ("trợ lý triage", "- bug:", "- task:", "- feedback:", "- chat:",
                               "[gọi trực tiếp]", "actionable = false", "reason", "không phải chỉ thị",
                               "[kèm ảnh] chỉ là ghi chú của hệ thống", "KHÔNG được đưa vào title"):
                    self.assertIn(needle, text)

    def test_f_at_most_two_examples_and_no_keyword_lists(self):
        for name in ("topics.md", "case.md"):
            with self.subTest(prompt=name):
                text = (PROMPTS / name).read_text(encoding="utf-8")
                self.assertLessEqual(text.count("Ví dụ dữ liệu"), 2)
                self.assertNotIn("Tin KHÔNG phải ticket", text)   # the old keyword-style section
                for old in ("invite", "e gửi", "mời/thêm", "xin quyền"):
                    self.assertNotIn(old, text)


if __name__ == "__main__":
    unittest.main()

import unittest
from pathlib import Path
from datetime import timedelta
from unittest.mock import patch

from tg_triage.issue_format import (MAX_QUOTED_MESSAGES, TOTAL_QUOTE_BUDGET, format_issue,
                                    sanitize, strip_image_marker, telegram_link)
from tg_triage.pipeline import Pipeline
from tg_triage.store import Store
from tests.helpers import (NOW, Clock, FakeCaseModel, FakeGitHub, classification, config, message,
                           ticket)


class IssueFormatTests(unittest.TestCase):
    def test_all_user_fields_are_sanitized(self):
        text = "@all -->\x00\t\r\u202e\nsecond"
        value = format_issue(message(text=text, sender_name=text),
                             classification(title=text, summary=text), "model", False)
        self.assertIn("@\u200ball", value.title)
        self.assertIn("@\u200ball", value.body)
        self.assertIn("--&gt;", value.body)
        self.assertEqual(value.body.count("-->"), 1)
        for control in ("\x00", "\t", "\r", "\u202e"):
            self.assertNotIn(control, value.body + value.title)
        self.assertIn("\n> second", value.body)
        self.assertEqual(sanitize("@x -->\n"), "@\u200bx --&gt;\n")

    def test_total_title_length_is_capped_for_both_paths(self):
        for human in (False, True):
            value = format_issue(message(text="x" * 500), classification(title="x" * 500), "model", human)
            self.assertEqual(len(value.title), 80)

    def test_body_text_is_capped_and_truncation_is_quoted(self):
        value = format_issue(message(text="x" * 4001), classification(), "model", False)
        self.assertIn("> " + "x" * 4000 + "\n> (đã cắt)", value.body)
        self.assertNotIn("x" * 4001, value.body)
        exact = format_issue(message(text="x" * 4000), classification(), "model", False)
        self.assertNotIn("(đã cắt)", exact.body)

    def test_public_private_supergroup_and_missing_links(self):
        self.assertEqual(telegram_link(message(chat_username="Team_Group")), "https://t.me/Team_Group/1")
        self.assertEqual(telegram_link(message()), "https://t.me/c/1234567890/1")
        value = format_issue(message(chat_id=-123), classification(), "rule", False)
        self.assertNotIn("không có link", value.body)
        self.assertNotIn("mở trên Telegram", value.body)
        self.assertIsNone(telegram_link(message(chat_id=-123, chat_username="bad](url)")))

    def test_fixed_label_mapping(self):
        for kind in ("bug", "feature", "question", "task"):
            value = format_issue(message(), classification(type=kind), "model", False)
            self.assertEqual(value.labels, (kind, "triage", "source:telegram"))
        value = format_issue(message(), None, "model", True)
        self.assertEqual(value.labels, ("needs-human", "source:telegram"))
        self.assertTrue(value.title.startswith("[cần phân loại] "))
        self.assertIn("### 📝 Tóm tắt\n(agent không chắc chắn, cần người đọc)", value.body)

    def test_noise_and_invalid_types_cannot_format_confident_issue(self):
        for value in (None, classification(type="noise"), classification(type="admin")):
            with self.assertRaises(ValueError):
                format_issue(message(), value, "model", False)

    def test_marker_timestamp_and_vietnamese_template(self):
        msg = message()
        model = format_issue(msg, classification(), "model", False)
        self.assertTrue(model.body.startswith("<!-- tg:-1001234567890:1 -->\n### 📝 Tóm tắt\n"))
        self.assertIn(msg.received_at.astimezone().strftime("%d/%m %H:%M"), model.body)
        self.assertNotIn(msg.received_at.astimezone().isoformat(), model.body)
        self.assertIn("<sub>Phân loại: tầng 2 qwen3.5:9b, confidence 0.83 · tạo tự động bởi tg-triage</sub>",
                      model.body)
        rule = format_issue(msg, classification(), "rule", False)
        self.assertIn("Phân loại: tầng 1 lệnh", rule.body)


def case(count, **extra):
    """`count` messages 1..count, one minute apart, each with a distinct text."""
    return [message(i, f"nội dung số {i}", received_at=NOW + timedelta(minutes=i), **extra)
            for i in range(1, count + 1)]


def render(messages, keys=None, **extra):
    return format_issue(messages[0], classification(), "model", False,
                        case_messages=messages, key_message_ids=keys, **extra).body


def quoted_chars(body):
    """Characters of quoted text: '> ' lines excluding the code-written truncation notice."""
    return sum(len(line) - 2 for line in body.split("\n")
               if line.startswith("> ") and line != "> (đã cắt)")


class WholeCaseTests(unittest.TestCase):
    def test_all_messages_rendered_and_only_key_marked(self):  # (a)
        body = render(case(3), keys=[2])
        for number in (1, 2, 3):
            self.assertIn(f"nội dung số {number}", body)
        self.assertEqual(body.count("**Người thử nghiệm · "), 3)
        marks = [line for line in body.split("\n") if "tin chính" in line]
        self.assertEqual(len(marks), 1)
        self.assertTrue(marks[0].startswith("**Người thử nghiệm · "))
        self.assertIn("\n> nội dung số 2", body.split(marks[0])[1][:40])   # the marked one is message 2

    def test_no_valid_key_means_no_marker(self):  # (b)
        for keys in (None, [], (), [999], [True]):
            body = render(case(3), keys=keys)
            for number in (1, 2, 3):
                self.assertIn(f"nội dung số {number}", body)
            self.assertNotIn("tin chính", body)

    def test_single_message_case_keeps_original_label_and_has_no_case_line(self):  # (c)
        body = render(case(1), keys=[1])
        self.assertIn("💬 Tin gốc (1 tin) — Người thử nghiệm, ", body)
        self.assertNotIn("Case:", body)
        self.assertNotIn("**Tin 1**", body)
        self.assertNotIn("case 1 tin", body)
        self.assertIn("<sub>Phân loại: tầng 2 qwen3.5:9b, confidence 0.83 · tạo tự động bởi tg-triage</sub>",
                      body)

    def test_multi_message_case_line_and_provenance(self):
        messages = case(3)
        body = render(messages)
        self.assertIn("### 📝 Tóm tắt\nĐăng nhập thất bại.\n\n<details><summary>💬 Tin gốc (3 tin)", body)
        self.assertNotIn("Case:", body)
        self.assertTrue(body.endswith("confidence 0.83 · case 3 tin · tạo tự động bởi tg-triage</sub>"))
        self.assertTrue(body.startswith("<!-- tg:-1001234567890:1 -->\n"))

    def test_messages_are_ordered_by_received_time_then_id(self):  # (d)
        early = message(30, "sớm nhất", received_at=NOW)
        tie_low = message(5, "cùng giờ id thấp", received_at=NOW + timedelta(minutes=1))
        tie_high = message(9, "cùng giờ id cao", received_at=NOW + timedelta(minutes=1))
        late = message(1, "muộn nhất", received_at=NOW + timedelta(minutes=2))
        body = render([late, tie_high, early, tie_low], keys=[1])
        positions = [body.index(text) for text in
                     ("sớm nhất", "cùng giờ id thấp", "cùng giờ id cao", "muộn nhất")]
        self.assertEqual(positions, sorted(positions))
        marked = [line for line in body.split("\n") if "tin chính" in line]
        self.assertEqual(len(marked), 1)
        self.assertIn("\n> muộn nhất", body.split(marked[0])[1][:40])   # message 1 is the last in time order

    def test_every_message_is_sanitized(self):  # (e)
        hostile = "a --> @user https://evil.invalid <img src=x> #12 [x](y)"
        messages = [message(i, hostile, sender_name="@boss -->", received_at=NOW + timedelta(minutes=i))
                    for i in (1, 2, 3)]
        body = render(messages, keys=[2])
        self.assertEqual(body.count("-->"), 1)  # only the code-written marker
        self.assertNotIn("<img", body)
        self.assertNotIn("@user", body)
        self.assertNotIn("@boss", body)
        quote = body.split('<details>', 1)[1]
        self.assertNotIn("://evil", quote)
        self.assertEqual(body.count("://evil"), 1)
        self.assertIn('- 🔗 [evil.invalid](https://evil.invalid)', body.splitlines())
        self.assertEqual(body.count("--&gt;"), 7)  # text + sender, three messages, + sender in <summary>
        self.assertEqual(body.count("&lt;img"), 3)

    def test_overflow_is_announced_not_silent(self):  # (f)
        total = MAX_QUOTED_MESSAGES + 3
        body = render(case(total), keys=[total])
        self.assertIn("(còn 3 tin khác trong case, xem nhóm Telegram)", body)
        self.assertIn(f"💬 Tin gốc ({total} tin)", body)   # the title counts every message of the case
        self.assertIn(f"nội dung số {MAX_QUOTED_MESSAGES}", body)
        self.assertNotIn(f"nội dung số {MAX_QUOTED_MESSAGES + 1}", body)
        self.assertIn(f"case {total} tin", body)
        self.assertNotIn("còn", render(case(MAX_QUOTED_MESSAGES)))

    def test_total_quoted_text_stays_within_budget(self):  # (g)
        for count in (1, 3, MAX_QUOTED_MESSAGES, MAX_QUOTED_MESSAGES + 5):
            messages = [message(i, "x" * 9000, received_at=NOW + timedelta(minutes=i))
                        for i in range(1, count + 1)]
            body = render(messages)
            self.assertLessEqual(quoted_chars(body), TOTAL_QUOTE_BUDGET)
            self.assertEqual(body.count("(đã cắt)"), min(count, MAX_QUOTED_MESSAGES))
        self.assertNotIn("(đã cắt)", render(case(3)))


class PipelinePassesWholeCaseTests(unittest.TestCase):
    def make(self, analysis):
        clock = Clock()
        store = Store(":memory:", clock=clock)
        self.addCleanup(store.close)
        self.github = FakeGitHub()
        pipeline = Pipeline(config("live", 20, window=15), store, FakeCaseModel(analysis), self.github)
        for number in (1, 2, 3):
            pipeline.process(message(number, f"app nexora lỗi bước {number}", sender_id=1,
                                     received_at=NOW + timedelta(minutes=number)))
        clock.advance(60)
        return pipeline

    def test_model_choosing_one_key_still_gets_all_messages_rendered(self):  # (h)
        pipeline = self.make(ticket([2]))
        pipeline.close_due_cases()
        self.assertEqual(len(self.github.calls), 1)
        body = self.github.calls[0].body
        for number in (1, 2, 3):
            self.assertIn(f"bước {number}", body)
        self.assertEqual(body.count("tin chính"), 1)
        self.assertIn("Tin gốc (3 tin)", body)
        self.assertNotIn("Case:", body)

    def test_pipeline_passes_every_message_and_only_model_keys(self):
        pipeline = self.make(ticket([2, 77]))
        with patch("tg_triage.pipeline.format_issue", wraps=format_issue) as spy:
            pipeline.close_due_cases()
        kwargs = spy.call_args.kwargs
        self.assertEqual([item.message_id for item in kwargs["case_messages"]], [1, 2, 3])
        self.assertEqual(tuple(kwargs["key_message_ids"]), (2,))



class ImageMarkerTests(unittest.TestCase):
    """The system's "[kèm ảnh]" note must never reach a title or summary (but stays in the quote)."""
    HINT = "Dấu [kèm ảnh] chỉ là ghi chú của hệ thống, KHÔNG được đưa vào title hoặc summary."

    def title(self, title, **extra):
        return format_issue(message(), classification(title=title), "model", False, **extra).title

    def test_marker_is_removed_from_a_model_title(self):  # (a)
        self.assertEqual(self.title("Không quét được QR [kèm ảnh]"), "[bug] Không quét được QR")
        self.assertEqual(self.title("Lỗi đăng nhập có kèm ảnh"), "[bug] Lỗi đăng nhập")

    def test_every_variant_in_any_case_and_spacing(self):
        for raw in ("[kèm ảnh] Lỗi đăng nhập", "Lỗi đăng nhập (có kèm ảnh)", "Lỗi đăng nhập [có kèm ảnh]",
                    "Lỗi   đăng nhập KÈM ẢNH", "Lỗi đăng nhập  [ Kèm  Ảnh ]", "Lỗi đăng nhập - (Có kèm ảnh)",
                    "Có Kèm Ảnh: Lỗi đăng nhập", "Lỗi đăng nhập, kèm ảnh.", "Lỗi \t đăng nhập\nkèm ảnh",
                    "Lỗi đăng nhập kèm ảnh"):
            with self.subTest(raw=raw):
                self.assertEqual(strip_image_marker(raw), strip_image_marker(raw).strip())
                self.assertEqual(strip_image_marker(raw).split(), ["Lỗi", "đăng", "nhập"])
                self.assertNotIn("kèm ảnh", " ".join(self.title(raw).lower().split()))

    def test_decomposed_unicode_is_matched_too(self):
        import unicodedata
        raw = unicodedata.normalize("NFD", "Lỗi đăng nhập [kèm ảnh]")
        self.assertEqual(strip_image_marker(raw), "Lỗi đăng nhập")

    def test_text_without_the_marker_is_not_rewritten(self):
        for raw in ("Lỗi (QR) khi quét.", "  - x -  ", "Ảnh bị mờ", "Không kèm theo tệp", "Lỗixkèm ảnh"):
            self.assertEqual(strip_image_marker(raw), raw)

    def test_hanging_punctuation_left_by_the_removal_is_dropped(self):
        self.assertEqual(strip_image_marker("Lỗi QR ([kèm ảnh]"), "Lỗi QR")
        self.assertEqual(strip_image_marker("Lỗi (QR) (kèm ảnh)"), "Lỗi (QR)")
        self.assertEqual(strip_image_marker("Lỗi QR - [kèm ảnh] -"), "Lỗi QR")
        self.assertEqual(strip_image_marker("[kèm ảnh]"), "")
        self.assertEqual(strip_image_marker(None), "")

    def test_a_title_that_is_only_the_marker_falls_back_to_the_original_message(self):  # (b)
        value = format_issue(message(text="App treo khi mở camera [kèm ảnh]"),
                             classification(title="[kèm ảnh]"), "model", False)
        self.assertEqual(value.title, "[bug] App treo khi mở camera")
        self.assertNotIn("kèm ảnh", value.title)
        value = format_issue(message(text="[kèm ảnh]"), classification(title="(có kèm ảnh)"), "model", False)
        self.assertEqual(value.title, "[bug] (không có tiêu đề)")

    def test_fallback_skips_a_first_message_that_is_only_the_marker(self):
        first = message(1, "[kèm ảnh]", received_at=NOW)
        second = message(2, "Ứng dụng đóng đột ngột", received_at=NOW + timedelta(minutes=1))
        value = format_issue(first, classification(title="kèm ảnh"), "model", False,
                             case_messages=[first, second])
        self.assertEqual(value.title, "[bug] Ứng dụng đóng đột ngột")

    def test_summary_is_cleaned_too(self):  # (c)
        value = format_issue(message(), classification(summary="Người dùng báo lỗi có kèm ảnh [kèm ảnh]"),
                             "model", False)
        self.assertIn("### 📝 Tóm tắt\nNgười dùng báo lỗi\n", value.body)
        value = format_issue(message(), classification(summary="có kèm ảnh"), "model", False)
        self.assertIn("### 📝 Tóm tắt\n(không có tóm tắt)\n", value.body)

    def test_needs_human_branch_cleans_title_summary_and_fallback(self):  # (4)
        value = format_issue(message(text="Chưa rõ [kèm ảnh]"), None, "model", True,
                             text=("Cần xem lại (có kèm ảnh)", "Chỉ có ảnh, kèm ảnh"))
        self.assertEqual(value.title, "[cần phân loại] Cần xem lại")
        self.assertIn("### 📝 Tóm tắt\nChỉ có ảnh\n", value.body)
        value = format_issue(message(text="Chưa rõ [kèm ảnh]"), None, "model", True, text=("[kèm ảnh]", ""))
        self.assertEqual(value.title, "[cần phân loại] Chưa rõ")
        typed = format_issue(message(), classification(), "rule", True, clarify=["?"],
                             text=("Lỗi QR [kèm ảnh]", "x"))
        self.assertEqual(typed.title, "[bug] Lỗi QR")

    def test_rule_tier_goes_through_the_same_cleaning(self):  # (4)
        value = format_issue(message(), classification(title="Lỗi in [kèm ảnh]", summary="kèm ảnh Lỗi in"),
                             "rule", False)
        self.assertEqual(value.title, "[bug] Lỗi in")
        self.assertIn("### 📝 Tóm tắt\nLỗi in\n", value.body)

    def test_quoted_original_message_keeps_the_marker_verbatim(self):  # (d)
        value = format_issue(message(text="Lỗi này [kèm ảnh] xem giúp"),
                             classification(title="Lỗi [kèm ảnh]", summary="Tóm tắt [kèm ảnh]"),
                             "model", False)
        self.assertEqual(value.title, "[bug] Lỗi")
        self.assertIn("> Lỗi này \\[kèm ảnh\\] xem giúp", value.body)  # Markdown-escaped, same words
        many = [message(1, "một [kèm ảnh]", received_at=NOW),
                message(2, "hai kèm ảnh", received_at=NOW + timedelta(minutes=1))]
        body = format_issue(many[0], classification(), "model", False, case_messages=many).body
        self.assertIn("> một \\[kèm ảnh\\]", body)
        self.assertIn("> hai kèm ảnh", body)

    def test_case_and_topics_prompts_carry_the_instruction(self):  # (e)
        folder = Path(__file__).resolve().parents[1] / "prompts"
        for name in ("case.md", "topics.md"):
            lines = (folder / name).read_text(encoding="utf-8").splitlines()
            self.assertEqual(lines.count(self.HINT), 1, name)

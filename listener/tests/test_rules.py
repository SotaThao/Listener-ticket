import unittest

from tg_triage import rules
from tg_triage.rules import evaluate


class RulesTests(unittest.TestCase):
    def test_all_commands_are_confident_and_keep_payload(self):
        for kind in ("bug", "feature", "question", "task"):
            with self.subTest(kind=kind):
                value = evaluate(f"/{kind} x")
                self.assertEqual(value.type, kind)
                self.assertEqual(value.confidence, 1.0)
                self.assertEqual(value.title, "x")
                self.assertEqual(value.summary, "x")

    def test_case_insensitive_bot_command(self):
        value = evaluate("/BuG@TenBot\n Nội dung lỗi ")
        self.assertEqual(value.type, "bug")
        self.assertEqual(value.summary, "Nội dung lỗi")

    def test_empty_commands_are_noise(self):
        for text in ("/bug", "/FEATURE@TenBot", "/task \n \t"):
            with self.subTest(text=text):
                self.assertEqual(evaluate(text).type, "noise")

    def test_command_requires_start_and_boundary(self):
        for text in ("xin /bug lỗi đăng nhập", "/bugfix vẫn còn vấn đề", " /task làm việc này"):
            self.assertIsNone(evaluate(text))

    # 12.12 changed this test (was: also "abc" and "a b c d e f g", which fell under the 8-character
    # minimum). Only a message with no content to judge (no letter at all) is noise now.
    def test_noise_is_a_message_with_no_letter_at_all(self):
        for text in ("", "   ", "👍😀🎉", "!!!???", "123456789", "0901234567"):
            with self.subTest(text=text):
                self.assertEqual(evaluate(text).type, "noise")

    # 12.12 changed this test (was: "OK!!!", "thanks", "cảm ơn"... are noise). Courtesy words are
    # not matched by code any more: the model reads them in context and decides.
    def test_short_messages_and_courtesies_are_left_to_the_model(self):
        for text in ("OK!!!", "oke", "ừ", "VÂNG", "Dạ", "Cảm ơn!!!", "cám ơn", "THANKS!!!",
                     "thank you.", "Cảm ơn", "abc", "a b c d e f g", "chào anh"):
            with self.subTest(text=text):
                self.assertIsNone(evaluate(text))
                self.assertIsNone(evaluate(text, 99))   # the old length limit is ignored

    def test_normal_text_passes_to_model(self):
        self.assertIsNone(evaluate("Đăng nhập đang báo lỗi 500"))
        self.assertIsNone(evaluate("abcdefgh", 8))

    def test_no_keyword_list_or_keyword_helper_decides_chat_or_ticket(self):
        for name in ("ACK_WORDS", "CONTACT_LABEL_WORDS", "ADMIN_KEYWORDS", "PRODUCT_KEYWORDS", "COURTESIES",
                     "is_admin_chat", "is_contact_or_courtesy_only", "has_contact_data", "has_mention"):
            with self.subTest(name=name):
                self.assertFalse(hasattr(rules, name))

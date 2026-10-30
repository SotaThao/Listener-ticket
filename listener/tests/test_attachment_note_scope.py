import unittest

from tg_triage.attachments import AttachmentProcessor
from tg_triage.config import AttachmentsConfig
from tests.helpers import FakeDownloader, JPG, message


def photo(number, **extra):
    return message(number, has_image=True, image_size=100, **extra)


class AttachmentNoteScopeTests(unittest.TestCase):
    def processor(self, downloader):
        return AttachmentProcessor(AttachmentsConfig(max_per_case=5), None, downloader=downloader)

    def test_prepared_batch_scopes_limit_notes_to_case(self):
        messages = [photo(i) for i in range(1, 8)]
        processor = self.processor(FakeDownloader({i: (JPG, "private.jpg") for i in range(1, 8)}))
        prepared = processor.prepare_images("batch", messages)
        self.addCleanup(prepared.close)
        a = processor.process("a", messages[:1], live=False, prepared=prepared)
        b = processor.process("b", messages[1:], live=False, prepared=prepared)
        self.assertEqual(a.image_skips, [])
        self.assertEqual(len(b.image_skips), 1)
        self.assertEqual(b.image_skips[0][2], "vượt giới hạn số ảnh")
        self.assertEqual((a.dry_run_count, b.dry_run_count), (1, 5))

    def test_unseen_note_counts_only_accepted_case_images(self):
        messages = [photo(i) for i in range(1, 8)]
        processor = self.processor(FakeDownloader({i: (JPG, "private.jpg") for i in range(1, 8)}))
        prepared = processor.prepare_images("batch", messages)
        self.addCleanup(prepared.close)
        a = processor.process("a", messages[:1], live=False, prepared=prepared)
        b = processor.process("b", messages[1:], live=False, prepared=prepared)
        self.assertEqual(a.notes, [])
        self.assertEqual(b.notes, ["Ảnh bị bỏ qua (1 ảnh): vượt giới hạn số ảnh",
                                   "Model chưa xem 1 ảnh đính kèm trong lượt phân tích; người xử lý cần mở các ảnh này để kiểm tra."])

    def test_failures_are_scoped_and_first_failure_is_per_case(self):
        def downloader(chat, number, directory):
            if number == 1:
                raise ValueError("private name")
            if number == 2:
                raise RuntimeError("private name")
            if number == 3:
                return None
            path = directory / "private.jpg"
            path.write_bytes(JPG)
            return path

        messages = [photo(i) for i in range(1, 5)]
        processor = self.processor(downloader)
        prepared = processor.prepare_images("batch", messages)
        self.addCleanup(prepared.close)
        self.assertEqual(processor.process("a", messages[:1], live=False, prepared=prepared).notes,
                         ["Ảnh đính kèm không tải được: ValueError"])
        self.assertEqual(processor.process("b", messages[1:2], live=False, prepared=prepared).notes,
                         ["Ảnh đính kèm không tải được: RuntimeError"])
        self.assertEqual(processor.process("c", messages[2:3], live=False, prepared=prepared).notes,
                         ["Ảnh bị bỏ qua (1 ảnh): không tải được"])
        self.assertEqual(processor.process("d", messages[3:], live=False, prepared=prepared).notes, [])

    def test_unprepared_matches_same_prepared_batch(self):
        messages = [photo(i) for i in range(1, 7)]
        processor = self.processor(FakeDownloader({i: (JPG, "private.jpg") for i in range(1, 7)}))
        plain = processor.process("case", messages, live=False)
        prepared = processor.prepare_images("case", messages)
        self.addCleanup(prepared.close)
        shared = processor.process("case", messages, live=False, prepared=prepared)
        self.assertEqual(plain.notes, ["Ảnh bị bỏ qua (1 ảnh): vượt giới hạn số ảnh"])
        self.assertEqual(shared.notes, ["Ảnh bị bỏ qua (1 ảnh): vượt giới hạn số ảnh"])

    def test_image_accounting_logs_counts_and_ids_without_names(self):
        messages = [photo(i) for i in range(1, 7)]
        processor = self.processor(FakeDownloader({i: (JPG, "secret-name.jpg") for i in range(1, 7)}))
        with self.assertLogs("tg_triage.attachments", level="INFO") as logs:
            processor.process("case", messages, live=False)
        self.assertEqual(len(logs.output), 1)
        line = logs.output[0]
        self.assertIn("Images found=6 accepted=5 skipped_limit=1 skipped_size=0 skipped_type=0 failed=0", line)
        self.assertIn("accepted_ids=-1001234567890:1,-1001234567890:2", line)
        self.assertIn("skipped_ids=-1001234567890:6", line)
        self.assertNotIn("secret-name", line)

    def test_chat_id_is_part_of_prepared_scope(self):
        first = photo(1, chat_id=10, image_mime="image/gif")
        second = photo(1, chat_id=20)
        processor = self.processor(FakeDownloader({1: (JPG, "private.jpg")}))
        prepared = processor.prepare_images("batch", [first, second])
        self.addCleanup(prepared.close)
        result = processor.process("case", [second], live=False, prepared=prepared)
        self.assertEqual(result.notes, [])
        self.assertEqual(result.dry_run_count, 1)

    def test_logged_id_lists_are_capped_at_twenty(self):
        messages = [photo(i, image_mime="image/gif") for i in range(1, 26)]
        processor = self.processor(None)
        with self.assertLogs("tg_triage.attachments", level="INFO") as logs:
            processor.process("case", messages, live=False)
        self.assertIn("-1001234567890:20", logs.output[0])
        self.assertNotIn("-1001234567890:21", logs.output[0])


if __name__ == "__main__":
    unittest.main()

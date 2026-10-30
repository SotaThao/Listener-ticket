import shutil
import tempfile
import unittest
import json
from dataclasses import replace
from pathlib import Path

from tg_triage.attachments import AttachmentProcessor
from tg_triage.config import AttachmentsConfig
from tg_triage.pipeline import Pipeline
from tg_triage.store import Store
from tg_triage.ticket_templates import TemplateError, validate
from tests.helpers import FakeDownloader, FakeGitHub, FakeUploader, JPG, config, message
from tests.test_autonomous import Router, route


def photo(i, **kwargs):
    return message(i, has_image=True, image_size=len(JPG), **kwargs)


class ImageCaseLimitTests(unittest.TestCase):
    def setUp(self):
        self.down = FakeDownloader({i: (JPG, 'private.jpg') for i in range(1, 30)})
        self.store = Store(':memory:'); self.addCleanup(self.store.close)
        self.uploader = FakeUploader()
        self.processor = AttachmentProcessor(AttachmentsConfig(max_per_case=5), self.store,
                                             downloader=self.down, uploader=self.uploader)

    def prepare(self, messages):
        batch = self.processor.prepare_images('batch', messages)
        self.addCleanup(batch.close)
        return batch

    def test_t1_batch_history_does_not_consume_case_limit(self):
        items = [photo(i) for i in range(1, 9)]
        batch = self.prepare(items)
        result = self.processor.process('a', items[5:], live=True, prepared=batch)
        self.assertEqual(len(result.images), 3)
        self.assertEqual(len(self.uploader.uploads), 3)
        self.assertEqual(len(self.down.calls), 8)

    def test_t1_unseen_note_only_counts_attached(self):
        items = [photo(i) for i in range(1, 9)]
        result = self.processor.process('a', items[5:], live=True, prepared=self.prepare(items))
        self.assertEqual(result.notes, ['Model chưa xem 3 ảnh đính kèm trong lượt phân tích; người xử lý cần mở các ảnh này để kiểm tra.'])

    def test_t2_case_limit(self):
        items = [photo(i) for i in range(1, 8)]
        result = self.processor.process('a', items, live=True, prepared=self.prepare(items))
        self.assertEqual(len(result.images), 5)
        self.assertEqual(len(self.uploader.uploads), 5)
        self.assertEqual(result.notes, ['Ảnh bị bỏ qua (2 ảnh): vượt giới hạn số ảnh'])

    def test_t2_skipped_images_not_counted_as_unseen(self):
        items = [photo(i) for i in range(1, 8)]
        result = self.processor.process('a', items, live=True, prepared=self.prepare(items))
        self.assertEqual(result.notes, ['Ảnh bị bỏ qua (2 ảnh): vượt giới hạn số ảnh'])

    def test_t3_absent_and_prepared_reuse(self):
        items = [photo(1), photo(2)]
        batch = self.prepare(items[:1])
        result = self.processor.process('a', items, live=True, prepared=batch)
        self.assertEqual(len(result.images), 2)
        self.assertEqual(len(self.uploader.uploads), 2)
        self.assertEqual(len(self.down.calls), 2)

    def test_t4_real_failure_not_retried(self):
        for error, note in ((None, 'Ảnh bị bỏ qua (1 ảnh): không tải được'),
                            (ValueError('private'), 'Ảnh đính kèm không tải được: ValueError')):
            with self.subTest(error=error):
                down = FakeDownloader(error=error)
                processor = AttachmentProcessor(AttachmentsConfig(max_per_case=5), self.store, downloader=down)
                batch = processor.prepare_images('batch', [photo(1)])
                self.addCleanup(batch.close)
                self.assertEqual(len(down.calls), 1)
                result = processor.process('a', [photo(1)], live=False, prepared=batch)
                self.assertEqual(len(down.calls), 1)
                self.assertEqual(result.notes, [note])

    def test_unseen_download_failure_has_no_unseen_note(self):
        items = [photo(i) for i in range(1, 7)]
        self.down.files.pop(6)
        result = self.processor.process('a', items[4:], live=False, prepared=self.prepare(items))
        self.assertEqual(result.notes, ['Ảnh bị bỏ qua (1 ảnh): không tải được'])

    def test_t5_logs_case_and_batch(self):
        items = [photo(i) for i in range(1, 4)]
        with self.assertLogs('tg_triage.attachments', 'INFO') as logs:
            batch = self.prepare(items)
            self.processor.process('a', items[:1], live=False, prepared=batch)
        self.assertIn('Vision batch found=3', logs.output[0])
        self.assertIn('Images found=1', logs.output[1])
        self.assertNotIn('private', str(logs.output))

    def test_t6_autonomous_two_groups(self):
        router = Router([route([1, 2, 3]), route([4, 5, 6, 7])])
        router.supports_vision = True
        store = Store(':memory:'); self.addCleanup(store.close)
        uploader = FakeUploader()
        p = Pipeline(replace(config('live', window=2), autonomous_enabled=True), store, router,
                     FakeGitHub(), downloader=self.down, uploader=uploader)
        items = [photo(i, text='Lỗi cần xử lý', mentions_listener=False) for i in range(1, 8)]
        for item in items: store.enqueue(item)
        p.process(items[0])
        self.assertEqual(len(p.github.calls), 2)
        self.assertEqual([c.body.count('![Ảnh ') for c in p.github.calls], [3, 4])
        self.assertEqual(len(uploader.uploads), 7)
        self.assertNotIn('Model chưa xem', p.github.calls[0].body)
        self.assertIn('Model chưa xem 2 ảnh', p.github.calls[1].body)

    def test_t7_guard_all_five_conditions(self):
        store = Store(':memory:'); self.addCleanup(store.close)
        router = Router(); router.supports_vision = True
        p = Pipeline(config('live'), store, router, FakeGitHub(), downloader=self.down)
        items = [photo(i, text='Lỗi') for i in range(1, 8)]
        batch = self.prepare(items); p._prepared_images = batch
        self.assertTrue(p._unseen_tolerated(items[3:7]))
        self.assertFalse(p._unseen_tolerated(items[5:7]))
        self.assertFalse(p._unseen_tolerated(items))
        self.assertFalse(p._unseen_tolerated([replace(m, text='/bug') for m in items[3:7]]))
        self.assertFalse(p._unseen_tolerated(items[3:6] + [replace(items[6], image_mime='image/gif')]))
        router.rejects_images = True
        self.assertFalse(p._unseen_tolerated(items[3:7]))
        router.rejects_images = False
        batch.result.image_skips[-1] = (*batch.result.image_skips[-1][:2], 'không tải được')
        self.assertFalse(p._unseen_tolerated(items[3:7]))

    def test_d1_unseen_caption_or_group_reply_required(self):
        router = Router(); router.supports_vision = True
        p = Pipeline(config('live'), self.store, router, FakeGitHub(), downloader=self.down)
        items = [photo(i, text='Lỗi') for i in range(1, 7)]
        p._prepared_images = self.prepare(items)
        seen, unseen = items[4:6]
        self.assertFalse(p._unseen_tolerated([seen, replace(unseen, text='', reply_to_id=None)]))
        self.assertTrue(p._unseen_tolerated([seen, unseen]))
        self.assertTrue(p._unseen_tolerated([seen, replace(unseen, text='', reply_to_id=seen.message_id)]))
        self.assertFalse(p._unseen_tolerated([seen, replace(unseen, text='', reply_to_id=99)]))

    def test_t7_single_unseen_image_clarifies(self):
        router = Router([route([1, 2, 3, 4, 5], 'CLARIFY'), route([6])]); router.supports_vision = True
        store = Store(':memory:'); self.addCleanup(store.close)
        p = Pipeline(replace(config('live', window=2), autonomous_enabled=True), store, router,
                     FakeGitHub(), downloader=self.down, uploader=FakeUploader())
        items = [photo(i, text='cái này lỗi', mentions_listener=False) for i in range(1, 7)]
        for item in items: store.enqueue(item)
        p.process(items[0])
        row = store.connection.execute('SELECT action, reason_code FROM autonomous_routes WHERE message_id=6').fetchone()
        self.assertEqual(tuple(row), ('CLARIFY', 'ingress_pixels_unavailable'))
        self.assertEqual(p.github.calls, [])

    def test_d1_captionless_unseen_without_reply_clarifies(self):
        router = Router([route([1, 2, 3, 4], 'CLARIFY'), route([5, 6])]); router.supports_vision = True
        store = Store(':memory:'); self.addCleanup(store.close)
        p = Pipeline(replace(config('live', window=2), autonomous_enabled=True), store, router,
                     FakeGitHub(), downloader=self.down, uploader=FakeUploader())
        items = [photo(i, text='Lỗi cần xử lý', mentions_listener=False) for i in range(1, 6)]
        items.append(photo(6, text='', mentions_listener=False))
        for item in items: store.enqueue(item)
        p.process(items[0])
        row = store.connection.execute('SELECT action, reason_code FROM autonomous_routes WHERE message_id=6').fetchone()
        self.assertEqual(tuple(row), ('CLARIFY', 'ingress_pixels_unavailable'))
        self.assertEqual(p.github.calls, [])

    def test_t8_followup_incoming_outside_batch(self):
        batch = self.prepare([photo(i) for i in range(1, 6)])
        result = self.processor.process('followup', [photo(6)], live=False, prepared=batch)
        self.assertEqual(result.dry_run_count, 1)
        self.assertEqual(len(self.down.calls), 6)

    def test_t8_followup_over_vision_budget_clarifies_without_upload(self):
        from tg_triage.followups import sweep
        from tests.test_lifecycle import LifecycleTests
        helper = LifecycleTests('test_closed_historical_evidence_comments_without_refresh_or_reopen')
        store, model, gh, sent, p = helper.setup_lifecycle()
        self.addCleanup(store.close)
        model.supports_vision = True
        p.attachments.downloader = self.down
        p.attachments.uploader = FakeUploader()
        gh.lifecycle_snapshot = lambda n: dict(number=n, repo=p.config.gh_repo, state='open', observed_at='2026-10-30T00:00:00+00:00')
        incoming = [photo(i, text='Ảnh lỗi', reply_to_id=901) for i in range(10, 16)]
        for item in incoming: store.start_classifying(item)
        store.enqueue_followup_batch(incoming, store.followup_source(incoming[0]))
        sweep(p)
        plan = json.loads(store.connection.execute('SELECT plan FROM followup_jobs').fetchone()[0])
        self.assertEqual((plan['action'], plan['context_reason']), ('clarify', 'vision_unavailable'))
        self.assertEqual(p.attachments.uploader.uploads, [])
        self.assertEqual(len(gh.calls), 1)

    def test_t9_template_validation(self):
        source = Path(__file__).resolve().parents[1] / 'templates' / 'github-ticket'
        templates = validate(source)
        self.assertIn('chưa xem 2 ảnh', templates.render('_common.md', 'note.unseen_images', {'count': '2'}))
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / 'templates'; shutil.copytree(source, dest)
            path = dest / '_common.md'; text = path.read_text(encoding='utf-8')
            text = text[:text.index('{{#block note.unseen_images}}')]
            path.write_text(text, encoding='utf-8')
            with self.assertRaises(TemplateError): validate(dest)

"""Document attachments (Assistant definition 12.14). Offline: fake downloader, uploader and events."""

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from tg_triage import telegram_io
from tg_triage.attachments import (DENIED_EXTENSIONS, DOCUMENT_EXTENSIONS, DOCUMENT_MIME,
                                   document_extension)
from tg_triage.config import AttachmentsConfig, ConfigError, load_config
from tg_triage.issue_format import document_label
from tg_triage.pipeline import Pipeline
from tg_triage.store import Store, _to_message
from tests.helpers import (ASSET_BASE, EXAMPLE, FAKE_HASH, FAKE_TOKEN, JPG, NOW, Clock, FakeCaseModel,
                           FakeDownloader, FakeGitHub, FakeUploader, config, message, ticket)

CHAT = -1001234567890
TAG = "attachments--1001234567890_1"
OFFICE = "application/vnd.openxmlformats-officedocument."
GOOD = (  # (file name, MIME, expected extension)
    ("bao_cao.pdf", "application/pdf", "pdf"),
    ("yeu cau.docx", OFFICE + "wordprocessingml.document", "docx"),
    ("so lieu.xlsx", OFFICE + "spreadsheetml.sheet", "xlsx"),
    ("slide.pptx", OFFICE + "presentationml.presentation", "pptx"),
    ("ghi chu.txt", "text/plain", "txt"),
    ("data.csv", "text/csv", "csv"),
    ("README.md", "text/markdown", "md"),
    ("config.json", "application/json", "json"),
    ("trang.html", "text/html", "html"),
    ("log.zip", "application/zip", "zip"),
)
PDF_BYTES = b"%PDF-1.4 fake"


def doc(message_id, name="bao_cao.pdf", mime="application/pdf", size=2048, text=None, **extra):
    fields = dict(sender_id=1, has_document=True, document_size=size, document_mime=mime,
                  document_name=name, received_at=NOW)
    fields.update(extra)
    return message(message_id, text or f"file lỗi đăng nhập app nexora {message_id}", **fields)


def image(message_id, **extra):
    fields = dict(sender_id=1, has_image=True, image_size=2048, received_at=NOW)
    fields.update(extra)
    return message(message_id, f"ảnh lỗi đăng nhập app nexora {message_id}", **fields)


class DocumentBase(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.store = Store(":memory:", clock=self.clock)
        self.addCleanup(self.store.close)
        self.github = FakeGitHub()
        self.uploader = FakeUploader()

    def build(self, mode="live", files=None, *, limits=None, ids=(1,), window=0, downloader=None):
        self.downloader = downloader or FakeDownloader(files if files is not None else {})
        self.model = FakeCaseModel(ticket(ids))
        return Pipeline(config(mode, window=window, attachments=limits), self.store, self.model,
                        self.github, uploader=self.uploader, downloader=self.downloader)

    def run_case(self, pipeline, messages):
        for item in messages:
            pipeline.process(item)
        self.clock.advance(60)
        return pipeline.close_due_cases()[0]

    @property
    def body(self):
        return self.github.calls[0].body


class AcceptedDocumentTests(DocumentBase):
    def test_every_allowed_type_is_downloaded_uploaded_and_listed_as_a_link(self):
        for index, (name, mime, extension) in enumerate(GOOD, 1):
            with self.subTest(extension=extension):
                self.setUp()
                pipeline = self.build(files={index: (PDF_BYTES, name)}, ids=(index,))
                result = pipeline.process(doc(index, name, mime))
                self.assertEqual(result.decision, "issue")
                asset = f"{index}-1.{extension}"
                self.assertEqual([(u["tag"], u["asset"]) for u in self.uploader.uploads],
                                 [(f"attachments--1001234567890_{index}", asset)])
                url = f"{ASSET_BASE}/attachments--1001234567890_{index}/{asset}"
                self.assertNotIn("Tài liệu đính kèm", self.body)   # no sub-label any more (12.16)
                self.assertRegex(self.body, rf"- 📎 \[[^\]]+\]\({url}\)")
                self.assertEqual(result.documents, 1)

    def test_telegram_web_markdown_mime_downloads_uploads_and_links(self):
        name = "production-note.md"
        pipeline = self.build(files={1: (b"# Markdown note\n" * 900, name)})
        result = pipeline.process(doc(1, name, "text/x-web-markdown", size=15938))
        self.assertEqual(result.decision, "issue")
        self.assertEqual(result.documents, 1)
        self.assertEqual(self.downloader.calls, [(CHAT, 1)])
        self.assertEqual([upload["asset"] for upload in self.uploader.uploads], ["1-1.md"])
        self.assertIn(f"- 📎 [production-note.md]({ASSET_BASE}/{TAG}/1-1.md)", self.body)
        self.assertNotIn("Tài liệu bị bỏ qua", self.body)

    def test_documents_are_links_never_embedded(self):
        pipeline = self.build(files={1: (PDF_BYTES, "a.pdf")})
        pipeline.process(doc(1, "bao_cao.pdf"))
        self.assertNotIn("![", self.body)
        self.assertNotIn("Ảnh đính kèm", self.body)
        self.assertIn(f"- 📎 [bao cao.pdf]({ASSET_BASE}/{TAG}/1-1.pdf)", self.body)

    def test_release_asset_name_is_message_id_ordinal_and_allowlisted_extension(self):
        pipeline = self.build(files={1: (PDF_BYTES, "x"), 2: (PDF_BYTES, "y")}, ids=(1, 2), window=15)
        self.run_case(pipeline, [doc(1, "Nguyen Van A secret.pdf"),
                                 doc(2, "Hop dong.DOCX", OFFICE + "wordprocessingml.document")])
        assets = [u["asset"] for u in self.uploader.uploads]
        self.assertEqual(assets, ["1-1.pdf", "2-2.docx"])
        for upload in self.uploader.uploads:
            self.assertNotIn("Nguyen", upload["asset"])
            self.assertTrue(upload["existed"] and upload["name_matches"])
            self.assertFalse(upload["path"].exists())   # temporary files are deleted afterwards
        self.assertEqual(self.uploader.releases, [TAG])

    def test_name_without_extension_takes_it_from_the_mime(self):
        pipeline = self.build(files={1: (PDF_BYTES, "README")})
        pipeline.process(doc(1, "README", "text/markdown"))
        self.assertEqual(self.uploader.uploads[0]["asset"], "1-1.md")

    def test_documents_and_images_share_one_release_and_both_sections_appear(self):
        pipeline = self.build(files={1: (JPG, "p.jpg"), 2: (PDF_BYTES, "d.pdf")}, ids=(1, 2), window=15)
        self.run_case(pipeline, [image(1), doc(2)])
        self.assertEqual(self.uploader.releases, [TAG])
        self.assertEqual([u["asset"] for u in self.uploader.uploads], ["1-1.jpg", "2-1.pdf"])
        self.assertIn("![Ảnh 1](", self.body)
        self.assertIn("- 📎 [", self.body)
        self.assertNotIn("Tài liệu đính kèm", self.body)
        self.assertEqual(len([line for line in self.body.splitlines() if line.startswith("![")]), 1)

    def test_document_alone_makes_the_message_content_and_waits_like_a_photo(self):
        pipeline = self.build(files={1: (PDF_BYTES, "d.pdf")}, window=15)
        result = pipeline.process(doc(1, text="ok"))
        self.assertEqual(result.decision, "case_open")


class RejectedDocumentTests(DocumentBase):
    def refused(self, name, mime, size=2048):
        self.setUp()
        pipeline = self.build(files={1: (PDF_BYTES, "downloaded.bin")})
        result = pipeline.process(doc(1, name, mime, size))
        self.assertEqual(self.downloader.calls, [], f"{name} {mime} was downloaded")
        self.assertEqual((self.uploader.releases, self.uploader.uploads), ([], []))
        self.assertEqual(result.decision, "issue")   # the issue itself is never lost
        self.assertIn("Tài liệu bị bỏ qua (1 tài liệu): định dạng không được nhận", self.body)
        self.assertNotIn("📎", self.body)
        return result

    def test_executable_and_script_extensions_are_refused_without_downloading(self):
        for extension in sorted(DENIED_EXTENSIONS):
            with self.subTest(extension=extension):
                self.refused(f"a.{extension}", "application/octet-stream")
                self.refused(f"a.{extension}", None)

    def test_named_examples_exe_bat_sh_apk_js_are_refused(self):
        for name, mime in (("setup.exe", "application/x-msdownload"), ("run.bat", "application/x-bat"),
                           ("install.sh", "application/x-sh"), ("app.apk", "application/vnd.android.package-archive"),
                           ("code.js", "text/javascript")):
            with self.subTest(name=name):
                self.refused(name, mime)

    def test_disguised_files_are_refused_when_name_and_mime_disagree(self):
        for name, mime in (("a.pdf", "application/x-msdownload"),   # exe under a pdf name
                           ("a.md", "application/x-msdownload"),
                           ("a.exe.md", "text/x-web-markdown"),
                           ("a.js", "text/x-web-markdown"),
                           ("a.pdf", "text/x-web-markdown"),
                           ("a.pdf", "text/html"),                  # html under a pdf name
                           ("a.pdf", "application/zip"),
                           ("a.html", "application/pdf"),
                           ("a.txt", "application/x-sh"),
                           ("a.pdf.exe", "application/pdf"), ("a.exe.pdf", "application/pdf"),
                           ("a.sh.zip", "application/zip"), ("a.pdf ", "application/x-dosexec"),
                           ("a.exe.", "application/pdf")):
            with self.subTest(name=name, mime=mime):
                self.refused(name, mime)

    def test_extensions_outside_the_allowlist_and_unknown_files_are_refused(self):
        for name, mime in (("a.tar", "application/x-tar"), ("a.rar", None), ("a.doc", "application/msword"),
                           ("a.svg", "image/svg+xml"), ("noname", None), (None, None),
                           ("a.pdf", "application/octet-stream"), (None, "application/octet-stream"),
                           ("", "")):
            with self.subTest(name=name, mime=mime):
                self.refused(name, mime)

    def test_the_allowlist_is_exactly_the_documented_set_and_has_no_denied_extension(self):
        self.assertEqual(DOCUMENT_EXTENSIONS, {"pdf", "docx", "xlsx", "pptx", "txt", "csv", "md",
                                               "json", "html", "zip"})
        self.assertFalse(DOCUMENT_EXTENSIONS & DENIED_EXTENSIONS)
        self.assertEqual(set(DOCUMENT_MIME), DOCUMENT_EXTENSIONS)

    def test_document_extension_unit_rules(self):
        self.assertEqual(document_extension("A.PDF", "Application/PDF; charset=x"), "pdf")
        self.assertEqual(document_extension("notes", "text/plain"), "txt")
        self.assertEqual(document_extension("a.md", "text/plain"), "md")
        self.assertEqual(document_extension("dir\\sub/a.csv", None), "csv")
        self.assertIsNone(document_extension("a.pdf", "application/x-msdownload"))
        self.assertIsNone(document_extension("a.exe", "application/pdf"))
        self.assertIsNone(document_extension(None, None))

    def test_oversized_declared_file_is_skipped_without_downloading(self):
        pipeline = self.build(files={1: (PDF_BYTES, "a.pdf")})
        result = pipeline.process(doc(1, size=10 * 1024 * 1024 + 1))
        self.assertEqual((self.downloader.calls, self.uploader.uploads), ([], []))
        self.assertEqual(result.decision, "issue")
        self.assertIn("Tài liệu bị bỏ qua (1 tài liệu): quá dung lượng cho phép", self.body)

    def test_file_larger_than_it_declared_is_dropped_after_download(self):
        limits = AttachmentsConfig(max_bytes=100)
        pipeline = self.build(files={1: (b"x" * 500, "a.pdf")}, limits=limits)
        pipeline.process(doc(1, size=None))
        self.assertEqual(self.downloader.calls, [(CHAT, 1)])
        self.assertEqual(self.uploader.uploads, [])
        self.assertIn("quá dung lượng cho phép", self.body)

    def test_size_limit_comes_from_the_attachments_config(self):
        pipeline = self.build(files={1: (PDF_BYTES, "a.pdf")}, limits=AttachmentsConfig(max_bytes=1000))
        pipeline.process(doc(1, size=1001))
        self.assertEqual(self.uploader.uploads, [])
        self.assertIn("quá dung lượng cho phép", self.body)

    def test_documents_over_the_per_case_limit_are_dropped_with_a_note(self):
        limits = AttachmentsConfig(max_documents_per_case=2)
        ids = (1, 2, 3)
        pipeline = self.build(files={i: (PDF_BYTES, "a.pdf") for i in ids}, ids=ids, window=15, limits=limits)
        self.run_case(pipeline, [doc(i) for i in ids])
        self.assertEqual([u["asset"] for u in self.uploader.uploads], ["1-1.pdf", "2-2.pdf"])
        self.assertEqual(self.downloader.calls, [(CHAT, 1), (CHAT, 2)])
        self.assertIn("Tài liệu bị bỏ qua (1 tài liệu): vượt giới hạn số tài liệu", self.body)

    def test_the_document_limit_is_separate_from_the_image_limit(self):
        limits = AttachmentsConfig(max_per_case=1, max_documents_per_case=1)
        pipeline = self.build(files={1: (JPG, "p.jpg"), 2: (PDF_BYTES, "d.pdf")}, ids=(1, 2), window=15,
                              limits=limits)
        self.run_case(pipeline, [image(1), doc(2)])
        self.assertEqual([u["asset"] for u in self.uploader.uploads], ["1-1.jpg", "2-1.pdf"])

    def test_zero_documents_per_case_turns_documents_off_silently(self):
        limits = AttachmentsConfig(max_documents_per_case=0)
        pipeline = self.build(files={1: (PDF_BYTES, "a.pdf")}, limits=limits)
        result = pipeline.process(doc(1))
        self.assertEqual((self.downloader.calls, self.uploader.uploads), ([], []))
        self.assertEqual(result.decision, "issue")
        self.assertNotIn("Tài liệu", self.body)

    def test_hourly_quota_counts_documents_like_images(self):
        limits = AttachmentsConfig(max_per_hour=1)
        pipeline = self.build(files={1: (PDF_BYTES, "a.pdf"), 2: (PDF_BYTES, "b.pdf")}, ids=(1, 2),
                              window=15, limits=limits)
        self.run_case(pipeline, [doc(1), doc(2)])
        self.assertEqual(len(self.uploader.uploads), 1)
        self.assertEqual(self.store.count_recent_attachments(), 1)
        self.assertIn("Tài liệu bị bỏ qua (1 tài liệu): đạt giới hạn tệp đính kèm mỗi giờ", self.body)


class DocumentNameTests(DocumentBase):
    def test_hostile_original_name_never_appears_raw_and_email_is_redacted(self):
        hostile = "bob@example.com [click](evil.example) ![img](evil.example) *b*.pdf"
        pipeline = self.build(files={1: (PDF_BYTES, "a.pdf")})
        pipeline.process(doc(1, hostile))
        link = next(line for line in self.body.splitlines() if line.startswith("- 📎 ["))
        for raw in ("bob@example.com", "[click](", "![", "](evil", "*b*"):
            self.assertNotIn(raw, link)
        self.assertIn("\\[email đã ẩn\\]", link)
        self.assertTrue(link.endswith(f"]({ASSET_BASE}/{TAG}/1-1.pdf)"))
        self.assertEqual(link.count("]("), 1)   # exactly one Markdown link: the code-built one
        self.assertNotIn("bob@example.com", self.body.split("📎")[1].split("---")[0])

    def test_a_name_made_of_urls_with_slashes_is_cut_to_its_last_segment_and_stays_inert(self):
        label = document_label("[x](http://evil.example/a) ![y](http://evil.example/p.png) bob@example.com.pdf")
        for raw in ("http", "evil", "![", "](", "bob@example.com"):
            self.assertNotIn(raw, label)

    def test_phone_number_in_the_name_is_redacted(self):
        self.assertIn("số đã ẩn", document_label("lien he 0901234567.pdf"))
        self.assertNotIn("0901234567", document_label("lien he 0901234567.pdf"))

    def test_label_drops_markdown_and_html_and_keeps_a_readable_name(self):
        self.assertEqual(document_label("bao_cao [v2] (final)!.pdf"), "bao cao v2 final.pdf")
        self.assertNotIn("<", document_label("<script>alert(1)</script>.html"))
        self.assertEqual(document_label("C:\\Users\\me\\a.pdf"), "a.pdf")
        self.assertEqual(document_label(None, "tài liệu 3"), "tài liệu 3")
        self.assertEqual(document_label("   ", "tài liệu 3"), "tài liệu 3")

    def test_label_is_capped_and_never_cuts_a_placeholder_in_half(self):
        label = document_label("a" * 200 + ".pdf")
        self.assertLessEqual(len(label), 80)
        label = document_label("x" * 75 + " me@example.com.pdf")
        self.assertNotIn("example.com", label)
        self.assertNotIn("\\", label.replace("\\[email", "").replace("\\]", ""))

    def test_name_goes_in_the_link_text_only_not_in_the_release_url(self):
        pipeline = self.build(files={1: (PDF_BYTES, "a.pdf")})
        pipeline.process(doc(1, "Bao cao Q3 secret.pdf"))
        link = next(line for line in self.body.splitlines() if line.startswith("- 📎 ["))
        self.assertIn("Bao cao Q3 secret.pdf", link.split("](")[0])
        self.assertNotIn("secret", link.split("](")[1])


class DocumentFailureTests(DocumentBase):
    def test_upload_failure_keeps_the_issue_notes_the_class_and_does_not_retry(self):
        self.uploader = FakeUploader(error=RuntimeError(f"{FAKE_TOKEN} https://uploads.example/secret"))
        pipeline = self.build(files={1: (PDF_BYTES, "a.pdf"), 2: (PDF_BYTES, "b.pdf")}, ids=(1, 2),
                              window=15)
        with self.assertLogs("tg_triage", level="INFO") as logs:
            result = self.run_case(pipeline, [doc(1, "ten rieng.pdf"), doc(2)])
        self.assertEqual(result.decision, "issue")
        self.assertEqual(len(self.github.calls), 1)
        self.assertEqual(len(self.uploader.uploads), 1)
        self.assertIn("Tài liệu không tải được: RuntimeError", self.body)
        self.assertNotIn("📎", self.body)
        output = "\n".join(logs.output)
        for secret in (FAKE_TOKEN, "uploads.example", "secret", "ten rieng"):
            self.assertNotIn(secret, output)
            self.assertNotIn(secret, self.body)
        self.assertFalse(self.uploader.uploads[0]["path"].exists())

    def test_download_failure_keeps_the_issue(self):
        pipeline = self.build(downloader=FakeDownloader(error=TimeoutError("fake offline")))
        result = pipeline.process(doc(1))
        self.assertEqual(result.decision, "issue")
        self.assertIn("Tài liệu không tải được: TimeoutError", self.body)
        self.assertEqual(self.uploader.uploads, [])

    def test_release_creation_failure_keeps_the_issue(self):
        self.uploader = FakeUploader(release_error=RuntimeError("no release"))
        pipeline = self.build(files={1: (PDF_BYTES, "a.pdf")})
        result = pipeline.process(doc(1))
        self.assertEqual(result.decision, "issue")
        self.assertIn("Tài liệu không tải được: RuntimeError", self.body)

    def test_downloader_returning_a_path_outside_the_work_directory_is_refused(self):
        with tempfile.TemporaryDirectory() as outside:
            stray = Path(outside) / "evil.pdf"
            stray.write_bytes(PDF_BYTES)
            pipeline = self.build(downloader=lambda chat_id, message_id, directory: stray)
            self.downloader = None
            result = pipeline.process(doc(1))
            self.assertEqual(result.decision, "issue")
            self.assertEqual(self.uploader.uploads, [])
            self.assertIn("Tài liệu không tải được: AttachmentError", self.body)
            self.assertTrue(stray.exists())   # a file outside is never moved or deleted

    def test_missing_downloader_or_uploader_never_blocks_the_issue(self):
        for uploader, downloader_ready in ((None, True), (FakeUploader(), False)):
            self.setUp()
            pipeline = Pipeline(config("live"), self.store, FakeCaseModel(ticket([1])), self.github,
                                uploader=uploader,
                                downloader=FakeDownloader({1: (PDF_BYTES, "a.pdf")}) if downloader_ready else None)
            self.assertEqual(pipeline.process(doc(1)).decision, "issue")
            self.assertIn("Tài liệu không tải được:", self.body)

    def test_nothing_is_ever_opened_or_unpacked(self):
        # A corrupt "zip" and a "pdf" full of garbage still travel: only name, MIME and size are used.
        pipeline = self.build(files={1: (b"not a zip at all", "a.zip")})
        pipeline.process(doc(1, "a.zip", "application/zip"))
        self.assertEqual([u["asset"] for u in self.uploader.uploads], ["1-1.zip"])


class DocumentDryRunTests(DocumentBase):
    def test_dry_run_downloads_and_validates_but_never_calls_gh(self):
        self.uploader = FakeUploader(error=AssertionError("gh must not be called"),
                                     release_error=AssertionError("release must not be created"))
        self.github = FakeGitHub(error=AssertionError("GitHub must not be called"))
        pipeline = self.build("dry-run", files={1: (PDF_BYTES, "private name.pdf"), 2: (PDF_BYTES, "b.pdf")},
                              ids=(1, 2, 3), window=15)
        pipeline.process(doc(1, "private name.pdf"))
        pipeline.process(doc(2, "b.exe", "application/x-msdownload"))
        self.clock.advance(60)
        with self.assertLogs("tg_triage", level="INFO") as logs:
            result = pipeline.close_due_cases()[0]
        self.assertEqual(result.decision, "dry_run_issue")
        self.assertEqual(self.downloader.calls, [(CHAT, 1)])
        self.assertEqual((self.uploader.releases, self.uploader.uploads, self.github.calls), ([], [], []))
        self.assertEqual(self.store.count_recent_attachments(), 0)
        output = "\n".join(logs.output)
        self.assertIn("documents_dry_run=1", output)
        self.assertIn("documents=0", output)
        self.assertIn("rejected_type=1", output)
        self.assertNotIn("private name", output)
        for directory in self.downloader.directories:
            self.assertFalse(directory.exists())

    def test_live_log_carries_counts_only(self):
        pipeline = self.build(files={1: (PDF_BYTES, "x"), 2: (PDF_BYTES, "y")}, ids=(1, 2, 3), window=15)
        with self.assertLogs("tg_triage", level="INFO") as logs:
            self.run_case(pipeline, [doc(1, "ten bi mat.pdf"), doc(2, "evil.exe", None)])
        output = "\n".join(logs.output)
        self.assertIn("documents=1", output)
        self.assertIn("rejected_type=1", output)
        for private in ("ten bi mat", "evil", "bi mat"):
            self.assertNotIn(private, output)


class DocumentPersistenceTests(unittest.TestCase):
    def test_document_metadata_survives_the_store(self):
        store = Store(":memory:")
        self.addCleanup(store.close)
        original = doc(7, "báo cáo.pdf", "application/pdf", 1234)
        self.assertTrue(store.enqueue(original))
        stored = _to_message(store.get(original.chat_id, original.message_id))
        self.assertEqual((stored.has_document, stored.document_size, stored.document_mime, stored.document_name),
                         (True, 1234, "application/pdf", "báo cáo.pdf"))
        self.assertFalse(stored.has_image)
        self.assertTrue(stored.has_attachment)

    def test_message_with_a_lone_surrogate_in_the_file_name_is_made_storable(self):
        item = doc(1, "a\ud800.pdf")
        self.assertNotIn("\ud800", item.document_name)


class DocumentConfigTests(unittest.TestCase):
    ENV = {"TG_API_ID": "123", "TG_API_HASH": FAKE_HASH}

    def load(self, extra=""):
        source = EXAMPLE.read_text(encoding="utf-8")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text(source + extra, encoding="utf-8")
            return load_config(path, self.ENV)

    def test_example_and_defaults_are_five_documents_per_case(self):
        self.assertEqual(load_config(EXAMPLE, self.ENV).attachments.max_documents_per_case, 5)
        self.assertEqual(AttachmentsConfig().max_documents_per_case, 5)

    def test_value_is_read_and_zero_means_off_but_negative_or_wrong_type_is_an_error(self):
        source = EXAMPLE.read_text(encoding="utf-8").replace(
            "max_documents_per_case = 5", "max_documents_per_case = 3")
        self.assertIn("max_documents_per_case = 3", source)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text(source, encoding="utf-8")
            self.assertEqual(load_config(path, self.ENV).attachments.max_documents_per_case, 3)
            for value, ok in (("0", True), ("-1", False), ('"5"', False), ("1.5", False)):
                path.write_text(source.replace("max_documents_per_case = 3",
                                               f"max_documents_per_case = {value}"), encoding="utf-8")
                with self.subTest(value=value):
                    if ok:
                        self.assertEqual(load_config(path, self.ENV).attachments.max_documents_per_case, 0)
                    else:
                        with self.assertRaisesRegex(ConfigError, "attachments.max_documents_per_case"):
                            load_config(path, self.ENV)


def event(**extra):
    fields = dict(out=False, is_private=False, chat_id=CHAT, id=1, date=NOW, raw_text="gửi file nexora",
                  get_sender=AsyncMock(return_value=SimpleNamespace(first_name="Tên", last_name="Gửi")),
                  get_chat=AsyncMock(return_value=SimpleNamespace(username="Team_Group")))
    fields.update(extra)
    return SimpleNamespace(**fields)


def file(name, mime, size=4096):
    return SimpleNamespace(name=name, mime_type=mime, size=size)


class TelegramDocumentTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.addCleanup(self.store.close)

    async def stored(self, **extra):
        incoming = event(**extra)
        await telegram_io.enqueue_event(incoming, asyncio.Queue(), (CHAT,), self.store)
        return self.store.get(CHAT, incoming.id)

    async def test_a_document_event_stores_declared_name_mime_and_size_only(self):
        row = await self.stored(document=object(), file=file("báo cáo.pdf", "application/pdf", 999))
        self.assertEqual((row["has_document"], row["document_size"], row["document_mime"], row["document_name"]),
                         (1, 999, "application/pdf", "báo cáo.pdf"))
        self.assertEqual(row["has_image"], 0)

    async def test_executable_documents_are_still_recorded_so_the_pipeline_can_refuse_and_note_them(self):
        row = await self.stored(document=object(), file=file("setup.exe", "application/x-msdownload"))
        self.assertEqual((row["has_document"], row["document_name"]), (1, "setup.exe"))

    async def test_image_files_photos_stickers_and_media_are_not_documents(self):
        cases = {"image file": dict(document=object(), file=file("a.png", "image/png")),
                 "photo": dict(photo=object(), document=object(), file=file("a.jpg", "image/jpeg")),
                 "sticker": dict(document=object(), sticker=object(), file=file("s.webp", "image/webp")),
                 "video": dict(document=object(), video=object(), file=file("v.mp4", "video/mp4")),
                 "voice": dict(document=object(), voice=object(), file=file("v.ogg", "audio/ogg")),
                 "gif": dict(document=object(), gif=object(), file=file("g.mp4", "video/mp4")),
                 "no media": dict()}
        for number, (label, extra) in enumerate(cases.items(), 10):
            with self.subTest(label):
                row = await self.stored(id=number, **extra)
                self.assertEqual(row["has_document"], 0)

    async def test_image_file_still_takes_the_image_path(self):
        row = await self.stored(document=object(), file=file("a.png", "image/png"))
        self.assertEqual((row["has_image"], row["has_document"]), (1, 0))

    async def test_unusable_file_metadata_is_stored_as_unknown(self):
        row = await self.stored(document=object(), file=SimpleNamespace(name=5, mime_type=None, size="big"))
        self.assertEqual((row["has_document"], row["document_name"], row["document_mime"], row["document_size"]),
                         (1, None, None, None))


if __name__ == "__main__":
    unittest.main()

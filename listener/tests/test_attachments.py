"""Image attachments (Assistant definition 12.8) and case config. Offline: no gh, no Telethon."""

import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tg_triage import attachments
from tg_triage.attachments import GhCliUploader, UploadError, release_tag, sniff_extension
from tg_triage.config import AttachmentsConfig, ConfigError, load_config
from tg_triage.models import CaseAnalysis
from tg_triage.pipeline import Pipeline
from tg_triage.store import Store
from tests.helpers import (ASSET_BASE, EXAMPLE, FAKE_HASH, FAKE_TOKEN, JPG, NOW, PNG, WEBP, Clock,
                           FakeCaseModel, FakeDownloader, FakeGitHub, FakeUploader, config, message,
                           classification, ticket)

CHAT = -1001234567890
TAG = "attachments--1001234567890_1"


def photo(message_id, minutes=0, text="", **extra):
    fields = dict(sender_id=1, has_image=True, image_size=2048, received_at=NOW)
    fields.update(extra)
    return message(message_id, text or f"ảnh lỗi đăng nhập app nexora {message_id}", **fields)


class AttachmentBase(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.store = Store(":memory:", clock=self.clock)
        self.addCleanup(self.store.close)
        self.github = FakeGitHub()
        self.uploader = FakeUploader()

    def build(self, mode="live", files=None, *, limits=None, uploader=None, downloader=None,
              ids=(1,), window=0):
        self.downloader = downloader or FakeDownloader(files if files is not None else {1: (JPG, "photo_Nguyen Van A.jpg")})
        self.model = FakeCaseModel(ticket(ids))
        settings = config(mode, window=window, attachments=limits)
        return Pipeline(settings, self.store, self.model, self.github,
                        uploader=self.uploader if uploader is None else uploader,
                        downloader=self.downloader)

    def issue_body(self):
        return self.github.calls[0].body


class ImageInIssueTests(AttachmentBase):
    def test_case_with_image_embeds_markdown_built_by_code_with_case_key_path(self):
        pipeline = self.build()
        result = pipeline.process(photo(1))
        self.assertEqual(result.decision, "issue")
        url = f"{ASSET_BASE}/{TAG}/1-1.jpg"
        self.assertIn(f"![Ảnh 1]({url})", self.issue_body())
        self.assertIn("### 🖼 Minh họa / Evidence", self.issue_body())
        self.assertNotIn("Ảnh đính kèm", self.issue_body())   # no sub-label any more (12.16)
        self.assertEqual(self.uploader.releases, [TAG])
        self.assertEqual([(item["tag"], item["asset"]) for item in self.uploader.uploads], [(TAG, "1-1.jpg")])
        self.assertEqual(result.images, 1)

    def test_images_upload_before_the_issue_is_created(self):
        events = []
        self.github.before_create = lambda: events.append("issue")
        original = self.uploader.upload
        self.uploader.upload = lambda *args: (events.append("upload"), original(*args))[1]
        self.build().process(photo(1))
        self.assertEqual(events, ["upload", "issue"])

    def test_file_exists_in_private_temp_directory_during_upload_and_is_deleted_after(self):
        self.build().process(photo(1))
        upload = self.uploader.uploads[0]
        self.assertTrue(upload["existed"])
        self.assertTrue(upload["name_matches"])
        if os.name == "posix":
            self.assertEqual(upload["mode"], 0o700)
        self.assertFalse(upload["path"].exists())
        self.assertFalse(upload["path"].parent.exists())
        for directory in self.downloader.directories:
            self.assertFalse(directory.exists())

    def test_every_image_of_the_case_is_uploaded_with_message_id_and_ordinal_names(self):
        pipeline = self.build(files={1: (JPG, "a.jpg"), 2: (PNG, "b.png"), 3: (WEBP, "c.webp")},
                              ids=(1, 2, 3), window=15)
        for index in (1, 2, 3):
            pipeline.process(photo(index, received_at=NOW))
        self.clock.advance(60)
        pipeline.close_due_cases()
        self.assertEqual([item["asset"] for item in self.uploader.uploads], ["1-1.jpg", "2-2.png", "3-3.webp"])
        for number, name in enumerate(("1-1.jpg", "2-2.png", "3-3.webp"), 1):
            self.assertIn(f"![Ảnh {number}]({ASSET_BASE}/{TAG}/{name})", self.issue_body())

    # 12.12 changed this test: code no longer calls "ok" noise, so the case is set aside by the
    # (fake) model's chat verdict. The point is unchanged: nothing is downloaded or uploaded.
    def test_a_case_the_model_sets_aside_downloads_and_uploads_nothing(self):
        pipeline = self.build()
        self.model.analysis = CaseAnalysis("noise", classification(type="noise", confidence=0.9), (1,), "chat")
        # 12.13 changed this: the photo is untagged now (a tagged one is forced into an issue, so its
        # image would be uploaded). Untagged + model chat => chat, nothing downloaded.
        result = pipeline.process(photo(1, text="ok", mentions_listener=False))
        self.assertEqual(result.decision, "chat")
        self.assertEqual((self.downloader.calls, self.uploader.uploads, self.uploader.releases), ([], [], []))

    def test_case_without_images_does_not_touch_gh_or_telegram(self):
        pipeline = self.build()
        pipeline.process(message(1, "app nexora đăng nhập lỗi rồi", sender_id=1))
        self.assertEqual((self.downloader.calls, self.uploader.releases), ([], []))
        self.assertNotIn("![", self.issue_body())

    def test_image_limit_zero_turns_the_feature_off_silently(self):
        pipeline = self.build(limits=AttachmentsConfig(max_per_case=0))
        pipeline.process(photo(1))
        self.assertEqual((self.downloader.calls, self.uploader.releases), ([], []))
        self.assertNotIn("![", self.issue_body())
        self.assertNotIn("Ảnh bị bỏ qua", self.issue_body())
        self.assertEqual(len(self.github.calls), 1)


class ImageLimitTests(AttachmentBase):
    def test_oversized_wrong_format_and_excess_images_are_skipped_with_notes(self):
        limits = AttachmentsConfig(max_bytes=1000, max_per_case=2, max_per_hour=60)
        files = {1: (JPG, "ok.jpg"), 2: (b"\xff\xd8\xff" + b"0" * 2000, "big.jpg"),
                 3: (b"GIF89a" + b"0" * 50, "anim.jpg"), 4: (PNG, "ok.png"), 5: (WEBP, "extra.webp")}
        pipeline = self.build(files=files, limits=limits, ids=(1,), window=15)
        for index in (1, 2, 3, 4, 5):
            pipeline.process(photo(index, image_size=None))
        self.clock.advance(60)
        pipeline.close_due_cases()
        body = self.issue_body()
        self.assertEqual([item["asset"] for item in self.uploader.uploads], ["1-1.jpg", "4-2.png"])
        self.assertIn("Ảnh bị bỏ qua (1 ảnh): quá dung lượng cho phép", body)
        self.assertIn("Ảnh bị bỏ qua (1 ảnh): sai định dạng (chỉ nhận JPG, PNG, WEBP)", body)
        self.assertIn("Ảnh bị bỏ qua (1 ảnh): vượt giới hạn số ảnh", body)
        self.assertEqual(len(self.github.calls), 1)

    def test_declared_size_over_limit_is_skipped_without_downloading(self):
        limits = AttachmentsConfig(max_bytes=1000)
        pipeline = self.build(limits=limits)
        pipeline.process(photo(1, image_size=5000))
        self.assertEqual(self.downloader.calls, [])
        self.assertEqual(self.uploader.uploads, [])
        self.assertIn("quá dung lượng cho phép", self.issue_body())

    def test_declared_non_allowed_mime_is_skipped_without_downloading(self):
        pipeline = self.build()
        pipeline.process(photo(1, image_mime="image/gif"))
        self.assertEqual(self.downloader.calls, [])
        self.assertIn("sai định dạng", self.issue_body())

    def test_format_is_detected_from_content_not_from_name_or_mime(self):
        with tempfile.TemporaryDirectory() as directory:
            cases = {"a.png": (JPG, "jpg"), "b.jpg": (PNG, "png"), "c.bin": (WEBP, "webp"),
                     "d.jpg": (b"<svg></svg>", None), "e.jpg": (b"", None)}
            for name, (data, expected) in cases.items():
                path = Path(directory) / name
                path.write_bytes(data)
                self.assertEqual(sniff_extension(path), expected, name)
            self.assertIsNone(sniff_extension(Path(directory) / "missing"))

    def test_hourly_upload_limit_is_separate_from_the_issue_limit(self):
        limits = AttachmentsConfig(max_per_hour=1)
        pipeline = self.build(files={1: (JPG, "a.jpg"), 2: (PNG, "b.png")}, limits=limits, ids=(1, 2),
                              window=15)
        pipeline.process(photo(1))
        pipeline.process(photo(2))
        self.clock.advance(60)
        pipeline.close_due_cases()
        self.assertEqual(len(self.uploader.uploads), 1)
        self.assertIn("Ảnh bị bỏ qua (1 ảnh): đạt giới hạn ảnh mỗi giờ", self.issue_body())
        self.assertEqual(len(self.github.calls), 1)
        self.assertEqual(self.store.count_recent_attachments(), 1)
        self.assertEqual(self.store.count_recent_issues(), 1)

    def test_attachment_quota_uses_a_sixty_minute_window(self):
        self.assertIsNotNone(self.store.reserve_attachment_call("k", 1, "1-1.jpg", 1))
        self.assertIsNone(self.store.reserve_attachment_call("k", 2, "2-2.jpg", 1))
        self.clock.advance(61)
        self.assertEqual(self.store.count_recent_attachments(), 0)
        self.assertIsNotNone(self.store.reserve_attachment_call("k", 2, "2-2.jpg", 1))

    def test_images_are_not_uploaded_when_the_issue_quota_is_already_spent(self):
        self.store.start_classifying(photo(90))
        self.store.reserve_github_call(photo(90), 1)
        pipeline = self.build()
        pipeline.config = config("live", limit=1)
        with self.assertLogs("tg_triage.pipeline", level="WARNING"):
            self.assertEqual(pipeline.process(photo(1)).decision, "rate_limited")
        self.assertEqual((self.downloader.calls, self.uploader.releases), ([], []))


class ImageFailureTests(AttachmentBase):
    def test_upload_failure_keeps_the_issue_and_does_not_retry(self):
        self.uploader = FakeUploader(error=RuntimeError(f"{FAKE_TOKEN} https://uploads.example/secret"))
        pipeline = self.build(files={1: (JPG, "a.jpg"), 2: (PNG, "b.png")}, ids=(1, 2), window=15)
        pipeline.process(photo(1))
        pipeline.process(photo(2))
        self.clock.advance(60)
        with self.assertLogs("tg_triage", level="INFO") as logs:
            result = pipeline.close_due_cases()[0]
        self.assertEqual(result.decision, "issue")
        self.assertEqual(len(self.github.calls), 1)
        self.assertEqual(len(self.uploader.uploads), 1)
        self.assertIn("Ảnh đính kèm không tải được: RuntimeError", self.issue_body())
        self.assertNotIn("![", self.issue_body())
        output = "\n".join(logs.output)
        self.assertIn("exception=RuntimeError", output)
        for secret in (FAKE_TOKEN, "uploads.example", "secret"):
            self.assertNotIn(secret, output)
            self.assertNotIn(secret, self.issue_body())
        self.assertEqual(self.uploader.uploads[0]["path"].exists(), False)

    def test_release_creation_failure_keeps_the_issue(self):
        self.uploader = FakeUploader(release_error=UploadError("gh release create failed"))
        pipeline = self.build()
        self.assertEqual(pipeline.process(photo(1)).decision, "issue")
        self.assertEqual(self.uploader.uploads, [])
        self.assertIn("Ảnh đính kèm không tải được: UploadError", self.issue_body())

    def test_download_failure_keeps_the_issue_and_logs_only_the_class(self):
        downloader = FakeDownloader(error=OSError(f"{FAKE_TOKEN} Nguyen Van A secret.jpg"))
        pipeline = self.build(downloader=downloader)
        with self.assertLogs("tg_triage", level="INFO") as logs:
            self.assertEqual(pipeline.process(photo(1)).decision, "issue")
        self.assertIn("Ảnh đính kèm không tải được: OSError", self.issue_body())
        self.assertEqual(self.uploader.uploads, [])
        output = "\n".join(logs.output)
        self.assertIn("exception=OSError", output)
        self.assertNotIn("secret.jpg", output + self.issue_body())
        self.assertNotIn(FAKE_TOKEN, output)

    def test_missing_downloader_or_uploader_never_blocks_the_issue(self):
        pipeline = Pipeline(config("live"), self.store, FakeCaseModel(ticket([1])), self.github)
        self.assertEqual(pipeline.process(photo(1)).decision, "issue")
        self.assertIn("Ảnh đính kèm không tải được: DownloaderUnavailable", self.issue_body())

    def test_unexpected_attachment_error_still_creates_the_issue(self):
        pipeline = self.build()
        with patch.object(attachments.AttachmentProcessor, "process", side_effect=ValueError("PRIVATE")), \
                self.assertLogs("tg_triage", level="WARNING") as logs:
            self.assertEqual(pipeline.process(photo(1)).decision, "issue")
        self.assertIn("Ảnh đính kèm không tải được: ValueError", self.issue_body())
        self.assertNotIn("PRIVATE", "\n".join(logs.output))

    def test_invalid_url_returned_by_an_uploader_is_never_embedded(self):
        class BadUploader(FakeUploader):
            def upload(self, tag, path, asset_name):
                return "javascript:alert(1)) ![x](https://evil.invalid"

        pipeline = self.build(uploader=BadUploader())
        pipeline.process(photo(1))
        self.assertNotIn("evil.invalid", self.issue_body())
        self.assertNotIn("javascript", self.issue_body())
        self.assertIn("UploadError", self.issue_body())


class DryRunImageTests(AttachmentBase):
    def test_dry_run_downloads_and_validates_but_never_calls_gh_or_github(self):
        self.uploader = FakeUploader(error=AssertionError("gh must not be called"),
                                     release_error=AssertionError("release must not be created"))
        self.github = FakeGitHub(error=AssertionError("GitHub must not be called"))
        pipeline = self.build("dry-run", files={1: (JPG, "a.jpg"), 2: (b"not an image", "b.jpg")},
                              ids=(1, 2), window=15)
        pipeline.process(photo(1))
        pipeline.process(photo(2))
        self.clock.advance(60)
        with self.assertLogs("tg_triage.pipeline", level="INFO") as logs:
            result = pipeline.close_due_cases()[0]
        self.assertEqual(result.decision, "dry_run_issue")
        self.assertEqual(self.downloader.calls, [(CHAT, 1), (CHAT, 2)])
        self.assertEqual((self.uploader.releases, self.uploader.uploads, self.github.calls), ([], [], []))
        output = "\n".join(logs.output)
        self.assertIn("attachments_dry_run=1", output)
        self.assertIn("images=0", output)
        self.assertEqual(self.store.count_recent_attachments(), 0)
        for directory in self.downloader.directories:
            self.assertFalse(directory.exists())

    def test_dry_run_with_no_images_logs_zero(self):
        pipeline = self.build("dry-run")
        with self.assertLogs("tg_triage.pipeline", level="INFO") as logs:
            pipeline.process(message(1, "app nexora đăng nhập lỗi rồi", sender_id=1))
        self.assertIn("attachments_dry_run=0", "\n".join(logs.output))


class AssetNameTests(AttachmentBase):
    def test_names_never_contain_the_sender_or_markdown_or_the_telegram_file_name(self):
        hostile = "Nguyen [Van] A_(x)!.jpg"
        pipeline = self.build(files={1: (JPG, hostile)})
        pipeline.process(photo(1, sender_name="Nguyen Van A"))
        for upload in self.uploader.uploads:
            self.assertRegex(upload["asset"], r"^\d+-\d+\.(jpg|png|webp)$")
            self.assertNotIn("Nguyen", upload["asset"])
        image_lines = [line for line in self.issue_body().splitlines() if "![" in line]
        self.assertEqual(len(image_lines), 1)
        self.assertNotIn("Nguyen", image_lines[0])
        self.assertNotIn(hostile, self.issue_body())

    def test_release_tag_replaces_characters_git_refuses(self):
        self.assertEqual(release_tag("-1001234567890:1"), "attachments--1001234567890_1")
        self.assertNotIn(":", release_tag("a:b c/d"))
        self.assertTrue(re.fullmatch(r"attachments-[A-Za-z0-9._-]+", release_tag("-100:9 x/y")))


class FakeRunner:
    def __init__(self, *codes, error=None):
        self.codes = list(codes)
        self.calls = []
        self.error = error

    def __call__(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        if self.error:
            raise self.error
        return subprocess.CompletedProcess(argv, self.codes.pop(0), stdout=b"", stderr=f"{FAKE_TOKEN}".encode())


class GhCliUploaderTests(unittest.TestCase):
    def uploader(self, runner):
        return GhCliUploader("sandbox/triage", FAKE_TOKEN, runner=runner, executable="gh")

    def test_existing_release_is_reused_and_upload_uses_argv_list_without_shell(self):
        runner = FakeRunner(0, 0)
        uploader = self.uploader(runner)
        uploader.ensure_release(TAG)
        url = uploader.upload(TAG, Path("1-1.jpg"), "1-1.jpg")
        self.assertEqual(url, f"{ASSET_BASE}/{TAG}/1-1.jpg")
        self.assertEqual([argv for argv, _ in runner.calls], [
            ["gh", "release", "view", TAG, "--repo", "sandbox/triage"],
            ["gh", "release", "upload", TAG, "1-1.jpg", "--repo", "sandbox/triage", "--clobber"]])
        for argv, kwargs in runner.calls:
            self.assertIsInstance(argv, list)
            self.assertIs(kwargs["shell"], False)
            self.assertNotIn(FAKE_TOKEN, " ".join(argv))
            self.assertEqual(kwargs["env"]["GH_TOKEN"], FAKE_TOKEN)

    def test_missing_release_is_created_not_as_latest(self):
        runner = FakeRunner(1, 0)
        self.uploader(runner).ensure_release(TAG)
        argv = runner.calls[1][0]
        self.assertEqual(argv[:5], ["gh", "release", "create", TAG, "--repo"])
        self.assertIn("--latest=false", argv)
        self.assertNotIn(FAKE_TOKEN, " ".join(argv))

    def test_failures_raise_an_error_without_remote_output_or_token(self):
        for runner, call in ((FakeRunner(1), lambda u: u.upload(TAG, Path("1-1.jpg"), "1-1.jpg")),
                             (FakeRunner(1, 1), lambda u: u.ensure_release(TAG)),
                             (FakeRunner(error=OSError(FAKE_TOKEN)), lambda u: u.ensure_release(TAG)),
                             (FakeRunner(error=subprocess.TimeoutExpired(["gh", FAKE_TOKEN], 1)),
                              lambda u: u.ensure_release(TAG))):
            with self.assertRaises(UploadError) as caught:
                call(self.uploader(runner))
            self.assertNotIn(FAKE_TOKEN, str(caught.exception))
            self.assertIsNone(caught.exception.__cause__)

    def test_asset_name_must_equal_the_file_name(self):
        with self.assertRaises(UploadError):
            self.uploader(FakeRunner(0)).upload(TAG, Path("secret-name.jpg"), "1-1.jpg")

    def test_missing_gh_binary_is_reported_by_class_only(self):
        uploader = GhCliUploader("sandbox/triage", FAKE_TOKEN, runner=FakeRunner(0))
        with patch("tg_triage.attachments.shutil.which", return_value=None):
            with self.assertRaises(UploadError):
                uploader.ensure_release(TAG)

    def test_invalid_repo_or_token_is_rejected_without_echo(self):
        for repo, token in (("owner", FAKE_TOKEN), ("o/r", ""), ("o/r", "bad\ntoken")):
            with self.assertRaises(ValueError) as caught:
                GhCliUploader(repo, token)
            self.assertNotIn("bad", str(caught.exception))


class ConfigTests(unittest.TestCase):
    ENV = {"TG_API_ID": "123", "TG_API_HASH": FAKE_HASH}

    def load(self, text):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text(text, encoding="utf-8")
            return load_config(path, self.ENV)

    def without_sections(self):
        source = EXAMPLE.read_text(encoding="utf-8")
        head = source.split("# Case aggregation", 1)[0]
        return head + "[storage]\ndb_path = \"/tmp/x.sqlite3\"\n"

    def test_example_sets_the_documented_values(self):
        value = load_config(EXAMPLE, self.ENV)
        self.assertEqual((value.aggregation.window_minutes, value.aggregation.max_messages_per_case), (2, 20))
        self.assertEqual((value.attachments.max_bytes, value.attachments.max_per_case,
                          value.attachments.max_per_hour), (10485760, 5, 60))

    def test_missing_sections_use_defaults(self):
        value = self.load(self.without_sections())
        self.assertEqual((value.aggregation.window_minutes, value.aggregation.max_messages_per_case), (2, 20))
        self.assertEqual((value.attachments.max_bytes, value.attachments.max_per_case,
                          value.attachments.max_per_hour), (10485760, 5, 60))

    def test_partial_sections_fill_the_rest_with_defaults(self):
        value = self.load(self.without_sections() + "[aggregation]\nwindow_minutes = 5\n"
                          "[attachments]\nmax_per_case = 2\n")
        self.assertEqual((value.aggregation.window_minutes, value.aggregation.max_messages_per_case), (5, 20))
        self.assertEqual((value.attachments.max_per_case, value.attachments.max_per_hour), (2, 60))

    def test_zero_is_allowed_where_it_means_off(self):
        value = self.load(self.without_sections() + "[aggregation]\nwindow_minutes = 0\n"
                          "[attachments]\nmax_per_case = 0\nmax_per_hour = 0\n")
        self.assertEqual((value.aggregation.window_minutes, value.attachments.max_per_case,
                          value.attachments.max_per_hour), (0, 0, 0))

    def test_wrong_type_or_negative_values_are_config_errors_naming_the_field(self):
        bad = (("aggregation", "window_minutes", "-1"), ("aggregation", "window_minutes", '"15"'),
               ("aggregation", "window_minutes", "1.5"), ("aggregation", "window_minutes", "true"),
               ("aggregation", "max_messages_per_case", "0"), ("aggregation", "max_messages_per_case", "-3"),
               ("aggregation", "max_messages_per_case", "[1]"),
               ("attachments", "max_bytes", "0"), ("attachments", "max_bytes", "-1"),
               ("attachments", "max_bytes", '"10MB"'), ("attachments", "max_per_case", "-1"),
               ("attachments", "max_per_case", "2.0"), ("attachments", "max_per_hour", "-1"),
               ("attachments", "max_per_hour", "false"))
        for section, key, value in bad:
            with self.subTest(section=section, key=key, value=value):
                with self.assertRaisesRegex(ConfigError, f"{section}.{key}"):
                    self.load(self.without_sections() + f"[{section}]\n{key} = {value}\n")

    def test_section_that_is_not_a_table_is_a_config_error(self):
        for name in ("aggregation", "attachments"):
            with self.assertRaisesRegex(ConfigError, name):
                self.load(f"{name} = 5\n" + self.without_sections().replace("mode", "mode", 1))

    def test_max_messages_per_window_defaults_to_40_and_is_validated(self):
        self.assertEqual(load_config(EXAMPLE, self.ENV).aggregation.max_messages_per_window, 40)
        self.assertEqual(self.load(self.without_sections()).aggregation.max_messages_per_window, 40)
        value = self.load(self.without_sections() + "[aggregation]\nmax_messages_per_window = 7\n")
        self.assertEqual(value.aggregation.max_messages_per_window, 7)
        for bad in ("0", "-1", "4.0", '"40"', "true"):
            with self.subTest(value=bad):
                with self.assertRaisesRegex(ConfigError, "aggregation.max_messages_per_window"):
                    self.load(self.without_sections() + f"[aggregation]\nmax_messages_per_window = {bad}\n")

    def test_check_config_redaction_includes_the_new_sections(self):
        redacted = load_config(EXAMPLE, self.ENV).redacted()
        # 12.9 added `max_messages_per_window` (chunk size sent to the model) to this section.
        self.assertEqual(redacted["aggregation"], {
            "window_minutes": 2, "max_messages_per_case": 20, "max_messages_per_window": 40})
        self.assertEqual(redacted["attachments"]["max_per_hour"], 60)


if __name__ == "__main__":
    unittest.main()


class ImageBeforeCaptionTests(AttachmentBase):
    def test_image_without_text_waits_and_travels_with_the_explanation_that_follows(self):
        pipeline = self.build(files={1: (JPG, "screenshot.jpg")}, ids=(1, 2), window=15)
        first = message(1, "", sender_id=1, has_image=True, image_size=2048, received_at=NOW)
        self.assertEqual(pipeline.process(first).decision, "case_open")
        second = message(2, "app nexora không đăng nhập được như ảnh", sender_id=1, received_at=NOW)
        self.assertEqual(pipeline.process(second).decision, "case_open")
        self.clock.advance(60)
        self.assertEqual([item.decision for item in pipeline.close_due_cases()], ["issue"])
        self.assertIn(f"![Ảnh 1]({ASSET_BASE}/{TAG}/1-1.jpg)", self.issue_body())
        self.assertEqual(len(self.github.calls), 1)

    def test_image_only_case_with_nothing_else_ends_as_noise(self):
        pipeline = self.build(window=15)
        pipeline.process(message(1, "", sender_id=1, has_image=True, image_size=2048, received_at=NOW))
        self.clock.advance(60)
        self.assertEqual([item.decision for item in pipeline.close_due_cases()], ["noise"])
        self.assertEqual((self.github.calls, self.downloader.calls), ([], []))

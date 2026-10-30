"""Image and document attachments for case issues (Assistant definition 12.8 and 12.14).

Documents (12.14) follow the same path as images but are accepted by extension and size only:
the file is never opened, unpacked or executed, and its content never decides anything.

Images are downloaded through an injected `downloader`, validated, uploaded to a
per-case GitHub release through an injected `uploader`, and embedded by code as
Markdown. Nothing here imports Telethon. Log lines carry counts, message IDs and exception class
names only: never file names, URLs, message text or tokens.
"""

import logging
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from . import ticket_templates

LOG = logging.getLogger(__name__)

ALLOWED_MIME = frozenset(("image/jpeg", "image/png", "image/webp"))
ALT_TEXT = "Ảnh"
VISION_MAX_IMAGES = 5
VISION_BUDGET = "vision_budget"

# 12.14: the only document types accepted. Fixed in code, not configurable. For each extension, the
# MIME types Telegram may report for it. The extension is read from the file name or, when the
# name has none, from the MIME; when both are present they must agree (the MIME alone is never trusted).
DOCUMENT_MIME = {
    "pdf": frozenset(("application/pdf", "application/x-pdf")),
    "docx": frozenset(("application/vnd.openxmlformats-officedocument.wordprocessingml.document",)),
    "xlsx": frozenset(("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",)),
    "pptx": frozenset(("application/vnd.openxmlformats-officedocument.presentationml.presentation",)),
    "txt": frozenset(("text/plain",)),
    "csv": frozenset(("text/csv", "application/csv", "text/x-csv", "application/vnd.ms-excel")),
    "md": frozenset(("text/markdown", "text/x-markdown", "text/x-web-markdown", "text/plain")),
    "json": frozenset(("application/json", "text/json")),
    "html": frozenset(("text/html", "application/xhtml+xml", "application/octet-stream")),
    "zip": frozenset(("application/zip", "application/x-zip-compressed", "application/x-zip")),
}
DOCUMENT_EXTENSIONS = frozenset(DOCUMENT_MIME)
# Executable / script extensions. Anything outside the allowlist is refused anyway; this set also
# refuses a name that carries one of them anywhere (`report.exe.pdf`).
DENIED_EXTENSIONS = frozenset((
    "exe", "bat", "cmd", "sh", "ps1", "msi", "apk", "js", "vbs", "jar", "dll", "scr", "com", "app",
    "dmg", "reg", "lnk", "php", "py"))
# MIME -> extension when the name has no extension (the first listed MIME of an extension wins).
_MIME_TO_EXTENSION = {mime: extension for extension, mimes in reversed(list(DOCUMENT_MIME.items()))
                      for mime in sorted(mimes)}
_MIME_TO_EXTENSION["text/plain"] = "txt"
_MIME_TO_EXTENSION.pop("application/octet-stream",None)  # Opaque MIME alone never identifies HTML.
_SAFE_URL = re.compile(r"https://[A-Za-z0-9._~/%+-]+")


class AttachmentError(RuntimeError):
    """Attachment failure whose message never carries remote output or credentials."""


class UploadError(AttachmentError):
    pass


class DownloaderUnavailable(AttachmentError):
    pass


class UploaderUnavailable(AttachmentError):
    pass


class AttachmentUploader(Protocol):
    """Anything with these two methods can upload case images (a fake in tests)."""

    def ensure_release(self, tag: str) -> None:
        """Create the release for `tag` when it does not exist yet."""

    def upload(self, tag: str, path: Path, asset_name: str) -> str:
        """Upload `path` as `asset_name` and return its public asset URL."""


def release_tag(case_key: str) -> str:
    """`attachments-<case_key>`; ':' is not valid in a git tag, so it becomes '_'."""
    return "attachments-" + re.sub(r"[^A-Za-z0-9._-]", "_", case_key)


def asset_name(message_id: int, ordinal: int, extension: str) -> str:
    return f"{int(message_id)}-{int(ordinal)}.{extension}"


def document_extension(name, mime) -> str | None:
    """The allowlisted extension of a document, or None when it must be refused (12.14).

    Only the declared file name and MIME are used; the file itself is never looked at.
    """
    mime = mime.split(";")[0].strip().lower() if isinstance(mime, str) else None
    name = name.strip().rstrip(". ").lower() if isinstance(name, str) else None
    name = re.split(r"[\\/]", name)[-1] if name else None
    name_extension = None
    if name and "." in name:
        parts = name.split(".")[1:]
        if any(part.strip() in DENIED_EXTENSIONS for part in parts):
            return None
        name_extension = parts[-1].strip()
        if name_extension=='htm':name_extension='html'
    if name_extension is not None:
        if name_extension not in DOCUMENT_EXTENSIONS:
            return None
        if mime is not None and mime not in DOCUMENT_MIME[name_extension]:
            return None
        return name_extension
    return _MIME_TO_EXTENSION.get(mime) if mime else None


def sniff_extension(path) -> str | None:
    """Detect JPG/PNG/WEBP from magic bytes, never from names or Telegram metadata."""
    try:
        with open(path, "rb") as stream:
            head = stream.read(16)
    except OSError:
        return None
    if head.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    return None


class GhCliUploader:
    """Upload through the `gh` CLI. argv is a list (no shell); the token goes via env only."""

    def __init__(self, repo, token, *, runner=subprocess.run, executable=None, timeout=120):
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
            raise ValueError("GH_REPO: expected owner/repo")
        if not token.strip() or any(ord(ch) < 32 or ord(ch) == 127 for ch in token):
            raise ValueError("GH_TOKEN: expected non-empty token without control characters")
        self.repo = repo
        self._token = token
        self._runner = runner
        self._executable = executable
        self._timeout = timeout

    def _run(self, *arguments):
        executable = self._executable or shutil.which("gh")
        if executable is None:
            raise UploadError("gh executable not found")
        env = {**os.environ, "GH_TOKEN": self._token, "GH_PROMPT_DISABLED": "1", "NO_COLOR": "1"}
        try:
            completed = self._runner(
                [executable, *arguments], env=env, capture_output=True, timeout=self._timeout,
                check=False, shell=False, stdin=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError):
            # Do not chain: the original exception text could echo argv or output.
            raise UploadError("gh could not be run") from None
        return completed.returncode

    def ensure_release(self, tag):
        if self._run("release", "view", tag, "--repo", self.repo) == 0:
            return
        code = self._run("release", "create", tag, "--repo", self.repo,
                         "--title", tag, "--notes", "Ảnh đính kèm tự động từ tg-triage",
                         "--latest=false")
        if code != 0:
            raise UploadError("gh release create failed")

    def upload(self, tag, path, asset):
        if Path(path).name != asset:
            raise UploadError("asset name must equal the file name")
        if self._run("release", "upload", tag, str(path), "--repo", self.repo, "--clobber") != 0:
            raise UploadError("gh release upload failed")
        return f"https://github.com/{self.repo}/releases/download/{tag}/{asset}"


@dataclass
class AttachmentResult:
    images: list = field(default_factory=list)   # [(alt text, url)] for the issue body
    notes: list = field(default_factory=list)    # fixed, code-written Vietnamese notes
    dry_run_count: int = 0                       # images that passed validation in dry-run
    attempted: int = 0                           # images found on candidate messages
    documents: list = field(default_factory=list)  # [(sender's file name or None, url)] (12.14)
    documents_dry_run: int = 0                   # documents that passed validation in dry-run
    image_skips: list = field(default_factory=list)  # (chat_id, message_id, reason)
    image_failures: list = field(default_factory=list)  # (chat_id, message_id, exception class)


class AttachmentProcessor:
    def __init__(self, settings, store, *, repo="", downloader=None, uploader=None):
        self.settings = settings
        self.store = store
        self.repo = repo
        self.downloader = downloader
        self.uploader = uploader

    def prepare_images(self, case_key, messages):
        """Private, bounded batch shared by vision and upload; caller must close it."""
        return PreparedImages(self, case_key, messages)

    def process(self, case_key, messages, *, live, prepared=None, document_reads=()) -> AttachmentResult:
        result = AttachmentResult()
        limits = self.settings
        candidates = [m for m in messages if m.has_image] if limits.max_per_case > 0 else []
        documents = ([m for m in messages if m.has_document]
                     if limits.max_documents_per_case > 0 else [])
        if not candidates and not documents:
            return result
        result.attempted = len(candidates)
        directory = Path(tempfile.mkdtemp(prefix="tg-triage-"))
        try:
            os.chmod(directory, 0o700)
            if prepared is None:
                accepted = self._download(case_key, candidates, directory, result) if candidates else []
            else:
                accepted = self._download(case_key, candidates, directory, result, reuse=prepared) if candidates else []
                seen = {(m.chat_id, m.message_id) for m, _ in prepared.accepted}
                unseen = sum((m.chat_id, m.message_id) not in seen for m, _ in accepted)
                if unseen:
                    result.notes.append(ticket_templates.active().render(
                        "_common.md", "note.unseen_images", {"count": str(unseen)}))
            accepted_documents = (self._download_documents(documents, directory, result, document_reads)
                                  if documents else [])
            if not live:
                result.dry_run_count = len(accepted)
                result.documents_dry_run = len(accepted_documents)
                return result
            state = {"ensured": False}   # one `release view/create` per case, images and documents
            if accepted:
                self._upload(case_key, accepted, result, state)
            if accepted_documents:
                self._upload_documents(case_key, accepted_documents, result, state)
        finally:
            # Temporary files never outlive the case, whether or not upload worked.
            shutil.rmtree(directory, ignore_errors=True)
        return result

    def _download(self, case_key, candidates, directory, result, *, limit=None,
                  limit_reason="vượt giới hạn số ảnh", reuse=None, label="Images"):
        """Validate each image; every skipped image is counted by reason, never named."""
        limits = self.settings
        skipped = {"vượt giới hạn số ảnh": 0, "quá dung lượng cho phép": 0,
                   "sai định dạng (chỉ nhận JPG, PNG, WEBP)": 0, "không tải được": 0}
        limit = limits.max_per_case if limit is None else limit
        accepted = []
        reused = {(m.chat_id, m.message_id): p for m, p in reuse.accepted} if reuse else {}
        previous = {item[:2]: item for item in reuse.result.image_skips if item[2] != VISION_BUDGET} if reuse else {}
        failures = {item[:2]: item for item in reuse.result.image_failures} if reuse else {}
        candidates = sorted(candidates, key=lambda m: (m.chat_id, m.message_id) not in reused)
        for message in candidates:
            key = (message.chat_id, message.message_id)
            if key in previous:
                item = previous[key]
                result.image_skips.append(item)
                skipped[item[2]] += 1
                if key in failures:
                    result.image_failures.append(failures[key])
                continue
            if len(accepted) >= limit:
                skipped["vượt giới hạn số ảnh"] += 1
                result.image_skips.append((*key, limit_reason))
                continue
            if key in reused:
                accepted.append((message, reused[key]))
                continue
            if message.image_size is not None and message.image_size > limits.max_bytes:
                skipped["quá dung lượng cho phép"] += 1
                result.image_skips.append((message.chat_id, message.message_id, "quá dung lượng cho phép"))
                continue
            if message.image_mime is not None and message.image_mime not in ALLOWED_MIME:
                skipped["sai định dạng (chỉ nhận JPG, PNG, WEBP)"] += 1
                result.image_skips.append((message.chat_id, message.message_id, "sai định dạng (chỉ nhận JPG, PNG, WEBP)"))
                continue
            try:
                if self.downloader is None:
                    raise DownloaderUnavailable("no downloader configured")
                workdir = directory / str(message.message_id)
                workdir.mkdir(mode=0o700, exist_ok=True)
                downloaded = self.downloader(message.chat_id, message.message_id, workdir)
                if downloaded is None:
                    skipped["không tải được"] += 1
                    result.image_skips.append((message.chat_id, message.message_id, "không tải được"))
                    continue
                downloaded = Path(downloaded)
                resolved = downloaded.resolve()
                if (downloaded.is_symlink() or not resolved.is_file()
                        or workdir.resolve() not in resolved.parents):
                    raise AttachmentError("downloaded image outside work directory")
                if downloaded.stat().st_size > limits.max_bytes:
                    skipped["quá dung lượng cho phép"] += 1
                    result.image_skips.append((message.chat_id, message.message_id, "quá dung lượng cho phép"))
                    continue
                extension = sniff_extension(downloaded)
                if extension is None:
                    skipped["sai định dạng (chỉ nhận JPG, PNG, WEBP)"] += 1
                    result.image_skips.append((message.chat_id, message.message_id, "sai định dạng (chỉ nhận JPG, PNG, WEBP)"))
                    continue
                final = directory / asset_name(message.message_id, len(accepted) + 1, extension)
                os.replace(downloaded, final)
            except Exception as error:
                result.image_failures.append((message.chat_id, message.message_id, type(error).__name__))
                LOG.warning("Attachment download failed message_id=%s exception=%s",
                            message.message_id, type(error).__name__)
                skipped["không tải được"] += 1
                result.image_skips.append((message.chat_id, message.message_id, "không tải được"))
                continue
            accepted.append((message, final))
        def ids(items):
            return ",".join(f"{chat}:{message}" for chat, message in items[:20])

        LOG.info("%s found=%s accepted=%s skipped_limit=%s skipped_size=%s skipped_type=%s "
                 "failed=%s accepted_ids=%s skipped_ids=%s", label, len(candidates), len(accepted),
                 skipped["vượt giới hạn số ảnh"], skipped["quá dung lượng cho phép"],
                 skipped["sai định dạng (chỉ nhận JPG, PNG, WEBP)"], skipped["không tải được"],
                 ids([(m.chat_id, m.message_id) for m, _ in accepted]),
                 ids([(chat, message) for chat, message, _ in result.image_skips]))
        result.notes.extend(self._image_notes(result.image_skips, result.image_failures))
        return accepted

    @staticmethod
    def _image_notes(skips, failures):
        reasons = ("vượt giới hạn số ảnh", "quá dung lượng cho phép",
                   "sai định dạng (chỉ nhận JPG, PNG, WEBP)", "không tải được")
        notes = []
        if failures:
            notes.append(f"Ảnh đính kèm không tải được: {failures[0][2]}")
        for reason in reasons:
            count = sum(item[2] == reason for item in skips)
            if count and not (reason == "không tải được" and failures):
                notes.append(f"Ảnh bị bỏ qua ({count} ảnh): {reason}")
        return notes

    def _upload(self, case_key, accepted, result, state=None):
        tag = release_tag(case_key)
        state = state if state is not None else {"ensured": False}
        try:
            if self.uploader is None:
                raise UploaderUnavailable("no uploader configured")
            for index, (message, path) in enumerate(accepted):
                call_id = self.store.reserve_attachment_call(
                    case_key, message.message_id, path.name, self.settings.max_per_hour)
                if call_id is None:
                    result.notes.append(
                        f"Ảnh bị bỏ qua ({len(accepted) - index} ảnh): đạt giới hạn ảnh mỗi giờ")
                    break
                try:
                    if not state["ensured"]:
                        self.uploader.ensure_release(tag)
                        state["ensured"] = True
                    url = self.uploader.upload(tag, path, path.name)
                    if not isinstance(url, str) or not _SAFE_URL.fullmatch(url):
                        raise UploadError("invalid asset url")
                except Exception:
                    self.store.finish_attachment(call_id, "error")
                    raise
                self.store.finish_attachment(call_id, "uploaded")
                result.images.append((f"{ALT_TEXT} {index + 1}", url))
        except Exception as error:
            # One attempt per image; the first failure stops the batch (no retry loop).
            LOG.warning("Attachment upload failed images=%s exception=%s",
                        len(accepted), type(error).__name__)
            result.notes.append(f"Ảnh đính kèm không tải được: {type(error).__name__}")

    def _download_documents(self, candidates, directory, result,document_reads=()):
        """Validate each document by name, MIME and size only (12.14); count every refusal by reason.

        Nothing here opens, unpacks or executes a file, and nothing logs a file name.
        """
        limits = self.settings
        skipped = {"type": 0, "limit": 0, "size": 0, "failed": 0}
        accepted, failure = [], None
        for message in candidates:
            extension = document_extension(message.document_name, message.document_mime)
            if extension is None:
                skipped["type"] += 1
                continue
            if len(accepted) >= limits.max_documents_per_case:
                skipped["limit"] += 1
                continue
            if message.document_size is not None and message.document_size > limits.max_bytes:
                skipped["size"] += 1
                continue
            try:
                if self.downloader is None:
                    raise DownloaderUnavailable("no downloader configured")
                workdir = directory / str(message.message_id)
                workdir.mkdir(mode=0o700, exist_ok=True)
                downloaded = self.downloader(message.chat_id, message.message_id, workdir)
                if downloaded is None:
                    skipped["failed"] += 1
                    continue
                downloaded = Path(downloaded)
                # Telethon names the file after the sender's file name: it must have stayed inside
                # the private work directory and be a plain file (no link, no directory).
                resolved = downloaded.resolve()
                if (downloaded.is_symlink() or not resolved.is_file()
                        or workdir.resolve() not in resolved.parents):
                    raise AttachmentError("downloaded file outside work directory")
                if downloaded.stat().st_size > limits.max_bytes:
                    skipped["size"] += 1
                    continue
                read=next((r for r in document_reads if r['message_id']==message.message_id and r.get('read_status')=='read'),None)
                if read is not None:
                    import hashlib
                    if downloaded.stat().st_size!=read['byte_count'] or hashlib.sha256(downloaded.read_bytes()).hexdigest()!=read['sha256']:
                        result.notes.append('Tài liệu tải lại không khớp nội dung đã đọc; tệp này không được đính kèm.')
                        skipped['failed']+=1
                        continue
                final = directory / asset_name(message.message_id, len(accepted) + 1, extension)
                os.replace(downloaded, final)
            except Exception as error:
                failure = failure or type(error).__name__
                LOG.warning("Document download failed message_id=%s exception=%s",
                            message.message_id, type(error).__name__)
                skipped["failed"] += 1
                continue
            accepted.append((message, final))
        LOG.info("Documents found=%s accepted=%s rejected_type=%s rejected_limit=%s rejected_size=%s "
                 "failed=%s", len(candidates), len(accepted), skipped["type"], skipped["limit"],
                 skipped["size"], skipped["failed"])
        if failure:
            result.notes.append(f"Tài liệu không tải được: {failure}")
        reasons = (("type", "định dạng không được nhận"), ("limit", "vượt giới hạn số tài liệu"),
                   ("size", "quá dung lượng cho phép"))
        for key, reason in reasons:
            if skipped[key]:
                result.notes.append(f"Tài liệu bị bỏ qua ({skipped[key]} tài liệu): {reason}")
        if skipped["failed"] and not failure:
            result.notes.append(f"Tài liệu bị bỏ qua ({skipped['failed']} tài liệu): không tải được")
        return accepted

    def _upload_documents(self, case_key, accepted, result, state):
        """Same mechanism as images: one `gh release upload` per file, counted in max_per_hour;
        the first failure stops the batch and is never retried."""
        tag = release_tag(case_key)
        try:
            if self.uploader is None:
                raise UploaderUnavailable("no uploader configured")
            for index, (message, path) in enumerate(accepted):
                call_id = self.store.reserve_attachment_call(
                    case_key, message.message_id, path.name, self.settings.max_per_hour)
                if call_id is None:
                    result.notes.append(
                        f"Tài liệu bị bỏ qua ({len(accepted) - index} tài liệu): "
                        "đạt giới hạn tệp đính kèm mỗi giờ")
                    break
                try:
                    if not state["ensured"]:
                        self.uploader.ensure_release(tag)
                        state["ensured"] = True
                    url = self.uploader.upload(tag, path, path.name)
                    if not isinstance(url, str) or not _SAFE_URL.fullmatch(url):
                        raise UploadError("invalid asset url")
                except Exception:
                    self.store.finish_attachment(call_id, "error")
                    raise
                self.store.finish_attachment(call_id, "uploaded")
                result.documents.append((message.document_name, url))
        except Exception as error:
            LOG.warning("Document upload failed documents=%s exception=%s",
                        len(accepted), type(error).__name__)
            result.notes.append(f"Tài liệu không tải được: {type(error).__name__}")


class PreparedImages:
    """Only validated image files; no documents, persisted bytes, or logged content."""

    def __init__(self, processor, case_key, messages):
        self.directory = Path(tempfile.mkdtemp(prefix="tg-triage-vision-"))
        self.result = AttachmentResult()
        self.accepted = []
        try:
            os.chmod(self.directory, 0o700)
            candidates = [m for m in messages if m.has_image]
            self.accepted = processor._download(case_key, candidates, self.directory, self.result,
                                               limit=min(VISION_MAX_IMAGES, processor.settings.max_per_case),
                                               limit_reason=VISION_BUDGET, label="Vision batch")
        except BaseException:
            self.close()
            raise

    def for_messages(self, messages):
        ids = {(m.chat_id, m.message_id) for m in messages}
        return {m.message_id: p for m, p in self.accepted if (m.chat_id, m.message_id) in ids}

    def close(self):
        shutil.rmtree(self.directory, ignore_errors=True)

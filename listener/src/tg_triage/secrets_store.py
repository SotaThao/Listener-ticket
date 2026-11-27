"""Listener credentials, their metadata, and the root CLI's local rotations."""

import json
import os
import re
import shlex
import stat
import tempfile
import unicodedata
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable


DEFAULT_DIR = Path("/etc/tg-triage/secrets")
DEFAULT_AUDIT_LOG = Path("/var/log/tg-triage/secrets-audit.log")
DEFAULT_ENV_FILE = Path("/etc/agentfleet/tg-triage.env")

CATALOG = {
    "gh-token": "Token GitHub tạo và cập nhật issue",
    "tg-api-id": "API ID Telegram",
    "tg-api-hash": "API hash Telegram",
    "tg-proxy-secret": "Mật khẩu MTProxy",
    "model-key.json": "Key nhà cung cấp model",
    "router-key": "Key router của trợ lý",
    "assistant-github-token": "Token GitHub chỉ đọc của trợ lý",
    "admin-password": "Mật khẩu Admin",
}
GITHUB_TOKENS = ("gh-token", "assistant-github-token")
LEGACY_ENV = {
    "GH_TOKEN": "gh-token",
    "TG_API_ID": "tg-api-id",
    "TG_API_HASH": "tg-api-hash",
    "TG_PROXY_SECRET": "tg-proxy-secret",
}
MODE_ENV = ("GH_REPO", "TG_TRIAGE_MODE")
MODEL_ENV_OVERRIDES = ("AGENTFLEET_KEY_SCOPE", "AGENTFLEET_API_KEY")
LEGACY_TOML = {
    ("model", "key_file"): "model-key.json",
    ("assistant", "router_key_file"): "router-key",
    ("assistant", "github_token_file"): "assistant-github-token",
    ("admin", "password_hash_file"): "admin-password",
}
STATUS_LABELS = {
    "ok": "OK",
    "missing": "Thiếu",
    "bad_mode": "Sai quyền",
    "bad_owner": "Sai quyền",
    "bad_format": "Sai định dạng",
    "unreadable": "Không đọc được — chạy bằng sudo",
    "expiring": "Sắp hết hạn",
    "expired": "Đã hết hạn",
}
_ACTOR = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.@-]{0,127}")
_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")


class SecretError(ValueError):
    """Names and fixed error kinds only; never wrap an upstream message."""

    def __init__(self, name, kind):
        self.kind = kind
        self.name = name
        hint = " (run secrets migrate or secrets set)" if kind == "missing" else ""
        if kind == "unreadable":
            hint = " (use sudo)"
        super().__init__(f"{name}: {kind}{hint}")


def _gid():
    import grp

    return grp.getgrnam("tglistener").gr_gid


@dataclass(frozen=True)
class Platform:
    """Unix operations are explicit so tests can exercise policy on Windows."""

    unix: bool = os.name == "posix"
    group_id: Callable = _gid
    euid: Callable = getattr(os, "geteuid", lambda: None)
    chown: Callable | None = getattr(os, "chown", None)
    fchmod: Callable | None = getattr(os, "fchmod", None)

    def permissions(self, path, gid, mode, *, fd=None):
        if self.unix:
            if self.chown is None:
                raise OSError()
            self.chown(path, 0, gid)
        if self.unix and fd is not None and self.fchmod is not None:
            self.fchmod(fd, mode)
        else:
            os.chmod(path, mode)


PLATFORM = Platform()


@dataclass(frozen=True)
class SecretPaths:
    dir: Path = DEFAULT_DIR
    audit_log: Path = DEFAULT_AUDIT_LOG


def required_names(mode, *, proxy, hosted, router_live, admin):
    required = {"tg-api-id", "tg-api-hash"}
    if mode == "live":
        required.add("gh-token")
    if proxy:
        required.add("tg-proxy-secret")
    if hosted:
        required.add("model-key.json")
    if router_live:
        required.add("router-key")
    if admin:
        required.add("admin-password")
    return frozenset(required)


def model_scope(model):
    return model.provider + "|" + model.base_url


def parse_expiry(value):
    if not isinstance(value, str) or not _DATE.fullmatch(value):
        raise ValueError()
    return date.fromisoformat(value)


def _validate_text(name, value):
    if not isinstance(value, str) or not value or any(
            char.isspace() or unicodedata.category(char) == "Cc" for char in value):
        raise SecretError(name, "bad_format")
    if name in GITHUB_TOKENS and not 20 <= len(value) <= 255:
        raise SecretError(name, "bad_format")
    if name == "tg-api-id":
        try:
            if not re.fullmatch(r"[0-9]+", value) or int(value) <= 0:
                raise ValueError()
        except ValueError:
            raise SecretError(name, "bad_format") from None
    if name == "tg-api-hash" and not re.fullmatch(r"[0-9a-fA-F]{32}", value):
        raise SecretError(name, "bad_format")
    return value


def _validate_hash(value):
    from .admin import _parse_password_hash

    try:
        _parse_password_hash(value)
    except ValueError:
        raise SecretError("admin-password", "bad_format") from None


@dataclass(frozen=True)
class SecretValue:
    data: bytes = field(repr=False)
    length: int


def format_value(name, value, scope, *, existing_hash=False):
    """Build the bytes to store, without ever representing a plaintext password."""
    try:
        if name == "admin-password":
            if existing_hash:
                encoded = value.encode("ascii")
                _validate_hash(encoded)
                return SecretValue(encoded.strip() + b"\n", len(encoded.strip()))
            if not value:
                raise SecretError(name, "bad_format")
            from .admin import _encode_password_hash

            encoded = _encode_password_hash(value)
            return SecretValue(encoded, len(encoded.rstrip(b"\n")))
        _validate_text(name, value)
        encoded = (json.dumps({"scope": scope, "key": value})
                   if name == "model-key.json" else value)
        return SecretValue((encoded + "\n").encode("utf-8"), len(value))
    except UnicodeError:
        raise SecretError(name, "bad_format") from None


def legacy_declarations(data, environ):
    for var in (*LEGACY_ENV, *MODEL_ENV_OVERRIDES):
        if var in environ:
            yield var
    for section, key in LEGACY_TOML:
        table = data.get(section)
        if isinstance(table, dict) and key in table:
            yield f"{section}.{key}"


def _check_stat(path, name, platform, *, follow=False):
    from .config import _validate_admin_secret_stat

    try:
        _validate_admin_secret_stat(path.stat(follow_symlinks=follow),
                                    name=name, platform=platform)
    except FileNotFoundError:
        raise SecretError(name, "missing") from None
    except OSError:
        raise SecretError(name, "bad_mode") from None


def _read_secret(path, name, scope, platform, *, follow=False):
    _check_stat(path, name, platform, follow=follow)
    try:
        if name == "model-key.json":
            from agentfleet_common.ai_transport import ProviderError, read_key

            try:
                value = read_key(path, scope, environ={}, group_name="tglistener")
            except ProviderError:
                raise SecretError(name, "bad_format") from None
        elif name in ("router-key", "assistant-github-token"):
            from .config import _assistant_token

            value = _assistant_token(path, name=name, platform=platform)
            if value is None:  # It existed at stat time, so an empty file is invalid.
                raise SecretError(name, "bad_format")
        elif name == "admin-password":
            with path.open("rb") as stream:
                value = stream.read(1025)
            _validate_hash(value)
            return value.strip().decode("ascii")
        else:
            value = path.read_text(encoding="utf-8").rstrip("\r\n")
        return _validate_text(name, value)
    except FileNotFoundError:
        raise SecretError(name, "missing") from None
    except OSError:
        raise SecretError(name, "bad_mode") from None
    except UnicodeError:
        raise SecretError(name, "bad_format") from None


def _manifest_metadata(entry):
    if not isinstance(entry, dict):
        return None
    ts, actor = entry.get("updated_at"), entry.get("updated_by")
    length, source, expiry = entry.get("length"), entry.get("source"), entry.get("expires_at")
    if (not isinstance(ts, str) or not isinstance(actor, str) or not _ACTOR.fullmatch(actor)
            or type(length) is not int or length < 0 or source not in ("cli", "ui", "migrate")):
        return None
    try:
        stamp = datetime.fromisoformat(ts)
        if stamp.tzinfo is None:
            return None
        if expiry is not None:
            expiry = parse_expiry(expiry).isoformat()
        ts = stamp.astimezone(timezone.utc).isoformat(timespec="seconds")
    except (ValueError, OverflowError):
        return None
    return {
        "updated_at": ts,
        "updated_by": actor, "length": length, "source": source, "expires_at": expiry,
    }


@dataclass(frozen=True)
class _Revision:
    data: bytes = field(repr=False)
    metadata: dict | None


@dataclass(frozen=True)
class Snapshot:
    current: _Revision | None
    previous: _Revision | None


class SecretsStore:
    def __init__(self, paths, *, platform=None, now=None):
        self.paths = paths
        self.platform = PLATFORM if platform is None else platform
        self.now = now if now is not None else lambda: datetime.now(timezone.utc)

    def path(self, name):
        if name not in CATALOG:
            raise ValueError("unknown secret")
        return self.paths.dir / name

    def read(self, name, scope=None, *, required=True):
        try:
            return _read_secret(self.path(name), name, scope, self.platform)
        except SecretError as error:
            if error.kind == "missing" and not required:
                return None
            raise

    def _directory(self, previous=False):
        return self.paths.dir / ".prev" if previous else self.paths.dir

    def _manifest(self, previous=False, *, strict=False):
        target = self._directory(previous) / "manifest.json"
        try:
            _check_stat(target, "manifest.json", self.platform)
            value = json.loads(target.read_text(encoding="utf-8"))
            if not isinstance(value, dict):
                raise ValueError()
        except SecretError as error:
            if strict and error.kind != "missing":
                raise
            return {}
        except (OSError, ValueError, RecursionError):
            if strict:
                raise SecretError("manifest.json", "bad_format") from None
            return {}
        return {name: metadata for name in CATALOG
                if (metadata := _manifest_metadata(value.get(name))) is not None}

    def check_status_directory(self):
        # Stat of the directory itself does not require search permission inside it.
        try:
            os.stat(os.path.join(self.paths.dir, "."))
        except PermissionError:
            raise SecretError("secrets.dir", "unreadable") from None
        except (FileNotFoundError, NotADirectoryError):
            pass  # Status still reports missing/bad files individually.

    def status(self, required, scope=None, *, metadata_only=False, today=None):
        today = date.today() if today is None else today
        manifest = self._manifest()
        rows = []
        for name, description in CATALOG.items():
            entry = manifest.get(name, {})
            expires = entry.get("expires_at")
            expiry_status = None
            if expires:
                days = (parse_expiry(expires) - today).days
                expiry_status = "expired" if days < 0 else "expiring" if days <= 7 else None
            try:
                if metadata_only:
                    _check_stat(self.path(name), name, self.platform)
                else:
                    self.read(name, scope)
                state = expiry_status or "ok"
            except SecretError as error:
                state = error.kind
            rows.append({
                "name": name, "description": description, "required": name in required,
                "state": state, "status": STATUS_LABELS[state],
                "updated_at": entry.get("updated_at"), "updated_by": entry.get("updated_by"),
                "expires_at": expires, "expiry_state": expiry_status if state != "missing" else None,
                "command": f"sudo tg-triage secrets set {name} --restart",
            })
        return rows

    @contextmanager
    def writer(self):
        """Hold a single writer lock through the restart/rollback decision."""
        try:
            group = self.platform.group_id() if self.platform.unix else 0
        except (OSError, KeyError):
            raise SecretError("secrets.dir", "bad_owner") from None
        self._ensure_dir(self.paths.dir, group, 0o750)
        self._ensure_dir(self._directory(True), 0, 0o700)
        self._ensure_dir(self.paths.audit_log.parent, 0, 0o700)
        with self._private_fd(self.paths.dir / ".lock", "secrets.dir") as lock:
            if os.name == "posix":
                import fcntl

                fcntl.flock(lock, fcntl.LOCK_EX)
            with self._private_fd(self.paths.audit_log, "audit_log") as audit:
                yield audit

    def _ensure_dir(self, path, gid, mode):
        try:
            path.mkdir(mode=mode, parents=True)
        except FileExistsError:
            info = path.stat(follow_symlinks=False)
            if not stat.S_ISDIR(info.st_mode) or (self.platform.unix and info.st_mode & 0o022):
                raise SecretError("secrets directory", "bad_mode") from None
            if self.platform.unix and info.st_uid != 0:
                raise SecretError("secrets directory", "bad_owner") from None
        else:
            self.platform.permissions(path, gid, mode)

    @contextmanager
    def _private_fd(self, path, label):
        flags = os.O_WRONLY | os.O_APPEND | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(path, flags | os.O_CREAT | os.O_EXCL, 0o600)
            created = True
        except FileExistsError:
            if not stat.S_ISREG(path.stat(follow_symlinks=False).st_mode):
                raise SecretError(label, "bad_mode") from None
            fd = os.open(path, flags)
            created = False
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise SecretError(label, "bad_mode")
            if self.platform.unix and not created:
                if info.st_mode & 0o077:
                    raise SecretError(label, "bad_mode")
                if info.st_uid != 0:
                    raise SecretError(label, "bad_owner")
            if created:
                self.platform.permissions(path, 0, 0o600, fd=fd)
            yield fd
        finally:
            os.close(fd)

    def _atomic(self, path, content, *, private):
        gid = self.platform.group_id() if self.platform.unix and not private else 0
        handle, name = tempfile.mkstemp(prefix=".tmp-", dir=path.parent)
        temporary = Path(name)
        try:
            with os.fdopen(handle, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
                self.platform.permissions(temporary, gid, 0o600 if private else 0o640,
                                          fd=stream.fileno())
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            self._sync_dir(path.parent)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _sync_dir(path):
        if os.name == "posix" and hasattr(os, "O_DIRECTORY"):
            fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)

    def _revision(self, name, previous):
        path = self._directory(previous) / self.path(name).name
        try:
            if not stat.S_ISREG(path.stat(follow_symlinks=False).st_mode):
                raise SecretError(name, "bad_mode")
            content = path.read_bytes()
        except FileNotFoundError:
            return None
        return _Revision(content, self._manifest(previous, strict=True).get(name))

    def _save_revision(self, name, revision, previous):
        manifest = self._manifest(previous, strict=True)
        directory = self._directory(previous)
        path = directory / name
        if revision is None:
            if path.exists():
                path.unlink()
                self._sync_dir(directory)
        else:
            self._atomic(path, revision.data, private=previous)
        if revision is None or revision.metadata is None:
            if name not in manifest:
                return
            del manifest[name]
        else:
            manifest[name] = revision.metadata
        self._atomic(directory / "manifest.json",
                     (json.dumps(manifest, sort_keys=True) + "\n").encode("utf-8"),
                     private=previous)

    def restore(self, name, snapshot):
        self._save_revision(name, snapshot.current, False)
        self._save_revision(name, snapshot.previous, True)

    def _replace(self, name, current, previous, snapshot):
        try:
            self._save_revision(name, previous, True)
            self._save_revision(name, current, False)
        except (OSError, SecretError):
            self.restore(name, snapshot)
            raise
        return snapshot

    def set(self, name, value, *, actor, source="cli", expires_at=None):
        snapshot = Snapshot(self._revision(name, False), self._revision(name, True))
        metadata = {"updated_at": self._timestamp(), "updated_by": actor, "length": value.length,
                    "source": source, "expires_at": expires_at}
        return self._replace(name, _Revision(value.data, metadata), snapshot.current, snapshot)

    def rollback(self, name, *, actor):
        snapshot = Snapshot(self._revision(name, False), self._revision(name, True))
        previous = snapshot.previous
        if previous is None:
            raise SecretError(name, "missing")
        if name in GITHUB_TOKENS:
            value = _read_secret(self._directory(True) / name, name, None, self.platform)
            self.check_distinct_github(name, value)
        metadata = previous.metadata or {}
        if "length" in metadata:
            length = metadata["length"]
        else:
            try:
                value = previous.data.decode("utf-8").rstrip("\r\n")
                if name == "model-key.json":
                    value = json.loads(value)["key"]
                length = len(value)
            except (KeyError, ValueError, TypeError, RecursionError):
                raise SecretError(name, "bad_format") from None
        restored = _Revision(previous.data, {
            "updated_at": self._timestamp(), "updated_by": actor, "length": length,
            "source": "cli", "expires_at": metadata.get("expires_at"),
        })
        return self._replace(name, restored, snapshot.current, snapshot), length

    def audit(self, fd, *, action, name, actor, source, length, restart_result, error_type=None):
        entry = {"ts": self._timestamp(), "action": action, "name": name, "actor": actor,
                 "source": source, "length": length, "restart_result": restart_result}
        if error_type is not None:
            entry["error_type"] = error_type
        line = (json.dumps(entry, sort_keys=True) + "\n").encode("utf-8")
        if os.write(fd, line) != len(line):
            raise OSError()
        os.fsync(fd)

    def _timestamp(self):
        return self.now().astimezone(timezone.utc).isoformat(timespec="seconds")

    def check_distinct_github(self, name, value):
        if name in GITHUB_TOKENS:
            other = next(token for token in GITHUB_TOKENS if token != name)
            if self.read(other, required=False) == value:
                raise SecretError(name, "bad_format")


def sudo_actor(environ):
    actor = environ.get("SUDO_USER", "")
    return actor if _ACTOR.fullmatch(actor) else "root"


def _legacy_env(path, names):
    """Parse only selected literal assignments. None marks the last assignment as invalid."""
    selected = {var.encode("ascii"): var for var in names}
    if not selected:
        return {}
    values = {}
    try:
        with path.open("rb") as stream:
            for number, line in enumerate(stream):
                if number == 0:
                    line = line.removeprefix(b"\xef\xbb\xbf")
                var, equals, raw = line.partition(b"=")
                name = selected.get(var.strip())
                if not equals or name is None:
                    continue
                try:
                    parts = shlex.split(raw.decode("utf-8"))
                    values[name] = None if len(parts) > 1 else parts[0] if parts else ""
                except ValueError:
                    values[name] = None
    except FileNotFoundError:
        return {}
    except PermissionError:
        raise SecretError("env-file", "unreadable") from None
    except OSError:
        raise SecretError("env-file", "bad_mode") from None
    return values


def service_environment(path, environ):
    """Pick the process value first, then fill only missing non-secret settings."""
    values = {var: environ[var] for var in MODE_ENV if var in environ}
    for var, value in _legacy_env(path, (var for var in MODE_ENV if var not in values)).items():
        if value is None:
            raise SecretError(var, "bad_format")
        values[var] = value
    return values


@dataclass(frozen=True)
class MigrationItem:
    name: str
    value: SecretValue


def migration_plan(data, env_file, store, scope, *, required):
    """Return copies, cleanup suggestions and safe warnings for skipped optional sources."""
    items, cleanup, skipped = {}, [], []

    def handle(error):
        if error.name in required:
            raise error from None
        skipped.append((error.name, error.kind))

    for var, value in _legacy_env(env_file, LEGACY_ENV).items():
        name = LEGACY_ENV[var]
        try:
            items[name] = MigrationItem(name, format_value(name, value, scope))
        except SecretError as error:
            handle(error)
        else:
            cleanup.append(var)
    for (section, key), name in LEGACY_TOML.items():
        table = data.get(section)
        if not isinstance(table, dict) or key not in table or table[key] == "":
            continue
        try:
            raw = table[key]
            if not isinstance(raw, str) or any(unicodedata.category(c) == "Cc" for c in raw):
                raise SecretError(name, "bad_format")
            path = Path(raw)
            if not path.is_absolute():
                raise SecretError(name, "bad_format")
            value = _read_secret(path, name, scope, store.platform, follow=True)
            prepared = format_value(name, value, scope, existing_hash=name == "admin-password")
            if not path.resolve().is_relative_to(store.paths.dir.resolve()):
                cleanup.append(str(path))
            items[name] = MigrationItem(name, prepared)
        except SecretError as error:
            handle(error)
        except (OSError, RuntimeError):
            handle(SecretError(name, "bad_mode"))
    return [items[name] for name in CATALOG if name in items], cleanup, skipped


def apply_migration(store, items, audit, *, actor, scope):
    """Do not overwrite a key already installed by a previous migration or rotation."""
    to_write = []
    planned_tokens = {}
    for item in items:
        if store.read(item.name, scope, required=False) is not None:
            continue
        if item.name in GITHUB_TOKENS:
            value = item.value.data.decode("utf-8").rstrip("\n")
            store.check_distinct_github(item.name, value)
            if value in planned_tokens.values():
                raise SecretError(item.name, "bad_format")
            planned_tokens[item.name] = value
        to_write.append(item)
    for item in to_write:
        snapshot = store.set(item.name, item.value, actor=actor, source="migrate")
        try:
            store.audit(audit, action="migrate", name=item.name, actor=actor,
                        source="migrate", length=item.value.length, restart_result="not_requested")
        except OSError:
            store.restore(item.name, snapshot)
            raise

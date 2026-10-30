"""Directory and process safeguards with injectable OS primitives."""

import importlib
import errno
import os
import stat
from pathlib import Path

from .config import ConfigError


class AlreadyRunning(RuntimeError):
    """The non-blocking service lock is held by another process."""


def validate_data_directories(config, *, stat_fn=None, uid_fn=None):
    if os.name != "posix" and stat_fn is None:
        return
    stat_fn = Path.stat if stat_fn is None else stat_fn
    uid_fn = getattr(os, "getuid", None) if uid_fn is None else uid_fn
    for value in (config.telegram.session_path, config.db_path):
        try:
            info = stat_fn(Path(value).parent)
        except OSError:
            raise ConfigError("Data directory must exist with mode 0700") from None
        if not stat.S_ISDIR(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o700:
            raise ConfigError("Data directory must have mode 0700")
        if uid_fn is not None and info.st_uid != uid_fn():
            raise ConfigError("Data directory owner must be the current user")


class ProcessLock:
    def __init__(self, directory, *, lock_module=None):
        self.directory = Path(directory)
        self.module = lock_module
        self.stream = None
        self._windows = False

    def __enter__(self):
        if self.module is None:
            self._windows = os.name == "nt"
            self.module = importlib.import_module("msvcrt" if self._windows else "fcntl")
        self.stream = (self.directory / "tg-triage.lock").open("a+b")
        try:
            os.chmod(self.stream.name, 0o600)
            try:
                if self._windows:
                    self.stream.seek(0, os.SEEK_END)
                    if self.stream.tell() == 0:
                        self.stream.write(b"\0")
                        self.stream.flush()
                    self.stream.seek(0)
                    self.module.locking(self.stream.fileno(), self.module.LK_NBLCK, 1)
                else:
                    self.module.flock(self.stream.fileno(), self.module.LOCK_EX | self.module.LOCK_NB)
            except OSError as error:
                if isinstance(error, BlockingIOError) or error.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                    raise AlreadyRunning() from None
                raise
        except BaseException:
            self.stream.close()
            self.stream = None
            raise
        return self

    def __exit__(self, *args):
        if self.stream is not None:
            try:
                if self._windows:
                    self.stream.seek(0)
                    self.module.locking(self.stream.fileno(), self.module.LK_UNLCK, 1)
                else:
                    self.module.flock(self.stream.fileno(), self.module.LOCK_UN)
            finally:
                self.stream.close()
                self.stream = None

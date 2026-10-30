"""Shared Ollama lock protocol. Keep both service copies byte-identical.

Foreground announces before waiting. Background rechecks live announcements after
nonblocking acquisition, so a queued foreground call cannot be overtaken.
"""
import errno
import os
import stat
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

LOCK_DIR_ENV = 'AGENTFLEET_LOCK_DIR'
DEFAULT_LOCK_DIR = '/run/agentfleet'
LOCK_FILE = 'ollama.lock'
FOREGROUND_PREFIX = 'ollama.foreground.'
BACKGROUND_POLL_SECONDS = 2
_PROCESS_LOCK = threading.Lock()
_COUNT_LOCK = threading.Lock()
_FOREGROUND_COUNT = 0


class LockUnavailable(OSError):
    """I/O failure in the lock protocol, distinct from the HTTP operation."""


class LockStopped(RuntimeError):
    """Shutdown requested while waiting for the model lock."""


def _after_fork():
    global _PROCESS_LOCK, _COUNT_LOCK, _FOREGROUND_COUNT
    _PROCESS_LOCK = threading.Lock()
    _COUNT_LOCK = threading.Lock()
    _FOREGROUND_COUNT = 0


if hasattr(os, 'register_at_fork'):
    os.register_at_fork(after_in_child=_after_fork)


def pid_alive(pid):
    if pid <= 0:
        return False
    if os.name == 'nt':
        # os.kill(pid, 0) can terminate a Windows process: never use it here.
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return ctypes.get_last_error() != 87  # Access denied is conservatively live.
        try:
            code = wintypes.DWORD()
            return not kernel.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value == 259
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def live_foreground(directory):
    live = False
    for marker in directory.glob(FOREGROUND_PREFIX + '*'):
        try:
            suffix = marker.name[len(FOREGROUND_PREFIX):]
            pid_text, random = suffix.split('.', 1)
            pid = int(pid_text)
            if not random or pid <= 0:
                continue
            if pid_alive(pid):
                live = True
            else:
                marker.unlink(missing_ok=True)
        except FileNotFoundError:
            pass
        except ValueError:
            # Unknown filenames cannot prove a live foreground caller.
            continue
    return live


def _open_lock(directory):
    flags = os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0)
    created = False
    try:
        fd = os.open(directory / LOCK_FILE, flags | os.O_CREAT | os.O_EXCL, 0o660)
        created = True
    except FileExistsError:
        fd = os.open(directory / LOCK_FILE, flags)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError('Model lock must be a regular file')
        if created and os.name == 'posix':
            os.fchmod(fd, 0o660)  # Both service users share agentfleet despite UMask=0077.
        if os.fstat(fd).st_size == 0:
            os.write(fd, b'\0')  # Windows locks exactly byte zero.
        return fd
    except BaseException:
        os.close(fd)
        raise


def _lock(fd, *, blocking):
    if os.name == 'nt':
        import msvcrt
        os.lseek(fd, 0, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError as error:
            if error.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                return False
            raise
        return True
    import fcntl
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
    except BlockingIOError:
        return False
    return True


def _unlock(fd):
    if os.name == 'nt':
        import msvcrt
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_UN)


def _wait(stop, seconds):
    if stop is None:
        time.sleep(seconds)
    elif stop.wait(seconds):
        raise LockStopped()


class ModelLock:
    def __init__(self, directory=None, *, poll_seconds=BACKGROUND_POLL_SECONDS):
        if poll_seconds <= 0:
            raise ValueError("Poll interval must be positive")
        self.directory = directory
        self.poll_seconds = poll_seconds

    @property
    def _foreground(self):
        # Retains the private diagnostic used by the original thread-priority test.
        return _FOREGROUND_COUNT

    @contextmanager
    def enter(self, *, background=False, stop=None):
        manager = self._enter(background=background, stop=stop)
        try:
            manager.__enter__()
        except OSError as error:
            raise LockUnavailable('Model lock I/O unavailable') from error
        try:
            yield
        except BaseException:
            try:
                manager.__exit__(*sys.exc_info())
            except OSError as error:
                raise LockUnavailable('Model lock I/O unavailable') from error
            raise
        else:
            try:
                manager.__exit__(None, None, None)
            except OSError as error:
                raise LockUnavailable('Model lock I/O unavailable') from error

    @contextmanager
    def _enter(self, *, background=False, stop=None):
        global _FOREGROUND_COUNT
        directory = Path(self.directory or os.environ.get(LOCK_DIR_ENV, DEFAULT_LOCK_DIR))
        directory.mkdir(parents=True, exist_ok=True, mode=0o2775)
        marker = None
        fd = None
        local = locked = counted = False
        try:
            if not background:
                with _COUNT_LOCK:
                    _FOREGROUND_COUNT += 1
                    counted = True
                marker = directory / (FOREGROUND_PREFIX + str(os.getpid()) + '.' + uuid.uuid4().hex)
                marker.touch(exist_ok=False)
                if stop is None:
                    _PROCESS_LOCK.acquire()
                else:
                    while not _PROCESS_LOCK.acquire(timeout=0.05):
                        _wait(stop, 0.05)
                    if stop.is_set():
                        _PROCESS_LOCK.release()
                        raise LockStopped()
                local = True
                fd = _open_lock(directory)
                while not _lock(fd, blocking=stop is None):
                    _wait(stop, 0.05)
                locked = True
                marker.unlink(missing_ok=True)
                with _COUNT_LOCK:
                    _FOREGROUND_COUNT -= 1
                    counted = False
            else:
                while True:
                    if stop is not None and stop.is_set():
                        raise LockStopped()
                    if not live_foreground(directory) and _PROCESS_LOCK.acquire(blocking=False):
                        local = True
                        fd = _open_lock(directory)
                        locked = _lock(fd, blocking=False)
                        if locked and not live_foreground(directory):
                            break
                        if locked:
                            _unlock(fd)
                            locked = False
                        os.close(fd)
                        fd = None
                        _PROCESS_LOCK.release()
                        local = False
                    _wait(stop, self.poll_seconds)
            yield
        finally:
            try:
                if marker is not None:
                    marker.unlink(missing_ok=True)
            finally:
                if counted:
                    with _COUNT_LOCK:
                        _FOREGROUND_COUNT -= 1
                try:
                    if locked:
                        _unlock(fd)
                finally:
                    if fd is not None:
                        os.close(fd)
                    if local:
                        _PROCESS_LOCK.release()


MODEL_LOCK = ModelLock()

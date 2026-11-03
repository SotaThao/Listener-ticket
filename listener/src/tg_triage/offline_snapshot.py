"""Private immutable inspection snapshots of an explicitly supplied offline copy."""
from contextlib import contextmanager
import hashlib
import sqlite3
import tempfile
import time
from pathlib import Path
from .rules_pending_store import RulesHalt


def _digest(path):
    value=hashlib.sha256();used=0;deadline=time.monotonic()+15
    with path.open('rb') as stream:
        for block in iter(lambda:stream.read(1024*1024),b''):
            used+=len(block)
            if used>512*1024*1024 or time.monotonic()>deadline:raise RulesHalt('SNAPSHOT_READ_LIMIT')
            value.update(block)
    return value.hexdigest()


def _identity(path):
    row=path.stat()
    return row.st_dev,row.st_ino,row.st_size,row.st_mtime_ns


def _safe_input(path):
    if not path.is_file() or any(p.is_symlink() or getattr(p,'is_junction',lambda:False)() for p in (path,*path.parents)):
        raise RulesHalt('SNAPSHOT_UNSAFE_PATH')
    if any(Path(str(path)+suffix).exists() or Path(str(path)+suffix).is_symlink() for suffix in ('-wal','-shm','-journal')):
        raise RulesHalt('SNAPSHOT_SIDECAR_PRESENT')


@contextmanager
def isolated_snapshot(path):
    """Never open the supplied file with SQLite; immutable applies only to our private clone."""
    path=Path(path);connection=None
    try:
        _safe_input(path);identity=_identity(path)
        if identity[2]>512*1024*1024:raise RulesHalt('SNAPSHOT_SIZE_LIMIT')
        before=_digest(path)
        with tempfile.TemporaryDirectory(prefix='triage-offline-inspect-') as directory:
            root=Path(directory);root.chmod(0o700);copy=root/'snapshot.sqlite3'
            used=0;deadline=time.monotonic()+15
            with path.open('rb') as source,copy.open('xb') as target:
                for block in iter(lambda:source.read(1024*1024),b''):
                    used+=len(block)
                    if used>512*1024*1024 or time.monotonic()>deadline:raise RulesHalt('SNAPSHOT_READ_LIMIT')
                    target.write(block)
            copy.chmod(0o600)
            _safe_input(path)
            if _identity(path)!=identity or _digest(path)!=before or _digest(copy)!=before:
                raise RulesHalt('SNAPSHOT_CHANGED')
            connection=sqlite3.connect(copy.resolve().as_uri()+'?mode=ro&immutable=1',uri=True,timeout=0,isolation_level=None)
            connection.execute('PRAGMA query_only=ON');connection.execute('BEGIN')
            deadline=time.monotonic()+15
            connection.set_progress_handler(lambda:int(time.monotonic()>deadline),10000)
            try:
                if connection.execute('PRAGMA quick_check(1)').fetchone()!=('ok',):raise RulesHalt('SNAPSHOT_INTEGRITY_UNKNOWN')
                yield connection,before
            finally:
                connection.close();connection=None
                _safe_input(path)
                if _identity(path)!=identity or _digest(path)!=before:raise RulesHalt('SNAPSHOT_CHANGED')
    except (sqlite3.Error,OSError,ValueError):
        raise RulesHalt('SNAPSHOT_UNREADABLE_OR_LIMIT') from None
    finally:
        if connection is not None:connection.close()

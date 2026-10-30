"""Exercise the real OS lock across processes, including abnormal exit."""
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tg_triage.runtime import AlreadyRunning, ProcessLock


CHILD = r'''
import sys
from tg_triage.runtime import AlreadyRunning, ProcessLock
try:
    with ProcessLock(sys.argv[1]):
        print('acquired', flush=True)
        if len(sys.argv) > 2:
            sys.stdin.readline()
except AlreadyRunning:
    print('blocked', flush=True)
    sys.exit(3)
'''


class ProcessLockTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = self.temp.name

    def attempt(self):
        return subprocess.run([sys.executable, '-c', CHILD, self.directory], capture_output=True, text=True, timeout=10)

    def test_other_process_is_blocked_then_acquires_after_release(self):
        with ProcessLock(self.directory):
            result = self.attempt()
            self.assertEqual((result.returncode, result.stdout.strip()), (3, 'blocked'))
        result = self.attempt()
        self.assertEqual((result.returncode, result.stdout.strip()), (0, 'acquired'))

    def test_abnormal_exit_releases_lock(self):
        child = subprocess.Popen([sys.executable, '-c', CHILD, self.directory, 'hold'], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            self.assertEqual(child.stdout.readline().strip(), 'acquired')
            with self.assertRaises(AlreadyRunning):
                with ProcessLock(self.directory): pass
            child.kill()
            child.wait(timeout=10)
            with ProcessLock(self.directory): pass
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=10)
            for stream in (child.stdin, child.stdout, child.stderr):
                if stream: stream.close()

    def test_missing_platform_lock_module_is_not_silently_ignored(self):
        with patch('tg_triage.runtime.importlib.import_module', side_effect=ImportError('unavailable')):
            with self.assertRaises(ImportError):
                with ProcessLock(self.directory): pass
        self.assertFalse((Path(self.directory) / 'tg-triage.lock').exists())

"""Real OS locks and spawned processes; no network/model service needed."""
import importlib.util
import multiprocessing
import os
import tempfile
import threading
import time
import unittest
import queue
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

from tg_triage import model_lock as locks

ROOT = Path(__file__).resolve().parents[2]
OWN = ROOT / 'listener/src/tg_triage/model_lock.py'
OTHER = ROOT / 'nailhub/src/nailhub/model_lock.py'


def child_call(module_path, directory, role, started, results, release, kernel_reached=None, foreground_allowed=None):
    spec = importlib.util.spec_from_file_location('child_model_lock', module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if role == 'background' and os.environ.get('AGENTFLEET_TEST_IGNORE_MARKERS') == '1':
        module.live_foreground = lambda directory: False  # Deliberate behavioral mutation, test-only child.
    if kernel_reached is not None:
        real_lock = module._lock
        def delayed_lock(fd, *, blocking):
            kernel_reached.set()
            if not foreground_allowed.wait(10):
                raise RuntimeError('Foreground test gate timed out')
            return real_lock(fd, blocking=blocking)
        module._lock = delayed_lock
    started.set()
    with module.ModelLock(directory, poll_seconds=0.01).enter(background=role == 'background'):
        results.put(role)
        if release is not None:
            release.wait(10)


class ModelLockTests(unittest.TestCase):
    def test_copies_are_byte_identical(self):
        if OTHER.is_file():
            self.assertEqual(OWN.read_bytes(), OTHER.read_bytes())

    def test_foreground_waiter_precedes_background_started_earlier(self):
        ctx = multiprocessing.get_context('spawn')
        with tempfile.TemporaryDirectory() as directory:
            results=ctx.Queue();bg_started=ctx.Event();fg_started=ctx.Event();fg_release=ctx.Event()
            kernel_reached=ctx.Event();foreground_allowed=ctx.Event()
            bg=ctx.Process(target=child_call,args=(str(OTHER if OTHER.exists() else OWN),directory,'background',bg_started,results,None))
            fg=ctx.Process(target=child_call,args=(str(OWN),directory,'foreground',fg_started,results,fg_release,kernel_reached,foreground_allowed))
            try:
                with locks.ModelLock(directory).enter():
                    bg.start();self.assertTrue(bg_started.wait(5))
                    fg.start();self.assertTrue(fg_started.wait(5));self.assertTrue(kernel_reached.wait(5))
                    self.assertTrue(list(Path(directory).glob(locks.FOREGROUND_PREFIX+'*')))
                # File lock is FREE but a live foreground announcement is queued.
                # Background polls every 10ms. Ignoring markers MUST acquire here and fail.
                with self.assertRaises(queue.Empty):results.get(timeout=0.2)
                foreground_allowed.set()
                self.assertEqual(results.get(timeout=5),'foreground')
                fg_release.set()
                self.assertEqual(results.get(timeout=5),'background')
                for process in (fg,bg):
                    process.join(5);self.assertEqual(process.exitcode,0)
                self.assertFalse(list(Path(directory).glob(locks.FOREGROUND_PREFIX+'*')))
            finally:
                foreground_allowed.set();fg_release.set()
                for process in (fg,bg):
                    if process.pid is not None:
                        if process.is_alive():process.terminate()
                        process.join(5);process.close()
                results.close();results.join_thread()

    def test_priority_mutation_is_killed(self):
        env=os.environ.copy();env['AGENTFLEET_TEST_IGNORE_MARKERS']='1';env['PYTHONPATH']='src'
        result=subprocess.run([sys.executable,'-m','unittest','tests.test_model_lock.ModelLockTests.test_foreground_waiter_precedes_background_started_earlier'],
                              cwd=Path(__file__).resolve().parents[1],env=env,capture_output=True,text=True,timeout=20)
        self.assertNotEqual(result.returncode,0)
        self.assertIn('Empty not raised',result.stderr)

    def test_background_releases_and_retries_if_marker_appears_after_acquiring(self):
        with tempfile.TemporaryDirectory() as directory:
            marker=Path(directory)/(locks.FOREGROUND_PREFIX+str(os.getpid())+'.race')
            real_lock=locks._lock;announced=False;waits=[]
            def acquire_then_announce(fd,*,blocking):
                nonlocal announced
                acquired=real_lock(fd,blocking=blocking)
                if acquired and not announced:
                    marker.touch();announced=True
                return acquired
            def observe_backoff(stop,seconds):
                waits.append(seconds)
                self.assertTrue(marker.exists())
                # Both the real file lock and process-local lock must be released
                # before waiting; foreground now has a chance to run.
                self.assertTrue(locks._PROCESS_LOCK.acquire(blocking=False))
                locks._PROCESS_LOCK.release()
                fd=locks._open_lock(Path(directory))
                try:
                    acquired=real_lock(fd,blocking=False)
                    self.assertTrue(acquired)
                    if acquired:locks._unlock(fd)
                finally:os.close(fd)
                marker.unlink()
            try:
                with patch.object(locks,'_lock',side_effect=acquire_then_announce),patch.object(locks,'_wait',side_effect=observe_backoff):
                    with locks.ModelLock(directory,poll_seconds=0.01).enter(background=True):
                        self.assertEqual(waits,[0.01])
                        self.assertFalse(marker.exists())
            finally:marker.unlink(missing_ok=True)

    def test_stale_marker_cleanup_and_live_marker_kept(self):
        with tempfile.TemporaryDirectory() as directory:
            stale=Path(directory)/(locks.FOREGROUND_PREFIX+'99999999.dead')
            live=Path(directory)/(locks.FOREGROUND_PREFIX+str(os.getpid())+'.live')
            stale.touch();live.touch()
            self.assertTrue(locks.live_foreground(Path(directory)))
            self.assertFalse(stale.exists());self.assertTrue(live.exists())
            live.unlink()
            with locks.ModelLock(directory).enter(background=True):pass

    def test_marker_removed_on_acquire_exception(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(locks,'_open_lock',side_effect=OSError('injected')):
                with self.assertRaises(OSError):
                    with locks.ModelLock(directory).enter():pass
            self.assertFalse(list(Path(directory).glob(locks.FOREGROUND_PREFIX+'*')))
            self.assertEqual(locks.MODEL_LOCK._foreground,0)
            with locks.ModelLock(directory).enter():pass

    def test_exception_releases_file_lock_to_another_process(self):
        ctx=multiprocessing.get_context('spawn')
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                with locks.ModelLock(directory).enter():raise ValueError('injected')
            results=ctx.Queue();started=ctx.Event()
            p=ctx.Process(target=child_call,args=(str(OWN),directory,'background',started,results,None))
            try:
                p.start();self.assertEqual(results.get(timeout=8),'background')
                p.join(5);self.assertEqual(p.exitcode,0)
            finally:
                if p.is_alive():p.terminate()
                p.join(5);p.close();results.close();results.join_thread()
            self.assertFalse(list(Path(directory).glob(locks.FOREGROUND_PREFIX+'*')))

    def test_background_stop_interrupts_wait(self):
        with tempfile.TemporaryDirectory() as directory:
            marker=Path(directory)/(locks.FOREGROUND_PREFIX+str(os.getpid())+'.live');marker.touch()
            stop=threading.Event();timer=threading.Timer(0.03,stop.set);timer.start()
            try:
                started=time.monotonic()
                with self.assertRaises(locks.LockStopped):
                    with locks.ModelLock(directory).enter(background=True,stop=stop):pass
                self.assertLess(time.monotonic()-started,1)
            finally:timer.cancel();marker.unlink()

    def test_separate_instances_still_serialize_threads(self):
        with tempfile.TemporaryDirectory() as directory:
            active=0;maximum=0;guard=threading.Lock()
            def call():
                nonlocal active,maximum
                with locks.ModelLock(directory).enter():
                    with guard:active+=1;maximum=max(maximum,active)
                    time.sleep(0.01)
                    with guard:active-=1
            threads=[threading.Thread(target=call) for _ in range(5)]
            for t in threads:t.start()
            for t in threads:t.join(5);self.assertFalse(t.is_alive())
            self.assertEqual(maximum,1)

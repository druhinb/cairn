"""File locks that two descriptors, in one process or two, contend for."""
import os
import tempfile
import unittest
from pathlib import Path

from cairn.core import locks


class LocksTest(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / "held.pid"

    def _open(self, flags=os.O_CREAT | os.O_RDWR):
        fd = locks.open_file(self.path, flags)
        self.addCleanup(os.close, fd)
        return fd

    def test_an_exclusive_lock_turns_away_every_other_holder(self):
        holder, other = self._open(), self._open()
        self.assertTrue(locks.acquire(holder))
        self.assertFalse(locks.acquire(other))
        self.assertFalse(locks.acquire(other, shared=True))
        locks.release(holder)
        self.assertTrue(locks.acquire(other))

    def test_shared_locks_let_each_other_in_and_keep_an_exclusive_one_out(self):
        first, second, other = self._open(), self._open(), self._open()
        self.assertTrue(locks.acquire(first, shared=True, wait=True))
        self.assertTrue(locks.acquire(second, shared=True))
        self.assertFalse(locks.acquire(other))
        locks.release(first)
        locks.release(second)
        self.assertTrue(locks.acquire(other))

    def test_the_holders_pid_reads_back_while_it_holds_the_lock(self):
        holder = self._open()
        locks.acquire(holder)
        locks.write_pid(holder)
        reader = self._open(os.O_RDONLY)
        self.assertEqual(locks.read_pid(reader), os.getpid())

    def test_an_empty_file_names_no_pid(self):
        self.assertIsNone(locks.read_pid(self._open()))

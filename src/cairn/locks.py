"""Whole-file locks the operating system drops when their holder dies, over a small
file that names the holder's pid.

flock on macOS and Linux; LockFileEx on Windows, which locks byte ranges and blocks
reads of a locked range, so it locks one byte far past the pid.
"""
import os
import sys

if sys.platform == "win32":
    import ctypes
    import msvcrt
    from ctypes import wintypes

    class _Overlapped(ctypes.Structure):
        _fields_ = [("internal", ctypes.c_void_p), ("internal_high", ctypes.c_void_p),
                    ("offset", wintypes.DWORD), ("offset_high", wintypes.DWORD),
                    ("event", wintypes.HANDLE)]

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.LockFileEx.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
                                     wintypes.DWORD, wintypes.DWORD,
                                     ctypes.POINTER(_Overlapped)]
    _kernel32.UnlockFileEx.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
                                       wintypes.DWORD, ctypes.POINTER(_Overlapped)]
    _kernel32.LockFileEx.restype = _kernel32.UnlockFileEx.restype = wintypes.BOOL
    _FAIL_IMMEDIATELY, _EXCLUSIVE = 0x1, 0x2
    _LOCK_VIOLATION, _IO_PENDING = 33, 997
else:
    import fcntl

PID_BYTES = 32


def open_file(path, flags, mode=0o644):
    """A descriptor for path, binary on Windows, where os.open defaults to text."""
    return os.open(path, flags | getattr(os, "O_BINARY", 0), mode)


def acquire(fd, shared=False, wait=False):
    """Lock fd, shared or exclusive. Returns False when another holder has it and
    wait is False."""
    if sys.platform == "win32":
        return _lock_file_ex(fd, shared, wait)
    try:
        fcntl.flock(fd, (fcntl.LOCK_SH if shared else fcntl.LOCK_EX)
                    | (0 if wait else fcntl.LOCK_NB))
    except BlockingIOError:
        return False
    return True


def release(fd):
    if sys.platform == "win32":
        handle = msvcrt.get_osfhandle(fd)
        if not _kernel32.UnlockFileEx(handle, 0, 1, 0, ctypes.byref(_far_byte())):
            raise ctypes.WinError(ctypes.get_last_error())
    else:
        fcntl.flock(fd, fcntl.LOCK_UN)


def write_pid(fd):
    os.ftruncate(fd, 0)
    os.lseek(fd, 0, os.SEEK_SET)
    os.write(fd, str(os.getpid()).encode())


def read_pid(fd):
    """The pid written in fd, or None when it names none yet."""
    os.lseek(fd, 0, os.SEEK_SET)
    try:
        return int(os.read(fd, PID_BYTES).decode().strip())
    except ValueError:
        return None


def _far_byte():
    return _Overlapped(offset=0, offset_high=1)  # the byte at 4 GiB


def _lock_file_ex(fd, shared, wait):
    flags = (0 if shared else _EXCLUSIVE) | (0 if wait else _FAIL_IMMEDIATELY)
    handle = msvcrt.get_osfhandle(fd)
    if _kernel32.LockFileEx(handle, flags, 0, 1, 0, ctypes.byref(_far_byte())):
        return True
    error = ctypes.get_last_error()
    if error in (_LOCK_VIOLATION, _IO_PENDING) and not wait:
        return False
    raise ctypes.WinError(error)

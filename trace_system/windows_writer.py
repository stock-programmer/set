"""Coordinate Windows SDK writers using an automatically released kernel mutex."""
from contextlib import contextmanager
from functools import lru_cache


@lru_cache(maxsize=1)
def _kernel():
    import ctypes
    from ctypes import wintypes
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
    api.CreateMutexW.restype = wintypes.HANDLE
    api.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    api.WaitForSingleObject.restype = wintypes.DWORD
    for fn in (api.ReleaseMutex, api.CloseHandle):
        fn.argtypes = [wintypes.HANDLE]
        fn.restype = wintypes.BOOL
    return api


@contextmanager
def writer_mutex(name):
    from .core import TraceError
    api = _kernel()
    handle = api.CreateMutexW(None, False, name)
    if not handle:
        raise TraceError("Cannot open the Trace writer mutex.")
    try:
        wait = api.WaitForSingleObject(handle, 30000)
        # WAIT_ABANDONED grants ownership after another process exits. SQLite
        # is still responsible for transaction recovery and all hash checks.
        if wait not in (0, 0x80):
            raise TraceError("Cannot acquire the Trace writer mutex (wait=%d)." % wait)
        try:
            yield
        finally:
            if not api.ReleaseMutex(handle):
                raise TraceError("Cannot release the Trace writer mutex.")
    finally:
        api.CloseHandle(handle)

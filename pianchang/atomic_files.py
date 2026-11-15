"""Traced atomic file replacement with bounded Windows sharing-lock recovery."""
from __future__ import annotations

from os import replace as _replace_file
from pathlib import Path
from time import sleep as _sleep

from trace_system import event, traced


@traced("file.atomic_replace", kind="tool", capture_inputs=False)
def replace_with_retry(temporary, target, *, replace_file=None, sleep=None):
    replace_file = replace_file or _replace_file
    sleep = sleep or _sleep
    delays = (.05, .1, .2, .4, .8, 1.0)
    for attempt in range(len(delays) + 1):
        event("file.atomic_replace.request", {"temporary": str(temporary), "target": str(target),
              "attempt": attempt + 1}, kind="filesystem")
        try:
            replace_file(temporary, target)
        except PermissionError as exc:
            code = getattr(exc, "winerror", None)
            retry = code in {5, 32, 33} and attempt < len(delays)
            event("file.atomic_replace.error", {"temporary": str(temporary), "target": str(target),
                  "attempt": attempt + 1, "winerror": code, "errno": exc.errno,
                  "retry_local_replace": retry, "network_request": False,
                  "temporary_preserved": Path(temporary).exists()}, kind="error")
            if not retry:
                raise
            sleep(delays[attempt])
        else:
            result = {"replaced": True, "target": str(target), "attempts": attempt + 1}
            event("file.atomic_replace.response", result, kind="filesystem")
            return result

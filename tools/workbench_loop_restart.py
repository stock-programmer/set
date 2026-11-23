"""Restart the sidecar's original local workbench and reconcile slow startup receipts."""
import ctypes
import hashlib
import http.client
import json
import os
import re
import socket
import subprocess
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from trace_system import TraceError, event, get_tracer


def _manifest(web, url):
    manifest = json.loads((web / "server-process.json").read_text(encoding="utf-8-sig"))
    if manifest["url"] != url or int(manifest["pid"]) <= 0 or not manifest["started_at"]:
        raise RuntimeError("工作台进程记录已变化")
    return manifest


def workbench_http_identity(port):
    path = "/agent-chat.css"
    event("workbench_loop.restart.probe.request", {"method": "HEAD", "port": port, "path": path}, kind="http")
    client = http.client.HTTPConnection("127.0.0.1", port, timeout=60)
    try:
        client.request("HEAD", path)
        response = client.getresponse()
        response.read()
        trace_id = response.getheader("X-Trace-Id", "")
        event("workbench_loop.restart.probe.response", {"status": response.status, "trace_id": trace_id}, kind="http")
        if (response.status == 200 and response.getheader("Server", "").startswith("DramaWorkbench/")
                and re.fullmatch(r"[0-9a-f]{32}", trace_id) and get_tracer().get_events(trace_id, limit=1)):
            return trace_id
        return None
    except (OSError, http.client.HTTPException) as exc:
        event("workbench_loop.restart.probe.error", {"error": type(exc).__name__}, kind="http")
        return None
    finally:
        client.close()


def _listeners(port):
    argv = [str(Path(os.environ["SystemRoot"]) / "System32/netstat.exe"), "-ano", "-p", "tcp"]
    event("workbench_loop.listener.request", {"argv": argv, "port": port}, kind="subprocess")
    try:
        result = subprocess.run(argv, check=True, capture_output=True, timeout=45,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError) as exc:
        event("workbench_loop.listener.error", {"error": type(exc).__name__}, kind="subprocess")
        raise
    owners = set()
    for line in result.stdout.splitlines():
        fields = line.split()
        # netstat localizes its state column, but a listening IPv4 socket has this remote endpoint.
        if len(fields) == 5 and fields[0] == b"TCP" and fields[2] == b"0.0.0.0:0":
            host, _, local_port = fields[1].rpartition(b":")
            if local_port == str(port).encode() and host in {b"127.0.0.1", b"0.0.0.0"}:
                owners.add((host.decode(), int(fields[4])))
    event("workbench_loop.listener.response", {"port": port, "owners": sorted(owners)}, kind="subprocess")
    return owners


@contextmanager
def _process(manifest, port, *, stopping=False):
    """Pin the kernel process object across its ownership checks and any termination."""
    from ctypes import wintypes as wt

    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
    api.OpenProcess.restype = wt.HANDLE
    api.GetProcessTimes.argtypes = [wt.HANDLE] + [ctypes.POINTER(wt.FILETIME)] * 4
    api.WaitForSingleObject.argtypes = [wt.HANDLE, wt.DWORD]
    api.WaitForSingleObject.restype = wt.DWORD
    api.TerminateProcess.argtypes = [wt.HANDLE, wt.UINT]
    api.CloseHandle.argtypes = [wt.HANDLE]
    pid = int(manifest["pid"])
    handle = api.OpenProcess(0x100000 | 0x1000 | int(stopping), False, pid)
    if not handle:
        error = ctypes.get_last_error()
        if error == 87:  # ERROR_INVALID_PARAMETER for a vanished PID
            yield None
            return
        raise ctypes.WinError(error)
    try:
        if api.WaitForSingleObject(handle, 0) == 0:
            yield None
            return
        timestamps = [wt.FILETIME() for _ in range(4)]
        if not api.GetProcessTimes(handle, *(ctypes.byref(stamp) for stamp in timestamps)):
            raise ctypes.WinError(ctypes.get_last_error())
        created = ((timestamps[0].dwHighDateTime << 32) + timestamps[0].dwLowDateTime) / 10**7 - 11644473600
        if abs(created - datetime.fromisoformat(manifest["started_at"]).timestamp()) > 120:
            raise RuntimeError("工作台 PID 的创建时间与进程记录不符")
        class UnicodeString(ctypes.Structure):
            _fields_ = [("length", wt.USHORT), ("capacity", wt.USHORT), ("address", ctypes.c_void_p)]

        query = ctypes.WinDLL("ntdll").NtQueryInformationProcess
        query.argtypes = [wt.HANDLE, ctypes.c_int, ctypes.c_void_p, wt.ULONG, ctypes.POINTER(wt.ULONG)]
        size = wt.ULONG()
        query(handle, 60, None, 0, ctypes.byref(size))  # ProcessCommandLineInformation
        if not 0 < size.value < 131072:
            raise RuntimeError("无法核验工作台进程命令")
        buffer = ctypes.create_string_buffer(size.value)
        if query(handle, 60, buffer, len(buffer), ctypes.byref(size)) != 0:
            raise RuntimeError("无法读取工作台进程命令")
        value = UnicodeString.from_buffer(buffer)
        command = ctypes.wstring_at(value.address, value.length // 2)
        if not re.search(r"(?:^|\s)-m\s+pianchang\.web_server\s+--port\s+" + str(port) + r"\s*$", command):
            raise RuntimeError("PID 的启动命令不属于原端口上的工作台")

        def terminate():
            if not stopping or not api.TerminateProcess(handle, 0):
                raise RuntimeError("未取得工作台进程的停止回执")
            if api.WaitForSingleObject(handle, 30000) != 0:
                raise RuntimeError("无法确认工作台进程已退出")

        yield terminate
    finally:
        api.CloseHandle(handle)


def _stop_if_idle(manifest, port, web, busy):
    if os.name != "nt":
        try:
            os.kill(int(manifest["pid"]), 0)
        except ProcessLookupError:
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", port))
            return True
        raise RuntimeError("请在原端口重启工作台后用 resume 继续")
    pid = int(manifest["pid"])
    expected = {("127.0.0.1", pid)}
    with _process(manifest, port, stopping=True) as terminate:
        owners = _listeners(port)
        if terminate is None and not owners:
            return True
        trace_id = workbench_http_identity(port) if terminate and owners == expected else None
        if not trace_id:
            raise RuntimeError("原服务进程、监听或 HTTP 身份未能核实")
        if _manifest(web, manifest["url"]) != manifest or _listeners(port) != expected:
            raise RuntimeError("停止前工作台进程发生变化")
        if busy():
            return False
        event("workbench_loop.stop.request", {"pid": pid, "port": port, "started_at": manifest["started_at"],
                                              "verified_http_trace": trace_id}, kind="subprocess")
        terminate()
        event("workbench_loop.stop.response", {"pid": pid, "exited": True}, kind="subprocess")
    if _listeners(port):
        raise RuntimeError("原工作台端口仍被占用")
    return True


def _replacement_ready(manifest, port):
    if os.name != "nt":
        return bool(workbench_http_identity(port))
    with _process(manifest, port) as process:
        if process is None:
            raise RuntimeError("工作台新进程已退出")
        expected = {("127.0.0.1", int(manifest["pid"]))}
        owners = _listeners(port)
        if owners and owners != expected:
            raise RuntimeError("原端口的监听进程与新工作台记录不一致")
        return bool(owners and workbench_http_identity(port) and _listeners(port) == expected)


def _await_replacement(web, url, port, old_pid, old_started):
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        current = _manifest(web, url)
        if current["pid"] != old_pid or (old_started and current["started_at"] != old_started):
            if _replacement_ready(current, port) and _manifest(web, url) == current:
                event("workbench_loop.restart.ready", {"pid": current["pid"], "port": port}, kind="validation")
                return True
        time.sleep(2)
    raise RuntimeError("已保留启动请求，仍待核实新进程的监听和 HTTP 回执")


def restart_original(state, web, runs, busy, start_server):
    port, url, old_pid = int(state["port"]), state["url"], int(state["old_pid"])
    if url != f"http://127.0.0.1:{port}":
        raise ValueError("工作台端口记录发生变化")
    current = _manifest(web, url)
    old_started = state.get("old_started_at")
    if current["pid"] != old_pid or (old_started and current["started_at"] != old_started):
        return _await_replacement(web, url, port, old_pid, old_started)
    old_started = current["started_at"]
    key = hashlib.sha256(f"{url}|{old_pid}|{old_started}".encode()).hexdigest()
    marker = runs / "restarts" / (key + ".json")
    if marker.exists():
        return _await_replacement(web, url, port, old_pid, old_started)
    if busy() or not _stop_if_idle(current, port, web, busy):
        return False
    if _manifest(web, url) != current:
        return _await_replacement(web, url, port, old_pid, old_started)
    marker.parent.mkdir(parents=True, exist_ok=True)
    request = {"old_pid": old_pid, "old_started_at": old_started, "url": url}
    event("workbench_loop.restart.launch.request", request, kind="subprocess")
    try:
        with marker.open("x", encoding="utf-8") as stream:
            json.dump(request, stream)
            stream.flush()
            os.fsync(stream.fileno())
    except FileExistsError:
        return _await_replacement(web, url, port, old_pid, old_started)
    try:
        start_server(port, url)
    except TraceError:
        raise
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
        event("workbench_loop.restart.launch.error", {"error": type(exc).__name__}, kind="subprocess")
    else:
        event("workbench_loop.restart.launch.response", {"port": port}, kind="subprocess")
    return _await_replacement(web, url, port, old_pid, old_started)

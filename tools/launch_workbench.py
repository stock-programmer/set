"""Prepare and open the workbench belonging to this extracted directory."""
import argparse
from contextlib import contextmanager, nullcontext
import http.client
from importlib.metadata import PackageNotFoundError, version
import json
import os
import subprocess
import sys
import time
import webbrowser
from datetime import datetime
from pathlib import Path

from pianchang.bailian_text import TextError
from pianchang.bailian_video import VideoError
from pianchang.model_routes import SDK_VERSION, load_routes
from tools.launcher_dependencies import prepare_dependencies
from tools.workbench_config import configure_workbench
from trace_system import current_context, event, get_tracer, register_secret, span, trace, traced, use_context

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / "outputs/web"
DEFAULT_PORT = 8765


def _probe(port):
    # A header alone cannot distinguish two copies of the application. The
    # response trace must also have been written to this checkout's own store.
    path = "/agent-chat.css"
    url = f"http://127.0.0.1:{port}{path}"
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
    event("launcher.http.request", {"method": "HEAD", "url": url}, kind="http")
    try:
        conn.request("HEAD", path)
        response = conn.getresponse()
        server = response.getheader("Server", "")
        trace_id = response.getheader("X-Trace-Id", "")
        event("launcher.http.response", {"status": response.status, "server": server,
              "server_trace_id": trace_id}, kind="http")
        ours = (response.status == 200 and server.startswith("DramaWorkbench/") and
                len(trace_id) == 32 and all(c in "0123456789abcdef" for c in trace_id) and
                bool(get_tracer().get_events(trace_id, limit=1)))
        return "ours" if ours else "occupied"
    except (OSError, http.client.HTTPException) as exc:
        event("launcher.http.unavailable", {"error": type(exc).__name__}, kind="http")
        return "free" if isinstance(exc, ConnectionRefusedError) else "occupied"
    finally:
        conn.close()


def _select_port(requested):
    candidates = [requested] if requested is not None else list(range(DEFAULT_PORT, DEFAULT_PORT + 10))
    if requested is None:
        try:
            saved = json.loads((STATE / "server-process.json").read_text("utf-8-sig"))
            last = int(saved["url"].removeprefix("http://127.0.0.1:"))
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            last = 0
        if 1024 <= last <= 65535 and _probe(last) == "ours":
            return last, True
    for port in candidates:
        state = _probe(port)
        if state != "occupied":
            return port, state == "ours"
    choose_port = (r".\启动片场.cmd -Port 8876" if os.name == "nt" else "./启动片场.command --port 8876")
    raise RuntimeError(f"端口 {candidates[0]} 已被占用。请在项目目录运行 {choose_port}，或指定其他空闲端口。")


def _start_server(port, url):
    STATE.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    out, err = (STATE / f"server-{stamp}{suffix}.log" for suffix in ("", ".error"))
    argv = [sys.executable, "-u", "-m", "pianchang.web_server", "--port", str(port)]
    with span("launcher.server.start", kind="subprocess", inputs={"argv": argv, "cwd": ROOT,
              "stdout": out, "stderr": err}) as operation:
        env = {**os.environ, "PYTHONIOENCODING": "utf-8",
               "PIANCHANG_START_CONTEXT": json.dumps(current_context())}
        with out.open("xb") as stdout, err.open("xb") as stderr:
            process = subprocess.Popen(argv, cwd=ROOT, env=env, stdin=subprocess.DEVNULL,
                                       stdout=stdout, stderr=stderr, close_fds=True,
                                       start_new_session=os.name == "posix",
                                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        operation.set_output({"pid": process.pid})
    # Keep the chosen URL even when a slow machine outlives our readiness check.
    manifest = STATE / "server-process.json"
    tmp = manifest.with_suffix(".tmp")
    tmp.write_text(json.dumps({"pid": process.pid, "url": url, "started_at": datetime.now().astimezone().isoformat(),
                              "stdout": str(out), "stderr": str(err)}, ensure_ascii=False), encoding="utf-8")
    tmp.replace(manifest)
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"后台服务已退出。详情见 {err}")
        state = _probe(port)
        if state == "ours":
            return
        time.sleep(.4)
    raise RuntimeError(f"工作台尚未就绪，请稍后再次双击。后台日志：{err}")


def _open_browser(url):
    with span("launcher.open_browser", kind="tool", inputs={"url": url}):
        try:
            if sys.platform == "darwin":
                subprocess.run(["/usr/bin/open", url], check=True)
            elif os.name == "nt":
                os.startfile(url)
            elif not webbrowser.open(url):
                raise OSError("No browser available")
        except (OSError, subprocess.CalledProcessError) as exc:
            raise RuntimeError(f"工作台已就绪，请在浏览器中手动打开 {url}") from exc


@contextmanager
def _posix_launch_lock():
    import fcntl

    STATE.mkdir(parents=True, exist_ok=True)
    with (STATE / "launcher.lock").open("a+b") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("另一个片场窗口正在准备这个项目，请切换到该窗口继续。")
            yield False
        else:
            yield True


def _launch(requested_port, open_browser):
    port, running = _select_port(requested_port)
    routes = load_routes(ROOT / ".env")
    for key in routes.secrets():
        register_secret(key)
    print("正在检查本机运行环境……", flush=True)
    configured = any(routes.resolve(role + ".decision").api_key for role in ("script", "video"))
    if any(routes.resolve(purpose).adapter == "litellm" for purpose in routes.routes):
        try:
            sdk_ready = version("litellm") == SDK_VERSION
        except PackageNotFoundError:
            sdk_ready = False
        if not sdk_ready:
            print(f'已选用 LiteLLM {SDK_VERSION}。请在项目目录运行 python -m pip install ".[models]" 安装模型依赖。')
    capabilities = prepare_dependencies(install_agent=configured)
    url = f"http://127.0.0.1:{port}"
    if not running:
        print("正在启动工作台……", flush=True)
        _start_server(port, url)
    event("launcher.ready", {"path": url, "expected": "this project's workbench",
          "actual": "verified HTTP response in local trace store", "status": "PASS", "reused": running},
          kind="validation")
    print(f"片场{'已在运行' if running else '已启动'}：{url}", flush=True)
    if open_browser:
        _open_browser(url)
        if configured and not all(capabilities.values()) and sys.stdin.isatty():
            input("工作台已打开。上方列有尚需安装的创作工具；看完后按回车关闭此窗口。")


@traced("cli.launcher.workbench", kind="cli")
def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int)
    parser.add_argument("--open-browser", action="store_true")
    parser.add_argument("--configure", action="store_true", help="首次启动时创建本地设置并提示填写 API Key")
    args = parser.parse_args()
    if args.port is not None and not 1024 <= args.port <= 65535:
        parser.error("端口必须在 1024 到 65535 之间")
    with (_posix_launch_lock() if os.name == "posix" else nullcontext(True)) as acquired:
        if acquired:
            configure_workbench(ROOT, interactive=args.configure)
            _launch(args.port, args.open_browser)


if __name__ == "__main__":
    try:
        # The HTTP server explicitly uses the trace store under the project.
        with use_context(None, tracer=get_tracer(ROOT / "outputs/traces")), trace("launcher.workbench"):
            main()
    except (OSError, RuntimeError, VideoError, TextError, ValueError) as error:
        print(f"启动提示：{error}", file=sys.stderr, flush=True)
        raise SystemExit(1)

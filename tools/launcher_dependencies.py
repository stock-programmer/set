"""Prepare the optional Agent and media runtimes for a local workbench launch."""
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from pianchang.media_tools import MediaError, find_ffmpeg, find_ffprobe
from trace_system import event, register_secret, span, trace, traced


ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "pi-runtime"
SDK = "node_modules/@earendil-works/pi-coding-agent"


def _check(path, expected, actual, passed):
    event("launcher.dependency.check", {"path": path, "expected": expected, "actual": actual,
          "status": "PASS" if passed else "FAIL"}, kind="validation")
    return passed


def _run(argv, cwd, timeout=30):
    """Run non-interactive dependency checks with the shared trace SDK."""
    with span("launcher.dependency.process", kind="subprocess",
              inputs={"argv": argv, "cwd": cwd, "stdin": "closed", "timeout_s": timeout}) as operation:
        try:
            result = subprocess.run(argv, cwd=cwd, stdin=subprocess.DEVNULL,
                                    capture_output=True, text=True, encoding="utf-8", errors="replace",
                                    timeout=timeout, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except (OSError, subprocess.TimeoutExpired) as exc:
            operation.set_status("ERROR", type(exc).__name__)
            operation.set_output({"error": str(exc)})
            return None
        operation.set_output({"returncode": result.returncode, "stdout": result.stdout, "stderr": result.stderr})
        if result.returncode:
            operation.set_status("ERROR", f"exit {result.returncode}")
        return result


def _pi_files_ready(runtime, locked):
    # npm's hidden lock describes the packages it actually unpacked, including
    # platform-specific optional packages. Check the required set and the SDK.
    try:
        installed = json.loads((runtime / "node_modules/.package-lock.json").read_text("utf-8"))["packages"]
        for path, wanted in locked.items():
            if not path or (wanted.get("optional") and path not in installed):
                continue
            local = installed.get(path, {})
            if any(local.get(field) != wanted.get(field) for field in ("version", "integrity")):
                return False
            manifest = json.loads((runtime / path / "package.json").read_text("utf-8"))
            if manifest.get("version") != wanted["version"]:
                return False
            if path == SDK and not (runtime / path / manifest["main"]).is_file():
                return False
        return True
    except (OSError, ValueError, KeyError, TypeError):
        return False


def _pi_ready(runtime, locked):
    matched = _pi_files_ready(runtime, locked)
    return _check("pi-runtime/node_modules", "包版本、npm 完整性清单与锁文件匹配，Pi 入口存在", matched, matched)


def _prepare_agent(runtime, *, install_agent=True):
    node = shutil.which("node")
    probe = _run([node, "--version"], runtime) if node else None
    version = probe.stdout.strip() if probe and probe.returncode == 0 else ""
    match = re.fullmatch(r"v(\d+)\.(\d+)\.(\d+)", version)
    supported = match is not None and tuple(map(int, match.groups())) >= (22, 19, 0)
    if not _check("node", ">=22.19.0", version or None, supported):
        print("Agent 尚未就绪。请从 https://nodejs.org/ 安装 Node.js 22.19 或更高版本（含 npm），再双击启动。")
        return False

    locked = json.loads((runtime / "package-lock.json").read_text("utf-8"))["packages"]
    if not _pi_ready(runtime, locked):
        if not install_agent:
            print("可以先浏览工作台。填写 API Key 后再双击启动，片场会安装 Agent 依赖。")
            return False
        npm = shutil.which("npm.cmd" if os.name == "nt" else "npm")
        if not _check("npm", "可用于安装 Agent 依赖", npm, bool(npm)):
            print("Agent 依赖需要 npm。请用 Node.js 安装程序补装 npm，再双击启动。")
            return False
        # Invoke the JS entry directly on Windows when available, so paths with
        # shell metacharacters are safe and a timed-out install has no cmd parent.
        npm_entry = Path(npm).parent / "node_modules/npm/bin/npm-cli.js"
        command = [node, str(npm_entry)] if os.name == "nt" and npm_entry.is_file() else [npm]
        print("正在按锁定版本安装 Agent 依赖，首次安装需要联网……", flush=True)
        result = _run([*command, "ci", "--ignore-scripts", "--no-audit", "--no-fund",
                       "--prefer-offline", "--fetch-retries=1", "--fetch-timeout=30000"], runtime, timeout=900)
        if result is None or result.returncode != 0 or not _pi_ready(runtime, locked):
            print("Agent 依赖未就绪，仍可浏览工作台。请检查网络后重新双击，或在项目目录运行 npm --prefix pi-runtime ci --ignore-scripts 查看详细错误。")
            return False
    print(f"Agent 已就绪（Node.js {version}，Pi {locked[SDK]['version']}）。")
    return True


def _media_ready():
    missing = []
    for name, locate in (("ffmpeg", find_ffmpeg), ("ffprobe", find_ffprobe)):
        try:
            tool = locate()
            probe = _run([str(tool), "-version"], ROOT)
            version = probe.stdout.splitlines()[0] if probe and probe.stdout and probe.returncode == 0 else ""
        except MediaError:
            version = ""
        if not _check(name, f"{name} version", version, version.startswith(name + " version ")):
            missing.append(name)
    if missing:
        print("媒体处理待配置（" + "、".join(missing) + "）。请将 ffmpeg 和 ffprobe 放入 .tools/ffmpeg/bin，"
              "或加入 PATH；自定义路径使用 FFMPEG_PATH 和 FFPROBE_PATH。下载地址 https://ffmpeg.org/download.html")
    else:
        print("媒体工具已就绪（ffmpeg、ffprobe）。")
    return not missing


@traced("launcher.dependencies.prepare", kind="tool")
def prepare_dependencies(*, install_agent=True):
    for name, value in os.environ.items():
        if any(word in name.upper() for word in ("KEY", "TOKEN", "PASSWORD", "SECRET")):
            register_secret(value)
    print(f"工作台使用 Python {sys.version.split()[0]}。")
    return {"agent": _prepare_agent(RUNTIME, install_agent=install_agent), "media": _media_ready()}


@traced("cli.launcher.dependencies", kind="cli")
def main():
    prepare_dependencies()
    return 0


if __name__ == "__main__":
    with trace("launcher.dependencies", kind="task"):
        exit_code = main()
    raise SystemExit(exit_code)

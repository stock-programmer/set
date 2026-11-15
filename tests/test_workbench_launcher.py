"""Offline regressions for opening a fresh checkout and reusing its workbench."""
import errno
import http.client
import json
import os
from pathlib import Path
import select
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from tools import launch_workbench as launcher
from trace_system import get_tracer, use_context


def write_test_routes(root):
    (root / "config").mkdir()
    config = {"version": 1, "connections": {"writer": {"provider": "example", "account": "writer",
              "base_url": "https://example.test/v1", "api_key_env": "WRITER_CREDENTIAL"}},
              "models": {"writer": {"adapter": "litellm", "connection": "writer", "sdk_provider": "openai",
                                    "model_id": "example-writer"}}, "routes": {"script.decision": "writer"}}
    (root / "config/models.json").write_text(json.dumps(config), encoding="utf-8")


class LauncherTests(unittest.TestCase):
    def test_custom_decision_key_prepares_pi_without_prompting_for_bailian(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".env.example").write_text("DASHSCOPE_API_KEY=\n", encoding="utf-8")
            write_test_routes(root)
            with (use_context(None, tracer=get_tracer(root / "traces")),
                  patch.dict(os.environ, {"DASHSCOPE_API_KEY": "", "WRITER_CREDENTIAL": "offline-test-credential"}),
                  patch("tools.workbench_config.getpass.getpass") as prompt,
                  patch.object(launcher, "ROOT", root),
                  patch.object(launcher, "_select_port", return_value=(22123, True)),
                  patch.object(launcher, "prepare_dependencies") as prepare):
                launcher.configure_workbench(root)
                launcher._launch(None, False)
            prompt.assert_not_called()
            prepare.assert_called_once_with(install_agent=True)
            self.assertNotIn("offline-test-credential", (root / ".env").read_text("utf-8"))

    def test_port_conflict_and_opening_the_verified_url(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".env").write_text("DASHSCOPE_API_KEY=\n", encoding="utf-8")
            tracer = get_tracer(root / "traces")
            with use_context(None, tracer=tracer), tracer.trace("launcher.offline.mock"):
                with patch.object(launcher, "_probe", return_value="occupied"):
                    with self.assertRaisesRegex(RuntimeError, "8876"):
                        launcher._select_port(8876)
                opener = (patch.object(os, "startfile", create=True) if os.name == "nt"
                          else patch.object(launcher, "_open_browser"))
                with (
                    patch.object(launcher, "ROOT", root),
                    patch.object(launcher, "_select_port", return_value=(22123, True)),
                    patch.object(launcher, "_start_server") as spawn,
                    patch.object(launcher, "prepare_dependencies") as prepare,
                    opener as open_url,
                    patch.dict(os.environ, DASHSCOPE_API_KEY=""),
                    patch.object(sys, "argv", ["launcher", "--open-browser"]),
                ):
                    launcher.main()
                    prepare.assert_called_once_with(install_agent=False)
                    spawn.assert_not_called()
                    open_url.assert_called_once_with("http://127.0.0.1:22123")

    def test_macos_sends_the_verified_url_to_launch_services(self):
        url = "http://127.0.0.1:22123"
        with tempfile.TemporaryDirectory() as tmp, use_context(None, tracer=get_tracer(tmp)):
            with (
                patch.object(sys, "platform", "darwin"),
                patch.object(subprocess, "run") as run,
            ):
                launcher._open_browser(url)
                run.assert_called_once_with(["/usr/bin/open", url], check=True)
                run.side_effect = subprocess.CalledProcessError(1, "open")
                with self.assertRaisesRegex(RuntimeError, url):
                    launcher._open_browser(url)

    @unittest.skipUnless(os.name == "posix", "requires a real POSIX terminal")
    def test_first_configuration_can_skip_then_enter_a_hidden_key(self):
        import pty
        import termios

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            shutil.copyfile(launcher.ROOT / ".env.example", root / ".env.example")
            trace_dir = root / "traces"
            env = {"HOME": tmp, "PATH": os.defpath, "AGENT_TRACE_DIR": str(trace_dir),
                   "PYTHONUTF8": "1", "PYTHONPATH": str(launcher.ROOT)}
            # Only configure the project. This explicitly offline test never runs npm or a model.
            code = ("import sys; from pathlib import Path; from tools.workbench_config import configure_workbench; "
                    "from trace_system import trace\nwith trace('launcher.offline_config_test'): "
                    "configure_workbench(Path(sys.argv[1]))")
            command = [sys.executable, "-c", code, str(root)]

            def answer(value):
                # forkpty gives getpass a controlling /dev/tty, just as Terminal.app does.
                pid, master = pty.fork()
                if pid == 0:
                    try:
                        os.execve(sys.executable, command, env)
                    finally:
                        os._exit(127)
                output = bytearray()
                deadline = time.monotonic() + 45

                def read_output():
                    remaining = deadline - time.monotonic()
                    if remaining <= 0 or not select.select([master], [], [], remaining)[0]:
                        self.fail("Timed out reading the terminal: " + output.decode("utf-8", "replace"))
                    try:
                        chunk = os.read(master, 4096)
                    except OSError as exc:
                        if exc.errno != errno.EIO:  # Linux reports EIO when the slave closes.
                            raise
                        return False
                    output.extend(chunk)
                    return bool(chunk)

                try:
                    prompt = "粘贴 API Key".encode()
                    while prompt not in output:
                        if not read_output():
                            self.fail("No Key prompt: " + output.decode("utf-8", "replace"))
                    self.assertFalse(termios.tcgetattr(master)[3] & termios.ECHO,
                                     "Echo enabled at Key prompt: " + output.decode("utf-8", "replace"))
                    os.write(master, value + b"\n")
                    while read_output():
                        pass
                    _, status = os.waitpid(pid, 0)
                    pid = None
                    self.assertEqual(os.waitstatus_to_exitcode(status), 0, output.decode("utf-8", "replace"))
                    return bytes(output)
                finally:
                    os.close(master)
                    if pid is not None:
                        try:
                            os.kill(pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                        os.waitpid(pid, 0)

            answer(b"")
            self.assertEqual((root / ".env").read_bytes(), (root / ".env.example").read_bytes())
            mock_key = b"offline-fixture-only-72326"
            self.assertNotIn(mock_key, answer(mock_key))
            self.assertIn(b"DASHSCOPE_API_KEY=" + mock_key, (root / ".env").read_bytes())
            self.assertEqual((root / ".env").stat().st_mode & 0o777, 0o600)
            files = [path for path in trace_dir.rglob("*") if path.is_file()]
            self.assertTrue(files)
            for path in files:
                self.assertNotIn(mock_key, path.read_bytes(), str(path))

    @unittest.skipUnless(os.name == "nt" or sys.platform == "darwin", "Windows and macOS desktop launchers")
    def test_first_launch_from_another_directory_and_relaunch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / ("source" if os.name == "nt" else "片场 解压目录")
            root.mkdir()
            # Only copy product files. Developer outputs and credentials are never inputs.
            for name in ("pianchang", "pi_bridge", "trace_system", "tools", "fixtures", "web", "contracts"):
                shutil.copytree(launcher.ROOT / name, root / name, ignore=shutil.ignore_patterns("__pycache__"))
            (root / "pi-runtime").mkdir()
            for name in (".env.example", "pi-runtime/package-lock.json"):
                shutil.copyfile(launcher.ROOT / name, root / name)

            if os.name == "nt":
                entry = root / "启动片场.cmd"
                windows = Path(os.environ["SystemRoot"])
                system = windows / "System32"
                env = {k: os.environ[k] for k in ("SystemRoot", "WINDIR", "TEMP", "TMP", "ComSpec",
                                                  "PATHEXT", "USERPROFILE", "LOCALAPPDATA") if k in os.environ}
                env["PATH"] = os.pathsep.join(map(str, (Path(sys.executable).parent,
                                                        system / "WindowsPowerShell/v1.0", system, windows)))
            else:
                entry = root / "启动片场.command"
                env = {"HOME": tmp, "PATH": f"{Path(sys.executable).parent}:/usr/bin:/bin:/usr/sbin:/sbin",
                       "LANG": "en_US.UTF-8"}
            shutil.copy2(launcher.ROOT / entry.name, entry)
            if os.name == "nt":
                write_test_routes(root)
                package = shutil.make_archive(str(Path(tmp) / "offline-product"), "zip", root)
                root = Path(tmp) / "片场 解压目录"
                shutil.unpack_archive(package, root)
                entry = root / entry.name
            env["PYTHONIOENCODING"] = "utf-8"
            manifest = root / "outputs/web/server-process.json"

            def start(port=None):
                if os.name == "nt":
                    options = f" -Port {port}" if port else ""
                    command = f'cmd.exe /d /s /c ""{entry}" -NoBrowser{options}"'
                else:
                    command = [str(entry), "--no-browser"]
                    if port:
                        command.extend(["--port", str(port)])
                result = subprocess.run(command, cwd=tmp, env=env, input=b"\r\n",
                                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=120)
                self.assertEqual(result.returncode, 0, result.stdout.decode("utf-8", "replace"))
                return json.loads(manifest.read_text(encoding="utf-8-sig"))

            with socket.socket() as reserve:
                reserve.bind(("127.0.0.1", 0))
                port = reserve.getsockname()[1]
            try:
                first = start(port)
                self.assertEqual(first["url"], f"http://127.0.0.1:{port}")
                self.assertEqual((root / ".env").read_bytes(), (root / ".env.example").read_bytes())
                if sys.platform == "darwin":
                    self.assertEqual((root / ".env").stat().st_mode & 0o777, 0o600)
                    self.assertEqual(os.getsid(first["pid"]), first["pid"], "server must survive Terminal closing")
                conn = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
                try:
                    conn.request("GET", "/api/bootstrap")
                    response = conn.getresponse()
                    self.assertEqual(response.status, 200)
                    data = json.load(response)
                finally:
                    conn.close()
                self.assertFalse(data["config"]["key_configured"])
                if os.name == "nt":
                    self.assertEqual(data["config"]["model_routes"]["script.decision"]["model"], "example-writer")
                sample = next(item for item in data["projects"] if item["title"].startswith("最后一把钥匙"))
                self.assertEqual([s["id"] for s in sample["opening"]["shots"]], ["S001", "S002", "S003"])
                self.assertEqual(start(), first)
                self.assertFalse((root / "pi-runtime/node_modules").exists())
            finally:
                if manifest.exists():
                    # The temporary folder starts empty and only this launcher can create its manifest.
                    pid = int(json.loads(manifest.read_text(encoding="utf-8-sig"))["pid"])
                    if os.name == "nt":
                        subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True, timeout=20)
                    else:
                        try:
                            os.kill(pid, signal.SIGTERM)
                        except ProcessLookupError:
                            pass


if __name__ == "__main__":
    unittest.main()

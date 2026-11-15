"""Offline checks for first-use detection, recovery, and repeat launches."""
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from tools import launcher_dependencies as launcher
from trace_system import get_tracer, use_context


class DependencyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.runtime = Path(temporary.name)
        self.lock = {launcher.SDK: {"version": "1.5.2", "integrity": "mock-digest"}}
        self.write("package-lock.json", {"packages": self.lock})
        tracer = get_tracer(self.runtime / "traces")
        self.enterContext(use_context(None, tracer=tracer))
        self.enterContext(tracer.trace("launcher.tests", inputs={"label": "explicit offline mock"}))
        self.enterContext(redirect_stdout(io.StringIO()))
        self.enterContext(patch.object(launcher.shutil, "which", side_effect=lambda name: str(self.runtime / name)))

    def write(self, name, data):
        target = self.runtime / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(data), encoding="utf-8")

    def populate(self):
        self.write("node_modules/.package-lock.json", {"packages": self.lock})
        self.write(launcher.SDK + "/package.json", {"version": "1.5.2", "main": "./dist/index.js"})
        self.write(launcher.SDK + "/dist/index.js", "mock SDK")

    def test_install_then_relaunch_reuses_dependencies(self):
        def mock_run(argv, cwd, timeout=30):
            if "ci" in argv:
                self.assertIn("--ignore-scripts", argv)
                self.populate()
            return subprocess.CompletedProcess(argv, 0, "v22.19.0\n", "")

        with patch.object(launcher, "_run", side_effect=mock_run) as run:
            self.assertTrue(launcher._prepare_agent(self.runtime))
            self.assertTrue(launcher._prepare_agent(self.runtime))
        self.assertEqual(1, sum("ci" in call.args[0] for call in run.call_args_list))
        (self.runtime / launcher.SDK / "dist/index.js").unlink()
        self.assertFalse(launcher._pi_files_ready(self.runtime, self.lock))

    def test_unsupported_node_and_failed_install(self):
        old_node = subprocess.CompletedProcess(["node"], 0, "v22.18.0\n", "")
        with patch.object(launcher, "_run", return_value=old_node) as run:
            self.assertFalse(launcher._prepare_agent(self.runtime))
            self.assertEqual(1, run.call_count)
        current = subprocess.CompletedProcess(["node"], 0, "v24.0.0\n", "")
        failed = subprocess.CompletedProcess(["npm"], 1, "", "offline mock")
        with patch.object(launcher, "_run", side_effect=[current, failed]):
            self.assertFalse(launcher._prepare_agent(self.runtime))

    def test_preview_skips_download_and_detects_existing_sdk(self):
        current = subprocess.CompletedProcess(["node"], 0, "v24.0.0\n", "")
        with patch.object(launcher, "_run", return_value=current) as run:
            self.assertFalse(launcher._prepare_agent(self.runtime, install_agent=False))
            self.populate()
            self.assertTrue(launcher._prepare_agent(self.runtime, install_agent=False))
        self.assertEqual([call.args[0][-1] for call in run.call_args_list], ["--version", "--version"])

    def test_media_requires_both_executables(self):
        probe = subprocess.CompletedProcess(["ffmpeg"], 0, "ffmpeg version 8.0\n", "")
        with patch.object(launcher, "find_ffmpeg", return_value=Path("mock-ffmpeg")), \
             patch.object(launcher, "find_ffprobe", side_effect=launcher.MediaError("offline mock")), \
             patch.object(launcher, "_run", return_value=probe):
            self.assertFalse(launcher._media_ready())


@unittest.skipUnless(os.name == "nt", "Windows PowerShell bootstrap")
class PythonInstallTests(unittest.TestCase):
    def run_powershell(self, script):
        result = subprocess.run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
                                cwd=launcher.ROOT, capture_output=True, timeout=120)
        self.assertEqual(0, result.returncode, (result.stdout + result.stderr).decode(errors="replace"))

    def test_store_alias_probe_cannot_open_store_or_install_runtime(self):
        self.run_powershell(r"""
$ErrorActionPreference = 'Stop'
. .\tools\launcher_dependencies.ps1
$env:PYTHON_MANAGER_AUTOMATIC_INSTALL = 'true'
function Get-Command { param($Name, $CommandType, [switch]$All, $ErrorAction)
    if ($Name -eq 'python.exe') { return @{Source='Invoke-MockStoreAlias'} }
}
function Invoke-MockStoreAlias {
    if ($args.Count -ne 3 -or $args[0] -ne '-I' -or $args[1] -ne '-c' -or
        $env:PYTHON_MANAGER_AUTOMATIC_INSTALL -ne 'false') {
        $script:unsafe = $true
    }
    if ($script:available) {
        $global:LASTEXITCODE = 0
        return [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($script:interpreter))
    }
    $global:LASTEXITCODE = 1
}
$script:unsafe = $script:available = $false
if (Find-PianchangPython) { throw 'Store placeholder reported as a runtime' }
$script:available = $true
$script:interpreter = (Get-Process -Id $PID).Path
if ((Find-PianchangPython) -ne $script:interpreter) { throw 'working Store alias missed' }
if ($script:unsafe -or $env:PYTHON_MANAGER_AUTOMATIC_INSTALL -ne 'true') { throw 'unsafe probe' }
exit 0
""")

    def test_consent_decline_failure_and_noninteractive(self):
        # No real installer, browser, or network access in this PowerShell process.
        script = r"""
$ErrorActionPreference = 'Stop'
. .\tools\launcher_dependencies.ps1
function Find-PianchangPython { if ($script:installed) { return 'mock-python.exe' } }
function Get-Command { param($Name, $CommandType, $ErrorAction)
    if ($Name -eq 'winget.exe') { return @{Source = 'mock-winget.exe'} }
}
function Install-PianchangPython { param($Winget)
    if ($Winget -ne 'mock-winget.exe') { throw 'unexpected installer' }
    $script:installs++
    $script:installed = $script:willSucceed
    return $script:installed
}
function Read-Host { return $script:answers.Dequeue() }
function Start-Process { param($url)
    if ($url -ne 'https://www.python.org/downloads/windows/') { throw 'unexpected URL' }
    $script:browsers++
}
foreach ($case in 'consent', 'decline', 'failure', 'unattended') {
    $script:answers = [Collections.Queue]::new()
    $script:installs = $script:browsers = 0
    $script:installed = $false
    $script:willSucceed = $case -eq 'consent'
    if ($case -ne 'unattended') {
        $script:answers.Enqueue($(if ($case -eq 'decline') {'N'} else {'Y'}))
        if ($case -ne 'consent') { $script:answers.Enqueue($(if ($case -eq 'failure') {'Y'} else {'N'})) }
    }
    $value = $null
    try { $value = Get-PianchangPython -Interactive:($case -ne 'unattended') }
    catch { if ($_.Exception.Message -notmatch 'python.org/downloads/windows') { throw } }
    if ($script:answers.Count -ne 0 -or
        ($case -eq 'consent') -ne ($value -eq 'mock-python.exe') -or
        $script:installs -ne [int]($case -in 'consent','failure') -or
        $script:browsers -ne [int]($case -eq 'failure')) { throw "failed scenario: $case" }
}
exit 0
"""
        self.run_powershell(script)

# Locate a real Python executable: the py launcher itself cannot run a module
# with the same arguments as every supported python.exe installation.
function Find-PianchangPython {
    $probe = 'import base64,sys; print(base64.b64encode(sys.executable.encode()).decode()) if sys.version_info >= (3,11) else sys.exit(1)'
    # Probe only: an empty Python install manager can otherwise install a runtime.
    $installSetting = $env:PYTHON_MANAGER_AUTOMATIC_INSTALL
    try {
        $env:PYTHON_MANAGER_AUTOMATIC_INSTALL = 'false'
        Find-PianchangInterpreter $probe
    } finally {
        $env:PYTHON_MANAGER_AUTOMATIC_INSTALL = $installSetting
    }
}

function Find-PianchangInterpreter($Probe) {
    $names = @('py.exe', 'python.exe', 'python3.exe')
    # A user install may not be on this already-running process's PATH.
    if ($env:LOCALAPPDATA) {
        $standardPath = Join-Path $env:LOCALAPPDATA 'Programs\Python\Python312\python.exe'
        if (Test-Path -LiteralPath $standardPath -PathType Leaf) { $names += $standardPath }
    }
    foreach ($name in $names) {
        $commands = Get-Command $name -CommandType Application -All -ErrorAction SilentlyContinue
        foreach ($command in $commands) {
            # With arguments the App Installer placeholder returns an error,
            # while a working Store Python runs this version/path probe.
            $arguments = @('-I', '-c', $Probe)
            if ($name -eq 'py.exe') { $arguments = @('-3') + $arguments }
            try {
                $encoded = & $command.Source @arguments 2>$null
                if ($LASTEXITCODE -eq 0 -and $encoded) {
                    $path = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($encoded))
                    if (Test-Path -LiteralPath $path -PathType Leaf) { return $path }
                }
            } catch {
                # Another entry on PATH may still point to a usable interpreter.
                continue
            }
        }
    }
}

function Install-PianchangPython($Winget) {
    try {
        & $Winget install --id Python.Python.3.12 --exact --source winget --scope user --silent --accept-source-agreements --accept-package-agreements | Out-Host
        return $LASTEXITCODE -eq 0
    } catch {
        return $false
    }
}

function Get-PianchangPython {
    param([switch]$Interactive)
    $python = Find-PianchangPython
    if ($python) { return $python }
    $download = 'https://www.python.org/downloads/windows/'
    if ($Interactive) {
        Write-Host '打开片场需要先安装 Python 3.11 或更高版本。'
        $winget = Get-Command winget.exe -CommandType Application -ErrorAction SilentlyContinue
        if ($winget) {
            $answer = Read-Host '是否同意用 Windows 程序包管理器为当前用户安装 Python 3.12 并继续启动？[Y/N]'
            if ($answer -match '^(y|yes|是)$') {
                if (Install-PianchangPython $winget.Source) {
                    $python = Find-PianchangPython
                    if ($python) { return $python }
                }
                Write-Host '未检测到已就绪的 Python，可改用官方安装程序。'
            }
        }
        $answer = Read-Host '现在打开 Python 官方 Windows 下载页吗？[Y/N]'
        if ($answer -match '^(y|yes|是)$') { Start-Process $download }
    }
    throw "请到 $download 安装 Python 3.11 或更高版本，勾选 Add python.exe to PATH，安装完成后再双击启动片场。"
}

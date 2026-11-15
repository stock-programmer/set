param(
    [ValidateRange(1024, 65535)][int]$Port = 8765,
    [switch]$Interactive,
    [switch]$NoBrowser
)
$ErrorActionPreference = 'Stop'
# Use the same encoding for PowerShell and native output, including redirected
# consoles whose Windows code page may differ from the user's language.
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
$env:PYTHONIOENCODING = 'utf-8'
$projectRoot = Split-Path -Parent $PSScriptRoot
. (Join-Path $PSScriptRoot 'launcher_dependencies.ps1')

Push-Location -LiteralPath $projectRoot
try {
    Write-Host '片场 · 短剧制作工作台'
    # Both .env setup and npm ci must finish before a second launcher can run.
    $hash = [Security.Cryptography.SHA256]::Create()
    try { $id = [BitConverter]::ToString($hash.ComputeHash([Text.Encoding]::UTF8.GetBytes($projectRoot.ToUpperInvariant()))) }
    finally { $hash.Dispose() }
    $mutex = New-Object Threading.Mutex($false, "Local\Pianchang-Launcher-$id")
    try {
        try { $owner = $mutex.WaitOne(0) } catch [Threading.AbandonedMutexException] { $owner = $true }
        if (-not $owner) {
            Write-Host '另一个片场启动窗口正在准备此项目，请切换到该窗口继续。'
            exit 0
        }
        $python = Get-PianchangPython -Interactive:$Interactive
        $options = @()
        if ($Interactive) { $options += '--configure' }
        if ($PSBoundParameters.ContainsKey('Port')) { $options += @('--port', "$Port") }
        if ($Interactive -and -not $NoBrowser) { $options += '--open-browser' }
        & $python -m tools.launch_workbench @options
        exit $LASTEXITCODE
    } finally {
        if ($owner) { $mutex.ReleaseMutex() }
        $mutex.Dispose()
    }
} catch {
    Write-Host ("片场启动未完成：" + $_.Exception.Message) -ForegroundColor Red
    exit 1
} finally { Pop-Location }

[CmdletBinding()]
param([string]$Python = "python", [switch]$Development)

$ErrorActionPreference = "Stop"
$repoPath = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
Push-Location -LiteralPath $repoPath
try {
    & $Python -c "import sys; assert sys.version_info >= (3,11), 'Python 3.11 or newer is required'"
    if ($LASTEXITCODE -ne 0) { throw "A working Python 3.11+ installation is required." }
    $venvPython = Join-Path $repoPath ".venv\Scripts\python.exe"
    if (-not (Test-Path -LiteralPath $venvPython)) {
        & $Python -m venv .venv
        if ($LASTEXITCODE -ne 0) { throw "Could not create .venv." }
    }
    $requirementsPath = if ($Development) { "requirements-dev.txt" } else { "requirements-gui.txt" }
    & $venvPython -m pip install -r $requirementsPath
    if ($LASTEXITCODE -ne 0) { throw "Dependency installation failed. The launcher has not been started." }
    Write-Host "Setup complete. Double-click Voice Clipper.cmd."
    Write-Host "Source mode also needs VLC matching Python's architecture, and ffmpeg/ffprobe on PATH."
} finally {
    Pop-Location
}

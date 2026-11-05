[CmdletBinding()]
param(
    [string]$Python = "",
    [string]$VlcPath = "",
    [string]$FfmpegPath = "",
    [string]$FfprobePath = ""
)

$ErrorActionPreference = "Stop"
$repoPath = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
if (-not $Python) { $Python = Join-Path $repoPath ".venv\Scripts\python.exe" }
if (-not (Test-Path -LiteralPath $Python)) {
    throw "Python environment missing. Run scripts/setup-windows.ps1 -Development first or pass -Python."
}
if (-not $VlcPath) {
    foreach ($basePath in @($env:ProgramFiles, ${env:ProgramFiles(x86)})) {
        if (-not $basePath) { continue }
        $candidate = Join-Path $basePath "VideoLAN\VLC"
        if (Test-Path -LiteralPath (Join-Path $candidate "libvlc.dll")) { $VlcPath = $candidate; break }
    }
}
if (-not $VlcPath -or -not (Test-Path -LiteralPath (Join-Path $VlcPath "plugins"))) {
    throw "Install VLC or pass -VlcPath pointing to a folder containing libvlc.dll and plugins/."
}
$VlcPath = [IO.Path]::GetFullPath($VlcPath)
function Resolve-Tool([string]$ExplicitPath, [string]$ToolName) {
    if ($ExplicitPath) {
        if (-not (Test-Path -LiteralPath $ExplicitPath -PathType Leaf)) { throw "$ToolName was not found: $ExplicitPath" }
        return (Get-Item -LiteralPath $ExplicitPath).FullName
    }
    $command = Get-Command $ToolName -ErrorAction SilentlyContinue
    if (-not $command) { throw "$ToolName is not on PATH. Pass its executable path explicitly." }
    return $command.Source
}
$FfmpegPath = Resolve-Tool $FfmpegPath "ffmpeg.exe"
$FfprobePath = Resolve-Tool $FfprobePath "ffprobe.exe"

# Compare the PE machine against Python before producing an unusable bundle.
$stream = [IO.File]::OpenRead((Join-Path $VlcPath "libvlc.dll"))
$reader = New-Object IO.BinaryReader($stream)
try {
    $stream.Position = 0x3c
    $peOffset = $reader.ReadInt32()
    $stream.Position = $peOffset + 4
    $vlcMachine = $reader.ReadUInt16()
} finally { $reader.Dispose(); $stream.Dispose() }
$pythonBits = & $Python -c "import struct; print(struct.calcsize('P') * 8)"
if ($LASTEXITCODE -ne 0) { throw "Could not inspect Python architecture." }
if (($pythonBits -eq "64" -and $vlcMachine -eq 0x14c) -or ($pythonBits -eq "32" -and $vlcMachine -ne 0x14c)) {
    throw "VLC and Python architectures do not match. Use matching 64-bit installations."
}

Push-Location -LiteralPath $repoPath
try {
    $buildArgs = @(
        "-m", "PyInstaller", "--noconfirm", "--clean", "--onedir", "--windowed",
        "--name", "Voice Clipper", "--distpath", "dist", "--workpath", "build\pyinstaller",
        "--icon", (Join-Path $repoPath "assets\voice-clipper.ico"),
        "--specpath", "build", "--paths", $repoPath,
        "--add-data", ((Join-Path $repoPath "assets") + ";assets"),
        "--add-data", ((Join-Path $repoPath "LICENSE") + ";."),
        "--collect-data", "soundcard", "--collect-all", "soundfile",
        "--exclude-module", "soundcard.__pyinstaller.conftest", "--exclude-module", "pytest",
        "--exclude-module", "PySide6.QtWebEngineCore", "--exclude-module", "PySide6.QtWebEngineWidgets",
        "--exclude-module", "PySide6.QtWebEngineQuick", "--exclude-module", "PySide6.QtPdf",
        "--collect-all", "keyboard", "--copy-metadata", "python-vlc",
        "--copy-metadata", "PySide6", "--copy-metadata", "numpy",
        "--copy-metadata", "soundcard", "--copy-metadata", "soundfile",
        "--copy-metadata", "keyboard", "voice_clipper_gui.py"
    )
    & $Python @buildArgs
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller build failed." }
    $appPath = Join-Path $repoPath "dist\Voice Clipper"
    $runtimePath = Join-Path $appPath "_internal"
    $vlcBundlePath = Join-Path $runtimePath "vlc"
    $toolsBundlePath = Join-Path $runtimePath "tools\bin"
    $noticesPath = Join-Path $appPath "third-party-notices"
    New-Item -ItemType Directory -Force -Path $vlcBundlePath, $toolsBundlePath, $noticesPath | Out-Null
    # Include the complete installed VLC runtime and its own notices/data. Leave out its uninstaller.
    Get-ChildItem -LiteralPath $VlcPath | Where-Object Name -NE "uninstall.exe" | ForEach-Object {
        Copy-Item -LiteralPath $_.FullName -Destination $vlcBundlePath -Recurse -Force
    }
    Copy-Item -LiteralPath $FfmpegPath -Destination (Join-Path $toolsBundlePath "ffmpeg.exe") -Force
    Copy-Item -LiteralPath $FfprobePath -Destination (Join-Path $toolsBundlePath "ffprobe.exe") -Force
    # Shared FFmpeg builds also need adjacent codec DLLs; static builds have none.
    foreach ($toolParent in @((Split-Path -Parent $FfmpegPath), (Split-Path -Parent $FfprobePath)) | Select-Object -Unique) {
        Get-ChildItem -LiteralPath $toolParent -File -Filter "*.dll" | ForEach-Object {
            Copy-Item -LiteralPath $_.FullName -Destination $toolsBundlePath -Force
        }
    }
    Copy-Item -LiteralPath (Join-Path $repoPath "LICENSE"), (Join-Path $repoPath "README.md") -Destination $appPath -Force
    Copy-Item -LiteralPath (Join-Path $VlcPath "COPYING.txt") -Destination (Join-Path $noticesPath "VLC-COPYING.txt") -Force
    & $FfmpegPath -L 2>&1 | Out-File -LiteralPath (Join-Path $noticesPath "FFmpeg-license.txt") -Encoding utf8
    & $FfmpegPath -version 2>&1 | Out-File -LiteralPath (Join-Path $noticesPath "FFmpeg-build.txt") -Encoding utf8
    @"
Voice Clipper's application code is MIT licensed; see LICENSE.
This local build contains third-party software with separate licenses.
VLC runtime notices are preserved in _internal/vlc/ and VLC-COPYING.txt.
VLC source: https://www.videolan.org/vlc/download-sources.html
FFmpeg license and configuration: FFmpeg-license.txt and FFmpeg-build.txt.
FFmpeg source: https://ffmpeg.org/download.html
Python package metadata/licenses are preserved under _internal/ where provided.
Before redistributing a build, provide corresponding source and notices required
by the particular VLC/FFmpeg/Qt binaries included in that build.
"@ | Set-Content -LiteralPath (Join-Path $noticesPath "README.txt") -Encoding utf8
    Write-Host "Built $appPath"
    Write-Host "Distribute the entire Voice Clipper folder, not the .exe alone."
} finally {
    Pop-Location
}

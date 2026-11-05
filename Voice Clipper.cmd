@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\pythonw.exe" (
    echo Voice Clipper needs its local Python environment.
    echo Run: powershell -NoProfile -ExecutionPolicy Bypass -File scripts\setup-windows.ps1
    pause
    exit /b 1
)
".venv\Scripts\python.exe" -c "import PySide6, vlc" >nul 2>&1
if errorlevel 1 (
    echo GUI dependencies or the VLC runtime are unavailable.
    echo Install matching 64-bit VLC and run scripts\setup-windows.ps1 again.
    pause
    exit /b 1
)
start "Voice Clipper" ".venv\Scripts\pythonw.exe" "voice_clipper_gui.py"

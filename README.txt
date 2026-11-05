VOICE CLIPPER
=============

Watch -> mark -> replay -> export.

TWO WAYS TO START
Just Listen: capture any Windows audio, even while this app is minimized.
Open video: watch a local file in the embedded VLC player and mark source clips.
Both buttons are highlighted at the top, with Just Listen on the left.

PACKAGED WINDOWS BUILD
Extract the complete Voice Clipper folder and open Voice Clipper.exe.
Keep its _internal folder beside the executable.

RUN FROM SOURCE
Install Python 3.11+, matching-architecture VLC, and ffmpeg/ffprobe on PATH.
In this folder run:
  powershell -NoProfile -ExecutionPolicy Bypass -File scripts/setup-windows.ps1
Then double-click Voice Clipper.cmd.

LOCAL VIDEO WORKFLOW
Open a file, play it, mark a line's start/end or save the previous few seconds.
Replay and adjust clips, add transcript/speaker/language/translation, and review.
Save the project and export WAV files with CSV/JSONL and Anki TSV metadata.
The source video is preserved; local-file export extracts its selected audio
track rather than recording desktop sounds or changes from playback speed.

LIVE SYSTEM AUDIO
Choose Just Listen for browsers and other players. It captures every application
playing through the selected output device. Check the meter, then use Minimize
to tray to keep clipping in the background. B starts/stops a clip; Ctrl+Shift+B
saves the previous 10 seconds. F7 discards the latest; Ctrl+Shift+Z restores it.
Settings lets you change these keys and the recordings folder.
The CLI is also available:
  .venv\Scripts\python.exe voice_clipper.py --help

Optional vocal separation remains in isolate_vocals.py and keeps originals.
See README.md for installation, development, export, licensing and troubleshooting.

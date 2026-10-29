# Voice Clipper

Capture dialogue and other desktop audio on Windows with global hotkeys. A rolling buffer includes audio from before you press the capture key, helping preserve the beginning of a line.

## Setup

Install Python and the recording dependencies:

```powershell
python -m pip install -r requirements.txt
python voice_clipper.py
```

Play audio in your preferred player, then use:

| Key | Action |
| --- | --- |
| B | Start or stop a clip |
| F7 | Delete the most recently saved clip from this session |
| Ctrl+Shift+Q | Quit and save a clip in progress |

Clips are saved beside the script in `voice_clips/` as numbered, mono, 48 kHz, 16-bit WAV files. Numbering resumes after the highest existing clip. Defaults include 1.5 seconds of pre-roll, 0.5 seconds of post-roll, and a 10-minute automatic stop. Change the settings and hotkeys near the top of `voice_clipper.py` as needed.

## Optional vocal separation

```powershell
python -m pip install -r requirements-vocals.txt
python isolate_vocals.py
```

The helper runs Demucs in batches and saves vocal estimates to `voice_clips_clean/`, skipping existing outputs. Separation may retain background sounds or introduce artifacts. A CUDA-enabled PyTorch installation enables compatible NVIDIA GPUs; otherwise processing uses the CPU. See the [PyTorch installation selector](https://pytorch.org/get-started/locally/) for the appropriate installation command. Demucs downloads its model on first use and may require FFmpeg for audio decoding.

## Troubleshooting

- This recorder targets Windows WASAPI loopback and captures system output, including other apps playing audio.
- If you change the default output device, restart the recorder.
- If the player runs as administrator, the recorder may need the same permissions for hotkeys.
- Near-silence warnings can indicate the wrong loopback device; inspect `find_loopback()` if needed.
- Hotkeys are global while the program runs; choose another capture key if B conflicts with your workflow.

Recorded audio and generated files are excluded from this repository. Use recordings only where you have appropriate rights and permissions.

## License

The source code is available under the [MIT license](LICENSE). This license does not grant rights to third-party audio or model weights.

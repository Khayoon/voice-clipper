# Voice Clipper — collect voice AI training clips while watching VLC

Build a collection of speech clips for voice AI datasets while you watch or listen. Start Voice Clipper, play your material in VLC, and tap **B** at the beginning and end of a line. Keep watching in full screen: each clip is saved automatically as a numbered WAV file.

Designed for collecting speech examples for text-to-speech (TTS), voice conversion, and other voice AI projects. It captures Windows desktop audio through WASAPI loopback, so it also works with other players and browsers.

## What it does

- Global hotkeys let you clip dialogue without switching away from the player.
- A rolling 1.5-second pre-roll buffer helps catch the beginning of a line when you press the key slightly late.
- A 0.5-second post-roll helps preserve the end of the line.
- Numbered WAV files let you collect clips across multiple sessions.
- An optional Demucs helper estimates vocals separately from background music and effects.

This is a clip collection tool. It does not train a model, transcribe speech, identify speakers, or automatically produce a finished training dataset.

## Setup

On Windows, install Python and Git, then open PowerShell:

```powershell
git clone https://github.com/Khayoon/voice-clipper.git
cd voice-clipper
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe voice_clipper.py
```

If you already have the dependencies installed in your current Python environment, you can also run:

```powershell
python voice_clipper.py
```

Play audio in your preferred player, then use:

| Key | Action |
| --- | --- |
| B | Start or stop a clip |
| F7 | Delete the most recently saved clip from this session |
| Ctrl+Shift+Q | Quit and save a clip in progress |

Clips are saved beside the script in `voice_clips/` as numbered, mono, 48 kHz, 16-bit WAV files. Numbering resumes after the highest existing clip. Defaults include 1.5 seconds of pre-roll, 0.5 seconds of post-roll, and a 10-minute automatic stop. Change the settings and hotkeys near the top of `voice_clipper.py` as needed.

## Example: collect a speaker's lines in VLC

1. Run Voice Clipper and check the output device shown in the terminal.
2. Open your recording or video in VLC and start playback.
3. When the speaker begins a useful line, tap **B**. Tap **B** again when the line ends, then allow the short post-roll to finish.
4. Continue watching and repeat for each line. To discard the last saved clip, press **F7**.
5. Press **Ctrl+Shift+Q** when finished. Review the WAV files in `voice_clips/`.

For example, a line you capture is saved as `voice_clips/clip_0001.wav`; the next becomes `clip_0002.wav`. The recorder captures everything playing through the selected output, so silence other apps and avoid overlapping speakers where possible.

Before training, listen to your clips, remove unwanted speech and artifacts, and prepare transcripts, speaker labels, and the sample rate required by your chosen trainer. Keep separate collections for different speakers; the recorder itself does not label or separate speakers. Use recordings you have permission to use for voice training.

## Optional vocal separation

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-vocals.txt
.\.venv\Scripts\python.exe isolate_vocals.py
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

voice_clipper - grab dialogue clips from a movie without leaving full screen
============================================================================

ONE-TIME SETUP
    pip install soundcard soundfile keyboard numpy

    Optional, for stripping music/effects off the clips afterwards:
    pip install demucs soxr
    (install the CUDA build of torch first so it uses the 3060:
     https://pytorch.org/get-started/locally/)

DAILY USE
    python voice_clipper.py

    Then go watch the movie full screen in VLC / MPC-HC / a browser.

        B            character starts talking -> press it
        B            character stops -> press it again, clip is saved
        F7            that one was bad -> deletes it
        Ctrl+Shift+Q  quit

    Clips land in voice_clips\clip_0001.wav, clip_0002.wav, ...
    Numbering resumes where you left off, so relaunching never overwrites.

    Because of the rolling pre-roll buffer you can press B slightly LATE and
    still get the whole line - it keeps the 1.5 seconds before your keypress.
    Tune PRE_ROLL / POST_ROLL at the top of the script if you want more.

CLEANING THEM UP
    python isolate_vocals.py

    Runs every new clip through Demucs and writes voice-only versions into
    voice_clips_clean\. Rerunnable - it skips anything already done.

GOTCHAS
    - If your player runs as administrator, run the script as administrator
      too, or Windows will not deliver the hotkey to it.
    - It records the default output device. If you switch headphones/speakers
      mid-session, restart the script.
    - "near silence" warnings mean it is listening to the wrong device.
    - Voice models trained on a real actor's performance are a consent and
      copyright question, not just a technical one. Worth knowing before you
      publish anything made with the dataset.


"""Headless Windows loopback recorder. Use --help for settings.

Global shortcuts fire on key release, so holding a key does not repeatedly
start and stop recording. The VLC GUI adds source-file marking and review.
"""

from __future__ import annotations
import argparse
import json
from pathlib import Path
import sys
import threading
from voiceclipper.capture import CaptureConfig, CaptureEngine

DEFAULT_KEYS = {
    "toggle": "b",
    "discard": "f7",
    "restore": "shift+f7",
    "last": "f8",
    "quit": "ctrl+shift+q",
}


def parse_settings(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, help="Read a JSON configuration file.")
    parser.add_argument(
        "--write-config", type=Path, help="Save settings as JSON and exit."
    )
    parser.add_argument(
        "--list-devices", action="store_true", help="List WASAPI loopback IDs and exit."
    )
    parser.add_argument("--device", help="Exact loopback ID shown by --list-devices.")
    parser.add_argument("--output", type=Path, help="Folder for captured WAVs.")
    parser.add_argument("--pre-roll", type=float)
    parser.add_argument("--post-roll", type=float)
    parser.add_argument(
        "--history", type=float, help="Rolling history duration, seconds."
    )
    parser.add_argument(
        "--last", type=float, help="Duration captured by the retrospective shortcut."
    )
    parser.add_argument("--max-clip", type=float)
    for key in DEFAULT_KEYS:
        parser.add_argument(f"--{key}-key", help=f"Global {key} shortcut.")
    args = parser.parse_args(argv)
    settings = {}
    if args.config:
        try:
            settings = json.loads(args.config.read_text(encoding="utf-8-sig"))
            if not isinstance(settings, dict):
                raise ValueError("The configuration must be a JSON object.")
        except (OSError, ValueError) as exc:
            parser.error(f"Cannot read configuration: {exc}")
    defaults = {
        "output_dir": str(Path(__file__).resolve().parent / "voice_clips"),
        "pre_roll": 1.5,
        "post_roll": 0.5,
        "history_seconds": 30.0,
        "max_clip": 600.0,
        "last_seconds": 10.0,
        "device": None,
    }
    defaults.update({key: settings[key] for key in defaults if key in settings})
    mapping = {
        "output": "output_dir",
        "pre_roll": "pre_roll",
        "post_roll": "post_roll",
        "history": "history_seconds",
        "last": "last_seconds",
        "max_clip": "max_clip",
        "device": "device",
    }
    for argument, setting in mapping.items():
        value = getattr(args, argument)
        if value is not None:
            defaults[setting] = str(value) if isinstance(value, Path) else value
    if not isinstance(settings.get("hotkeys", {}), dict):
        parser.error("hotkeys must be a JSON object.")
    keys = {**DEFAULT_KEYS, **settings.get("hotkeys", {})}
    for key in DEFAULT_KEYS:
        value = getattr(args, f"{key}_key")
        if value is not None:
            keys[key] = value
    if any(not isinstance(value, str) or not value.strip() for value in keys.values()):
        parser.error("Hotkeys must be non-empty strings.")
    if len({value.lower().strip() for value in keys.values()}) != len(keys):
        parser.error("Each action needs a different hotkey.")
    defaults["hotkeys"] = keys
    try:
        config = CaptureConfig(
            **{
                k: defaults[k]
                for k in (
                    "output_dir",
                    "pre_roll",
                    "post_roll",
                    "history_seconds",
                    "max_clip",
                )
            }
        )
        if not 0 < defaults["last_seconds"] <= config.history_seconds:
            raise ValueError(
                "Last-seconds duration must be positive and no longer than history."
            )
    except (TypeError, ValueError) as exc:
        parser.error(str(exc))
    return args, defaults, config


def main(argv=None):
    args, settings, config = parse_settings(argv)
    if args.write_config:
        args.write_config.parent.mkdir(parents=True, exist_ok=True)
        args.write_config.write_text(
            json.dumps(settings, indent=2) + "\n", encoding="utf-8"
        )
        print(f"Settings saved to {args.write_config}")
        return 0
    if args.list_devices:
        try:
            for device in CaptureEngine.list_devices():
                print(
                    f"{device['name']}{' [default]' if device.get('default') else ''}\n  {device['id']}"
                )
            return 0
        except Exception as exc:
            print(f"Cannot list output devices: {exc}", file=sys.stderr)
            return 1
    try:
        import keyboard
    except ImportError:
        print(
            "Install dependencies: python -m pip install -r requirements.txt",
            file=sys.stderr,
        )
        return 1
    finished = threading.Event()
    failed = threading.Event()

    def event(data):
        kind = data["type"]
        if kind == "ready":
            print(f"Listening on: {data['device']}", flush=True)
        elif kind == "state" and data["state"] != "stopped":
            print(f"[{data['state']}]", flush=True)
        elif kind == "saved":
            print(
                f"Saved {Path(data['path']).name}  {data['duration']:.2f}s", flush=True
            )
            if data.get("near_silence"):
                print(
                    "  Near silence: check the selected output device and playback volume.",
                    flush=True,
                )
        elif kind in ("discarded", "restored"):
            print(f"{kind.capitalize()}: {data['clip_id']}", flush=True)
        elif kind == "error":
            print(data["message"], file=sys.stderr, flush=True)
            if data.get("fatal"):
                failed.set()
                finished.set()
        elif kind in ("message", "warning"):
            print(data["message"], flush=True)

    engine = CaptureEngine(config, event)
    keys = settings["hotkeys"]
    actions = {
        "toggle": engine.toggle,
        "discard": engine.discard_latest,
        "restore": engine.restore_last,
        "last": lambda: engine.save_last(settings["last_seconds"]),
        "quit": finished.set,
    }
    hooks = []
    try:
        for action, callback in actions.items():
            hooks.append(
                keyboard.add_hotkey(keys[action], callback, trigger_on_release=True)
            )
        print("Voice Clipper - Windows system audio")
        for action in actions:
            print(f"  {keys[action]:18} {action}")
        print(f"  Retrospective duration: {settings['last_seconds']:g}s")
        print(f"  Saving to {config.output_dir}")
        engine.start(settings["device"])
        finished.wait()
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        print(f"Could not start recorder: {exc}", file=sys.stderr)
        failed.set()
    finally:
        for hook in hooks:
            keyboard.remove_hotkey(hook)
        if not engine.stop():
            failed.set()
    print("Stopped. Check any reported save errors before closing.")
    return 1 if failed.is_set() else 0


if __name__ == "__main__":
    raise SystemExit(main())

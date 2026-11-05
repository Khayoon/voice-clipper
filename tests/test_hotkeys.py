"""Hotkey edge cases and listener lifecycle without sending OS input."""

import queue
import threading

import pytest

from voiceclipper.hotkeys import (
    CTRL,
    SHIFT,
    GlobalHotkeys,
    Hotkey,
    HotkeyMatcher,
    parse_hotkey,
)


@pytest.mark.parametrize(
    "text,expected",
    [
        ("b", Hotkey(0, 0x42)),
        ("CTRL+Shift+B", Hotkey(CTRL | SHIFT, 0x42)),
        ("f7", Hotkey(0, 0x76)),
        ("Ctrl+Shift+Z", Hotkey(CTRL | SHIFT, 0x5A)),
        ("escape", Hotkey(0, 0x1B)),
        ("alt+space", Hotkey(1, 0x20)),
    ],
)
def test_shortcut_parsing(text, expected):
    assert parse_hotkey(text) == expected


@pytest.mark.parametrize(
    "text", ["", "ctrl", "ctrl+ctrl+b", "b+c", "ctrl++", "a,b", "f25"]
)
def test_unsupported_or_ambiguous_shortcuts_are_rejected(text):
    with pytest.raises(ValueError):
        parse_hotkey(text)


def test_release_only_and_hold_repeat_never_retoggle():
    matcher = HotkeyMatcher({"toggle": "b"})
    assert matcher.feed(0x42, True) is None
    for _ in range(20):
        assert matcher.feed(0x42, True) is None
    assert matcher.feed(0x42, False) == "toggle"
    assert matcher.feed(0x42, False) is None
    assert matcher.feed(0x42, True) is None
    assert matcher.feed(0x42, False) == "toggle"


@pytest.mark.parametrize("control,shift", [(0xA2, 0xA0), (0xA3, 0xA1), (0x11, 0x10)])
def test_modified_b_does_not_trigger_plain_b(control, shift):
    matcher = HotkeyMatcher({"toggle": "b", "last": "ctrl+shift+b"})
    matcher.feed(control, True)
    matcher.feed(shift, True)
    matcher.feed(0x42, True)
    assert matcher.feed(0x42, False) == "last"
    matcher.feed(shift, False)
    matcher.feed(control, False)
    matcher.feed(0x42, True)
    assert matcher.feed(0x42, False) == "toggle"


def test_start_with_held_modifier_or_capture_key_is_not_a_new_press():
    matcher = HotkeyMatcher({"toggle": "b", "last": "ctrl+shift+b"})
    matcher.seed_pressed([0xA2, 0xA0, 0x42])
    assert matcher.feed(0x42, False) is None
    matcher.feed(0xA2, False)
    matcher.feed(0xA0, False)
    matcher.feed(0x42, True)
    assert matcher.feed(0x42, False) == "toggle"


def test_extra_modifiers_prevent_unintended_actions_and_unbound_text_is_not_tracked():
    matcher = HotkeyMatcher({"toggle": "b"})
    matcher.feed(0xA4, True)
    matcher.feed(0x42, True)
    assert matcher.feed(0x42, False) is None
    matcher.feed(0xA4, False)
    matcher.feed(0x58, True)
    assert 0x58 not in matcher._pressed


def test_duplicate_normalized_shortcuts_are_rejected():
    with pytest.raises(ValueError):
        HotkeyMatcher({"a": "ctrl+b", "b": "Control+B"})


class FakeAPI:
    def __init__(self, fail_install=False):
        self.messages = queue.Queue()
        self.fail_install = fail_install
        self.installed = False
        self.uninstalled = False
        self.handler = None
        self.thread_id = None

    def prepare_queue(self):
        self.thread_id = threading.get_ident()
        return self.thread_id

    def pressed_keys(self, keys):
        return set()

    def install(self, on_key, on_error):
        if self.fail_install:
            raise OSError("access denied")
        self.handler = on_key
        self.installed = True

    def pump(self):
        while True:
            item = self.messages.get()
            if item == "quit":
                return
            if item == "error":
                raise OSError("message pump failed")
            if item == "unrelated":
                continue
            if isinstance(item, tuple) and item[0] == "pulse":
                item[1].set()
                continue
            self.handler(*item)

    def wake_stop(self, thread_id):
        assert thread_id == self.thread_id
        self.messages.put("quit")

    def uninstall(self):
        self.uninstalled = True
        self.installed = False

    def pulse(self, thread_id, timeout=1):
        assert thread_id == self.thread_id
        answered = threading.Event()
        self.messages.put(("pulse", answered))
        if not answered.wait(timeout):
            raise OSError("Message pump unresponsive")


def test_checked_start_background_callback_message_pump_and_stop():
    api = FakeAPI()
    fired = threading.Event()
    observed = []

    def action(name):
        observed.append((name, threading.get_ident()))
        fired.set()

    listener = GlobalHotkeys({"toggle": "b"}, action, api=api)
    try:
        listener.start()
        assert listener.running and api.installed
        api.messages.put("unrelated")
        api.messages.put((0x42, True))
        api.messages.put((0x42, False))
        assert fired.wait(2)
        assert observed == [("toggle", api.thread_id)]
        assert observed[0][1] != threading.get_ident()
    finally:
        listener.stop()
    assert not listener.running and api.uninstalled
    api.handler(0x42, True)
    api.handler(0x42, False)
    assert len(observed) == 1


def test_failed_hook_install_is_not_reported_as_success():
    listener = GlobalHotkeys({"toggle": "b"}, lambda action: None, api=FakeAPI(True))
    with pytest.raises(OSError, match="access denied"):
        listener.start()
    assert not listener.running
    listener.stop()


def test_pump_failure_clears_readiness_and_reports_it():
    api = FakeAPI()
    failed = threading.Event()
    errors = []

    def error(message):
        errors.append(message)
        failed.set()

    listener = GlobalHotkeys({"toggle": "b"}, lambda action: None, error, api=api)
    listener.start()
    api.messages.put("error")
    assert failed.wait(2)
    listener.stop()
    assert not listener.running and api.uninstalled
    assert "message pump failed" in errors[0]


def test_listener_can_restart_with_fresh_owned_queue():
    first = FakeAPI()
    listener = GlobalHotkeys({"toggle": "b"}, lambda _: None, api=first)
    listener.start()
    listener.stop()
    second = FakeAPI()
    listener.api = second
    listener.start()
    listener.stop()
    assert first.uninstalled and second.uninstalled and not listener.running

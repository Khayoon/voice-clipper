"""Checked, non-suppressing Windows hotkeys with an independent message pump.

Only the configured keys and modifier state are tracked. Key input is never
logged or swallowed. Callbacks must be fast; the GUI supplies queued Qt signals.
"""

from __future__ import annotations

from dataclasses import dataclass
import sys
import threading

ALT, CTRL, SHIFT, WIN = 1, 2, 4, 8
MODIFIER_KEYS = {
    0x10: SHIFT,
    0xA0: SHIFT,
    0xA1: SHIFT,
    0x11: CTRL,
    0xA2: CTRL,
    0xA3: CTRL,
    0x12: ALT,
    0xA4: ALT,
    0xA5: ALT,
    0x5B: WIN,
    0x5C: WIN,
}
MODIFIER_NAMES = {
    "ctrl": CTRL,
    "control": CTRL,
    "shift": SHIFT,
    "alt": ALT,
    "win": WIN,
    "windows": WIN,
    "meta": WIN,
}
NAMED_KEYS = {
    "backspace": 0x08,
    "tab": 0x09,
    "enter": 0x0D,
    "return": 0x0D,
    "pause": 0x13,
    "caps lock": 0x14,
    "capslock": 0x14,
    "esc": 0x1B,
    "escape": 0x1B,
    "space": 0x20,
    "spacebar": 0x20,
    "page up": 0x21,
    "pageup": 0x21,
    "pgup": 0x21,
    "page down": 0x22,
    "pagedown": 0x22,
    "pgdown": 0x22,
    "end": 0x23,
    "home": 0x24,
    "left": 0x25,
    "up": 0x26,
    "right": 0x27,
    "down": 0x28,
    "print screen": 0x2C,
    "printscreen": 0x2C,
    "insert": 0x2D,
    "ins": 0x2D,
    "delete": 0x2E,
    "del": 0x2E,
    "numlock": 0x90,
    "num lock": 0x90,
    "scrolllock": 0x91,
    "scroll lock": 0x91,
    "semicolon": 0xBA,
    "plus": 0xBB,
    "comma": 0xBC,
    "minus": 0xBD,
    "period": 0xBE,
    "slash": 0xBF,
    "backtick": 0xC0,
    "left bracket": 0xDB,
    "backslash": 0xDC,
    "right bracket": 0xDD,
    "quote": 0xDE,
}


@dataclass(frozen=True)
class Hotkey:
    modifiers: int
    vk: int


def parse_hotkey(text: str) -> Hotkey:
    if not isinstance(text, str) or not text.strip():
        raise ValueError("Choose a nonempty shortcut.")
    parts = [part.strip().casefold() for part in text.split("+")]
    if any(not part for part in parts) or "," in text:
        raise ValueError(
            "Use one key with optional Ctrl, Shift or Alt, such as B or Ctrl+Shift+B."
        )
    modifiers = 0
    keys = []
    for part in parts:
        if part in MODIFIER_NAMES:
            flag = MODIFIER_NAMES[part]
            if modifiers & flag:
                raise ValueError("A modifier may occur only once in a shortcut.")
            modifiers |= flag
        else:
            keys.append(part)
    if len(keys) != 1:
        raise ValueError("A shortcut needs exactly one key, with optional modifiers.")
    key = keys[0]
    if len(key) == 1 and key.isascii() and key.isalnum():
        vk = ord(key.upper())
    elif key.startswith("f") and key[1:].isdigit() and 1 <= int(key[1:]) <= 24:
        vk = 0x70 + int(key[1:]) - 1
    elif key in NAMED_KEYS:
        vk = NAMED_KEYS[key]
    else:
        raise ValueError(
            f"Unsupported shortcut key: {key}. Use a letter, number, function key or named key."
        )
    return Hotkey(modifiers, vk)


class HotkeyMatcher:
    """Exact modifiers, one activation on release, no auto-repeat toggles."""

    def __init__(self, bindings):
        self.bindings = {}
        for action, text in bindings.items():
            key = parse_hotkey(text)
            if key in self.bindings:
                raise ValueError(f"Duplicate shortcut: {text}")
            self.bindings[key] = action
        self.interesting_keys = set(MODIFIER_KEYS) | {key.vk for key in self.bindings}
        self._pressed = set()
        self._armed = {}

    def seed_pressed(self, keys):
        self._pressed = self.interesting_keys.intersection(keys)
        self._armed.clear()

    def feed(self, vk, down):
        if vk not in self.interesting_keys:
            return None
        if down:
            if vk in self._pressed:
                return None
            self._pressed.add(vk)
            if vk in MODIFIER_KEYS:
                return None
            modifiers = 0
            for key in self._pressed:
                modifiers |= MODIFIER_KEYS.get(key, 0)
            action = self.bindings.get(Hotkey(modifiers, vk))
            if action is not None:
                self._armed[vk] = action
            return None
        self._pressed.discard(vk)
        return self._armed.pop(vk, None)


class Win32KeyboardAPI:
    """The native boundary, kept small enough to inspect and replace in tests."""

    WM_QUIT = 0x12

    def __init__(self):
        if sys.platform != "win32":
            raise OSError("Global system-audio shortcuts require Windows.")
        import ctypes
        from ctypes import wintypes

        self.ctypes = ctypes
        self.wintypes = wintypes
        self.user = ctypes.WinDLL("user32", use_last_error=True)
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        lresult = ctypes.c_ssize_t
        self.proc_type = ctypes.WINFUNCTYPE(
            lresult, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM
        )

        class KeyData(ctypes.Structure):
            _fields_ = [
                ("vkCode", wintypes.DWORD),
                ("scanCode", wintypes.DWORD),
                ("flags", wintypes.DWORD),
                ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.c_size_t),
            ]

        self.KeyData = KeyData
        self.user.SetWindowsHookExW.argtypes = [
            ctypes.c_int,
            self.proc_type,
            wintypes.HINSTANCE,
            wintypes.DWORD,
        ]
        self.user.SetWindowsHookExW.restype = wintypes.HHOOK
        self.user.CallNextHookEx.argtypes = [
            wintypes.HHOOK,
            ctypes.c_int,
            wintypes.WPARAM,
            wintypes.LPARAM,
        ]
        self.user.CallNextHookEx.restype = lresult
        self.user.UnhookWindowsHookEx.argtypes = [wintypes.HHOOK]
        self.user.UnhookWindowsHookEx.restype = wintypes.BOOL
        self.user.GetMessageW.argtypes = [
            ctypes.POINTER(wintypes.MSG),
            wintypes.HWND,
            wintypes.UINT,
            wintypes.UINT,
        ]
        self.user.GetMessageW.restype = ctypes.c_int
        self.user.PeekMessageW.argtypes = [
            ctypes.POINTER(wintypes.MSG),
            wintypes.HWND,
            wintypes.UINT,
            wintypes.UINT,
            wintypes.UINT,
        ]
        self.user.PeekMessageW.restype = wintypes.BOOL
        self.user.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
        self.user.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
        self.user.DispatchMessageW.restype = lresult
        self.user.PostThreadMessageW.argtypes = [
            wintypes.DWORD,
            wintypes.UINT,
            wintypes.WPARAM,
            wintypes.LPARAM,
        ]
        self.user.PostThreadMessageW.restype = wintypes.BOOL
        self.user.GetAsyncKeyState.argtypes = [ctypes.c_int]
        self.user.GetAsyncKeyState.restype = ctypes.c_short
        self.kernel.GetCurrentThreadId.restype = wintypes.DWORD
        self.kernel.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
        self.kernel.GetModuleHandleW.restype = wintypes.HMODULE
        self.handle = None
        self._callback = None
        self.message = wintypes.MSG()
        self._pulse_event = threading.Event()

    def prepare_queue(self):
        # Allocate valid message storage and create the thread queue before Stop
        # can post WM_QUIT. Low-level hooks are serviced by this same thread.
        self.user.PeekMessageW(self.ctypes.byref(self.message), None, 0, 0, 0)
        return int(self.kernel.GetCurrentThreadId())

    def pressed_keys(self, keys):
        # Seed side-specific modifiers only. Seeding both CTRL and LCTRL would
        # leave a phantom CTRL held after the real LCTRL key-up arrives.
        return {
            vk
            for vk in keys
            if vk not in (0x10, 0x11, 0x12) and self.user.GetAsyncKeyState(vk) & 0x8000
        }

    def install(self, on_key, on_error):
        def callback(code, message, pointer):
            try:
                if code >= 0 and pointer and message in (0x100, 0x101, 0x104, 0x105):
                    data = self.ctypes.cast(
                        pointer, self.ctypes.POINTER(self.KeyData)
                    ).contents
                    on_key(int(data.vkCode), message in (0x100, 0x104))
            except Exception as exc:
                on_error(f"Global hotkey callback failed: {exc}")
            # Always let other applications receive the key, including B.
            return self.user.CallNextHookEx(self.handle, code, message, pointer)

        self._callback = self.proc_type(callback)
        module = self.kernel.GetModuleHandleW(None)
        self.handle = self.user.SetWindowsHookExW(13, self._callback, module, 0)
        if not self.handle:
            self._callback = None
            raise self.ctypes.WinError(self.ctypes.get_last_error())

    def pump(self):
        while True:
            result = self.user.GetMessageW(self.ctypes.byref(self.message), None, 0, 0)
            if result == 0:
                return
            if result < 0:
                raise self.ctypes.WinError(self.ctypes.get_last_error())
            if self.message.message == 0x8001:
                self._pulse_event.set()
                continue
            self.user.TranslateMessage(self.ctypes.byref(self.message))
            self.user.DispatchMessageW(self.ctypes.byref(self.message))

    def wake_stop(self, thread_id):
        if not self.user.PostThreadMessageW(thread_id, self.WM_QUIT, 0, 0):
            raise self.ctypes.WinError(self.ctypes.get_last_error())

    def pulse(self, thread_id, timeout=1):
        self._pulse_event.clear()
        if not self.user.PostThreadMessageW(thread_id, 0x8001, 0, 0):
            raise self.ctypes.WinError(self.ctypes.get_last_error())
        if not self._pulse_event.wait(timeout):
            raise OSError("The Windows global hotkey message pump did not respond.")

    def uninstall(self):
        if self.handle:
            if not self.user.UnhookWindowsHookEx(self.handle):
                error = self.ctypes.get_last_error()
                if error != 1404:  # Windows may already have removed the hook.
                    raise self.ctypes.WinError(error)
            self.handle = None
        self._callback = None


class GlobalHotkeys:
    """One owned hook thread with checked startup and graceful teardown."""

    def __init__(self, bindings, on_action, on_error=lambda message: None, *, api=None):
        self.matcher = HotkeyMatcher(bindings)
        self.on_action = on_action
        self.on_error = on_error
        self.api = api
        self._thread = None
        self._thread_id = None
        self._started = threading.Event()
        self._ready = threading.Event()
        self._stop_requested = threading.Event()
        self._startup_error = None

    @property
    def running(self):
        return bool(self._ready.is_set() and self._thread and self._thread.is_alive())

    def start(self, timeout=3):
        if self._thread and self._thread.is_alive():
            raise RuntimeError("Global hotkeys are already running.")
        self._started.clear()
        self._ready.clear()
        self._stop_requested.clear()
        self._startup_error = None
        self._thread_id = None
        self._thread = threading.Thread(
            target=self._run, name="voice-clipper-hotkeys", daemon=True
        )
        self._thread.start()
        if not self._started.wait(timeout):
            self.stop(timeout=timeout)
            raise OSError(
                "Windows did not finish installing the global hotkey listener."
            )
        if self._startup_error:
            self._thread.join(timeout)
            raise OSError(
                f"Windows could not install global hotkeys: {self._startup_error}"
            )
        if not self.running:
            raise OSError("The Windows global hotkey listener stopped during startup.")
        try:
            self.api.pulse(self._thread_id, timeout)
        except Exception:
            self.stop(timeout=timeout)
            raise

    def _key(self, vk, down):
        if self._stop_requested.is_set() or not self._ready.is_set():
            return
        action = self.matcher.feed(vk, down)
        if action is not None:
            self.on_action(action)

    def _report_error(self, message):
        try:
            self.on_error(message)
        except Exception:
            pass

    def _run(self):
        api = None
        installed = False
        try:
            api = self.api or Win32KeyboardAPI()
            self.api = api
            self._thread_id = api.prepare_queue()
            self.matcher.seed_pressed(api.pressed_keys(self.matcher.interesting_keys))
            api.install(self._key, self._report_error)
            installed = True
            if not self._stop_requested.is_set():
                self._ready.set()
            self._started.set()
            if not self._stop_requested.is_set():
                api.pump()
        except Exception as exc:
            if not self._started.is_set():
                self._startup_error = exc
            elif not self._stop_requested.is_set():
                self._report_error(f"Windows global hotkeys stopped: {exc}")
        finally:
            self._ready.clear()
            self._started.set()
            if installed:
                try:
                    api.uninstall()
                except Exception as exc:
                    self._report_error(
                        f"Could not remove the global hotkey listener: {exc}"
                    )

    def stop(self, timeout=3):
        self._stop_requested.set()
        thread = self._thread
        if not thread or not thread.is_alive():
            self._ready.clear()
            return
        if self._thread_id is not None and self.api:
            try:
                self.api.wake_stop(self._thread_id)
            except OSError:
                if thread.is_alive():
                    raise
        if thread is not threading.current_thread():
            thread.join(timeout)
            if thread.is_alive():
                raise OSError("The Windows global hotkey listener has not stopped yet.")
        self._ready.clear()

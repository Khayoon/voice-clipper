"""A small embedded libVLC adapter. No desktop window automation."""

from __future__ import annotations

import sys
from .runtime import prepare_vlc


class VlcBackend:
    def __init__(self, video_widget):
        prepare_vlc()
        try:
            import vlc

            self.vlc = vlc
            self.instance = vlc.Instance(
                "--no-video-title-show",
                "--no-keyboard-events",
                "--no-mouse-events",
                "--quiet",
            )
            if self.instance is None:
                raise RuntimeError("libVLC could not start")
            self.player = self.instance.media_player_new()
            handle = int(video_widget.winId())
            if sys.platform == "win32":
                self.player.set_hwnd(handle)
            elif sys.platform == "darwin":
                self.player.set_nsobject(handle)
            else:
                self.player.set_xwindow(handle)
            self.player.video_set_key_input(False)
            self.player.video_set_mouse_input(False)
            self.media = None
        except Exception as exc:
            raise RuntimeError(
                "VLC playback is unavailable. Install 64-bit VLC, or set VOICE_CLIPPER_VLC to its folder.\n"
                + str(exc)
            ) from exc

    def open(self, path):
        self.player.stop()
        if self.media is not None:
            self.media.release()
        self.media = self.instance.media_new(str(path))
        self.player.set_media(self.media)
        if self.player.play() == -1:
            raise RuntimeError("VLC could not open this media file")

    @property
    def time_ms(self):
        return max(0, self.player.get_time())

    @property
    def duration_ms(self):
        return max(0, self.player.get_length())

    @property
    def playing(self):
        return bool(self.player.is_playing())

    @property
    def failed(self):
        return self.player.get_state() == self.vlc.State.Error

    def toggle_pause(self):
        if self.player.get_state() in (self.vlc.State.Ended, self.vlc.State.Stopped):
            self.player.play()
        else:
            self.player.pause()

    def pause(self):
        self.player.set_pause(1)

    def seek(self, time_ms):
        self.player.set_time(max(0, int(time_ms)))

    def set_volume(self, value):
        self.player.audio_set_volume(int(value))

    def set_rate(self, value):
        return self.player.set_rate(float(value))

    def set_audio_track(self, ordinal):
        tracks = [
            (i, name)
            for i, name in (self.player.audio_get_track_description() or [])
            if i != -1
        ]
        if 0 <= ordinal < len(tracks):
            return self.player.audio_set_track(tracks[ordinal][0])
        return -1

    def stop(self):
        self.player.stop()

    def close(self):
        self.player.stop()
        self.player.release()
        if self.media is not None:
            self.media.release()
        self.instance.release()

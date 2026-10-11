# mpv_gl_area.py
#
# Copyright 2025 Diego Povliuk
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
#

import ctypes
import logging
import os
import threading

import gi
import mpv

from .platform_compat import (
    GL_FRAMEBUFFER_BINDING,
    RENDER_BLOCK_FOR_TARGET_TIME,
    get_display_param,
    gl_get_integerv,
    gl_get_proc_address,
    trace,
)

gi.require_version("GLib", "2.0")
gi.require_version("Gtk", "4.0")
from gi.repository import GLib, Gtk

logger = logging.getLogger(__name__)

DISPLAY_PARAM = get_display_param()

# python-mpv has no binding for libmpv's hook API, only for the event it
# delivers. The function objects are shared with python-mpv, which never
# calls these two, so setting their signatures here is safe.
_hook_add = mpv.backend.mpv_hook_add
_hook_add.argtypes = [mpv.MpvHandle, ctypes.c_uint64, ctypes.c_char_p, ctypes.c_int]
_hook_add.restype = ctypes.c_int
_hook_continue = mpv.backend.mpv_hook_continue
_hook_continue.argtypes = [mpv.MpvHandle, ctypes.c_uint64]
_hook_continue.restype = ctypes.c_int

# Tells our on_preloaded hook's events apart from anyone else's.
_PRELOADED_HOOK = 0x6E656B6F


class BaseGLArea(Gtk.GLArea):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._ctx: mpv.MpvRenderContext | None = None
        self._mpv: mpv.MPV | None = None
        self._fbo = ctypes.c_int()
        self._render_queued = False
        self.connect("realize", self._on_realize)
        self.connect("render", self._on_render)

    def _setup_mpv_context(self, mpv_instance: mpv.MPV) -> mpv.MpvRenderContext | None:
        try:
            proc_address_fn = mpv.MpvGlGetProcAddressFn(
                lambda _inst, name: gl_get_proc_address(name)
            )
            ctx = mpv.MpvRenderContext(
                mpv_instance,
                "opengl",
                opengl_init_params={"get_proc_address": proc_address_fn},
                **DISPLAY_PARAM,
            )
            ctx.update_cb = self._on_mpv_update
            return ctx
        except Exception:
            logger.exception("BaseGLArea _setup_mpv_context failed")
            return None

    def _on_mpv_update(self):
        """mpv has a frame for us, so ask for a redraw.

        This runs on a thread of mpv's choosing while the core may be holding
        its locks, so it must not call back into mpv - a property read from
        here deadlocks against a property write on the main thread. Nothing
        but the redraw request belongs here.

        mpv calls this for the OSD and for state changes as well as frames,
        often several times before the main loop gets a turn; one request
        waiting is enough, since a render always draws the latest frame.
        """
        if self._render_queued:
            return
        self._render_queued = True
        GLib.idle_add(
            self._queue_render_once,
            priority=GLib.PRIORITY_HIGH_IDLE,  # type: ignore
        )

    def _queue_render_once(self):
        self._render_queued = False
        self.queue_render()
        return GLib.SOURCE_REMOVE

    def _on_realize(self, _area):
        raise NotImplementedError("Subclasses must implement _on_realize")

    def _on_render(self, _area, _context):
        try:
            gl_get_integerv(GL_FRAMEBUFFER_BINDING, self._fbo)
            assert self._ctx is not None
            self._ctx.render(
                flip_y=True,
                block_for_target_time=RENDER_BLOCK_FOR_TARGET_TIME,
                opengl_fbo={
                    "w": self.get_width() * self.props.scale_factor,
                    "h": self.get_height() * self.props.scale_factor,
                    "fbo": self._fbo.value,
                },
            )
        except Exception:
            logger.exception("ThumbPreviewGLArea _on_render failed")


class ThumbPreviewGLArea(BaseGLArea):
    def __init__(self, hwdec, **kwargs):
        super().__init__(**kwargs)
        self.set_auto_render(False)
        self._mpv = mpv.MPV(
            vo="libmpv",
            sub="no",
            audio="no",
            ao="null",
            hwdec=hwdec,
            ytdl=False,
            config=False,
            osc=False,
            terminal=False,
            load_scripts=False,
            msg_level="all=no",
            vd_lavc_threads=2,
            vd_lavc_fast=True,
            vd_lavc_skiploopfilter="all",
            vd_lavc_software_fallback=1,
            sws_scaler="fast-bilinear",
            demuxer_readahead_secs=0,
            demuxer_max_bytes="128KiB",
            hr_seek=False,
            pause=True,
            ovc="rawvideo",
            of="image2",
            ofopts="update=1",
            load_osd_console=False,
            load_stats_overlay=False,
            load_auto_profiles=False,
            really_quiet=True,
            dither=False,
            linear_downscaling=False,
            sigmoid_upscaling=False,
            hdr_compute_peak=False,
            allow_delayed_peak_detect=True,
            video_zoom=0.01,
        )
        self.connect("unrealize", self._on_unrealize)

        self._time = None
        self._is_seeking = False
        # Called once on the main loop when the file load_file opened has its
        # first frame; armed by that file's file-loaded, so a restart left
        # over from a seek in the last file cannot set it off early.
        self._on_loaded = None
        self._loaded_armed = False

        @self._mpv.property_observer("seeking")
        def on_seeking_change(_name, seeking):
            if not seeking:
                self._is_seeking = False
                self._flush_seek()

        @self._mpv.event_callback("file-loaded")
        def on_file_loaded(_event):
            self._loaded_armed = True

        @self._mpv.event_callback("playback-restart")
        def on_playback_restart(_event):
            if not self._loaded_armed:
                return
            callback, self._on_loaded = self._on_loaded, None
            self._loaded_armed = False
            if callback is not None:
                GLib.idle_add(
                    lambda: callback() and False,
                    priority=GLib.PRIORITY_HIGH_IDLE,  # type: ignore
                )

    def _on_realize(self, _area):
        try:
            self.make_current()
            assert self._mpv is not None
            self._ctx = self._setup_mpv_context(self._mpv)
        except Exception:
            logger.exception("ThumbPreviewGLArea _on_realize failed")

    def _on_unrealize(self, _area):
        try:
            self.make_current()
            assert self._ctx is not None
            self._ctx.free()
            assert self._mpv is not None
            self._mpv.terminate()
        except Exception:
            logger.exception("ThumbPreviewGLArea unrealize failed")
        finally:
            self._mpv = None
            self._ctx = None

    def load_file(self, path, start=None, on_loaded=None) -> bool:
        """Open *path*, from *start*, and call *on_loaded* on the main loop
        once its first frame is in."""
        try:
            assert self._mpv is not None
            # mpv hands back a name that is not valid UTF-8 as bytes, which
            # python-mpv's loadfile cannot take; bytes go through untouched.
            target = path if isinstance(path, bytes) else os.fsencode(path)
            # A seek still waiting was meant for the last file.
            self._time = None
            self._is_seeking = False
            self._loaded_armed = False
            self._on_loaded = on_loaded
            if start:
                self._mpv.command("loadfile", target, "replace", -1, f"start={start:.3f}")
            else:
                self._mpv.command("loadfile", target, "replace")
            return True
        except Exception:
            logger.exception("ThumbPreviewGLArea load_file failed")
            return False

    def seek(self, time):
        self._time = time
        self._flush_seek()

    def _flush_seek(self):
        try:
            if self._is_seeking or self._time is None:
                return

            time = self._time
            self._is_seeking = True
            self._time = None

            def done(error, _result):
                # Refused - the file is still opening, or gone. Nothing will
                # report seeking over, so stop waiting for it, and keep the
                # time for the next attempt unless a newer one has come.
                if error is not None:
                    self._is_seeking = False
                    if self._time is None:
                        self._time = time

            assert self._mpv is not None
            self._mpv.command_async("seek", time, "absolute+keyframes", callback=done)
        except Exception:
            logger.exception("ThumbPreviewGLArea _flush_seek failed")

    def stop(self):
        try:
            assert self._mpv is not None
            self._mpv.stop()
        except Exception:
            logger.exception("ThumbPreviewGLArea stop failed")


class VideoGLArea(BaseGLArea):
    def __init__(self, mpv_instance: mpv.MPV, **kwargs):
        super().__init__(**kwargs)
        self._mpv = mpv_instance
        # Set from mpv's event thread on playback-restart (NEKOPLAY_TRACE
        # only), so the trace can say when that frame reached the screen.
        self.trace_next_frame = False

        self._gate_lock = threading.Lock()
        self._gate_open = False
        self._held: list[int] = []
        self.holds_loads = self._hold_loads()

    def _hold_loads(self) -> bool:
        """Let mpv open a file before this area exists on screen.

        With vo=libmpv, mpv makes its video output as soon as it knows a
        file's tracks, and that fails outright - the file then plays without
        video - unless the render context already exists. The render context
        needs this area realized, and that needs the window shown, so a file
        could not be handed to mpv until all of that was done.

        An on_preloaded hook holds each load at the last point before the
        video output is made, and lets it go once the render context is up:
        reading the file's headers, finding its tracks and loading external
        subtitles all happen while the window is still being realized.
        Returns False if the hook could not be set, in which case nothing is
        held and loads must wait for the window as before.
        """
        try:
            handle = self._mpv._event_handle

            @self._mpv.event_callback(mpv.MpvEventID.HOOK)
            def on_hook(event):
                if event.reply_userdata != _PRELOADED_HOOK:
                    return
                hook_id = event.data.id
                with self._gate_lock:
                    if not self._gate_open:
                        self._held.append(hook_id)
                        trace("load held for the render context")
                        return
                _hook_continue(handle, hook_id)

            if _hook_add(handle, _PRELOADED_HOOK, b"on_preloaded", 0) < 0:
                raise RuntimeError("mpv_hook_add failed")
            return True
        except Exception:
            logger.exception("Could not hold loads for the render context")
            self._gate_open = True
            return False

    def release_loads(self):
        """Let every load through from now on, including any being held.
        Called once the render context is up, or failed to come up, and when
        the window closes - mpv waits on a held hook even to quit."""
        with self._gate_lock:
            self._gate_open = True
            held, self._held = self._held, []
        # python-mpv frees the handle once the core has shut down, and with
        # the core gone there is nothing left to release.
        if not held or self._mpv.core_shutdown:
            return
        for hook_id in held:
            try:
                _hook_continue(self._mpv._event_handle, hook_id)
            except Exception:
                logger.exception("Could not release a held load")

    def _on_render(self, area, context):
        super()._on_render(area, context)
        if self.trace_next_frame:
            self.trace_next_frame = False
            trace("frame drawn")

    def _on_realize(self, _area):
        trace("GL area realize")
        try:
            self.make_current()
            trace("GL context current")
            self._ctx = self._setup_mpv_context(self._mpv)
            trace("mpv render context up")
        finally:
            self.release_loads()

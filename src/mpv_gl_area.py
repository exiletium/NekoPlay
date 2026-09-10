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

import gi
import mpv

from .platform_compat import (
    GL_FRAMEBUFFER_BINDING,
    RENDER_BLOCK_FOR_TARGET_TIME,
    get_display_param,
    gl_get_integerv,
    gl_get_proc_address,
    TRACE,
    trace,
)

gi.require_version("GLib", "2.0")
gi.require_version("Gtk", "4.0")
from gi.repository import GLib, Gtk

logger = logging.getLogger(__name__)

DISPLAY_PARAM = get_display_param()


class BaseGLArea(Gtk.GLArea):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._ctx: mpv.MpvRenderContext | None = None
        self._mpv: mpv.MPV | None = None
        self._had_frame = False
        self._fbo = ctypes.c_int()
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

        Under NEKOPLAY_TRACE this also reports the first real frame, which
        is the end of startup as a user experiences it. The callback fires
        once when the render context is created too, before any file is
        loaded, so it has to check that the core is actually playing - the
        synchronous read that needs is why this is behind the flag.
        """
        if TRACE and not self._had_frame:
            try:
                if self._mpv is not None and self._mpv.core_idle is False:
                    self._had_frame = True
                    trace("first frame from mpv")
            except Exception:
                pass
        GLib.idle_add(
            self.queue_render,
            priority=GLib.PRIORITY_HIGH_IDLE,  # type: ignore
        )

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

        @self._mpv.property_observer("seeking")
        def on_seeking_change(_name, seeking):
            if not seeking:
                self._is_seeking = False
                self._flush_seek()

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

    def load_file(self, path):
        try:
            assert self._mpv is not None
            self._mpv.loadfile(path, "replace")
        except Exception:
            logger.exception("ThumbPreviewGLArea load_file failed")

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

            assert self._mpv is not None
            self._mpv.command_async("seek", time, "absolute+keyframes")
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

    def _on_realize(self, _area):
        trace("GL area realize")
        self.make_current()
        trace("GL context current")
        self._ctx = self._setup_mpv_context(self._mpv)
        trace("mpv render context up")

"""Spike: give mpv a native child HWND instead of routing frames through GTK.

NOT WIRED INTO THE APP. This is the experiment behind the "what still costs
more than mpv" section of README.md, kept so the numbers can be reproduced
and the design decision revisited.

  python build-aux/windows/spike-child-hwnd.py <clip> [seconds]

  SPIKE_INSET=1        reserve a strip at the bottom, outside the child
                       window, and put the controls there
  SPIKE_PASSTHROUGH=1  create the child hit-test transparent

WHAT IT MEASURED, 4K60 60 Mbit/s on a Radeon RX 9060 XT:

  CPU              7.5-10.6% of one core, against ~50% for the shipping
                   GLArea path. That is parity with bare mpv (7.7%).
  hwdec            d3d11va, ZERO COPY - the GPU->CPU->GPU trip that the
                   libmpv OpenGL render API forces simply is not there.
  frames           60fps, 0 drops, 0 delayed.
  resize/maximize  fine, once the child is moved with the parent.
  mouse input      fine, and for free: a STATIC child reports HTTRANSPARENT,
                   so clicks fall through to GTK. Both a click over the video
                   and one in the control strip reached the GTK gesture.

WHAT BREAKS, AND IT IS THE WHOLE DECISION:

  A Win32 child window paints above its parent's client area, so ANY GTK
  content underneath it is invisible - the overlay controls and the CSD
  headerbar both vanish completely. Video is all you see.

  Running with SPIKE_INSET=1 shows the only cheap way out: keep the child
  off the region the controls occupy. That works, but it means the controls
  can no longer float ON the video, which is what NekoPlay's UI is built
  around - auto-hiding controls and a headerbar over the picture.

  Preserving the current look would need either a second layered always-on-top
  window holding the controls and kept in sync, or mpv's swapchain placed in a
  DirectComposition visual beneath GTK's own content. GTK does have a DComp
  device now (see nekoplay.in), but its visual tree is not public API.
"""

import ctypes
import os
import sys
from ctypes import wintypes

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, GLib, Gtk  # noqa: E402

import mpv  # noqa: E402

CLIP = sys.argv[1]
RUN_SECONDS = float(sys.argv[2]) if len(sys.argv) > 2 else 40.0
INSET = bool(os.environ.get("SPIKE_INSET"))
CONTROL_STRIP = 90  # px reserved at the bottom when INSET is on

user32 = ctypes.WinDLL("user32", use_last_error=True)
gtklib = ctypes.CDLL("libgtk-4-1.dll")

gtklib.gdk_win32_surface_get_handle.restype = ctypes.c_void_p
gtklib.gdk_win32_surface_get_handle.argtypes = [ctypes.c_void_p]

user32.CreateWindowExW.restype = wintypes.HWND
user32.CreateWindowExW.argtypes = [
    wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, ctypes.c_void_p,
]
user32.MoveWindow.argtypes = [
    wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wintypes.BOOL,
]

WS_CHILD = 0x40000000
WS_VISIBLE = 0x10000000
WS_CLIPCHILDREN = 0x02000000
WS_EX_TRANSPARENT = 0x00000020
WS_EX_NOACTIVATE = 0x08000000

# With SPIKE_PASSTHROUGH the child is created hit-test transparent, so mouse
# messages fall through to the GTK window underneath instead of being eaten
# by the video surface.
PASSTHROUGH = bool(os.environ.get("SPIKE_PASSTHROUGH"))


def gpointer(obj):
    """The C pointer behind a PyGObject wrapper."""
    ctypes.pythonapi.PyCapsule_GetPointer.restype = ctypes.c_void_p
    ctypes.pythonapi.PyCapsule_GetPointer.argtypes = (ctypes.py_object, ctypes.c_char_p)
    return ctypes.pythonapi.PyCapsule_GetPointer(obj.__gpointer__, None)


class Spike(Gtk.ApplicationWindow):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.set_default_size(1280, 720)
        self.set_title("hwnd spike")

        self._child = None
        self._player = None

        # A GTK overlay drawn on top of where the video will be. If Win32
        # child-window painting wins, none of this will be visible.
        overlay = Gtk.Overlay()
        overlay.set_child(Gtk.Label(label="(video should cover this)"))

        bar = Gtk.CenterBox()
        bar.add_css_class("toolbar")
        bar.add_css_class("osd")
        bar.set_valign(Gtk.Align.END)
        bar.set_size_request(-1, 60)
        label = Gtk.Label(label="  OVERLAY CONTROLS - visible?  ")
        label.add_css_class("title-1")
        bar.set_center_widget(label)
        overlay.add_overlay(bar)

        # Does GTK still see clicks landing on top of the video surface?
        self._clicks = 0
        click = Gtk.GestureClick()
        click.connect("pressed", self._on_click)
        overlay.add_controller(click)

        self.set_child(overlay)
        self.connect("realize", self._on_realize)
        self.connect("notify::default-width", lambda *a: self._resize_child())
        self.connect("notify::default-height", lambda *a: self._resize_child())

    def _on_realize(self, *_a):
        surface = self.get_surface()
        parent = gtklib.gdk_win32_surface_get_handle(gpointer(surface))
        print(f"SPIKE parent HWND = {parent}", flush=True)
        if not parent:
            print("SPIKE FAILED: no HWND", flush=True)
            return

        w, h = self.get_width() or 1280, self.get_height() or 720
        vh = h - CONTROL_STRIP if INSET else h

        # "STATIC" is a predefined class, so no window class needs registering
        # for a spike. mpv only needs somewhere to put its swapchain.
        ex = (WS_EX_TRANSPARENT | WS_EX_NOACTIVATE) if PASSTHROUGH else 0
        self._child = user32.CreateWindowExW(
            ex, "STATIC", None, WS_CHILD | WS_VISIBLE | WS_CLIPCHILDREN,
            0, 0, w, vh, parent, None, None, None,
        )
        print(f"SPIKE child HWND  = {self._child} ({w}x{vh}, inset={INSET})", flush=True)
        if not self._child:
            print(f"SPIKE FAILED: CreateWindowExW {ctypes.get_last_error()}", flush=True)
            return

        # This is the whole point: mpv owns the window and presents with its
        # own D3D11 swapchain, exactly as bare mpv.exe does.
        self._player = mpv.MPV(
            wid=str(int(self._child)),
            vo="gpu-next",
            gpu_api="d3d11",
            hwdec="d3d11va",
            terminal=False,
            config=False,
            osc=False,
            really_quiet=True,
            keep_open="yes",
            loop_file="inf",
        )
        self._player.loadfile(CLIP, "replace")
        GLib.timeout_add_seconds(2, self._report)

    def _on_click(self, _g, _n, x, y):
        self._clicks += 1
        where = "CONTROL STRIP" if (INSET and y > self.get_height() - CONTROL_STRIP) else "OVER VIDEO"
        print(f"SPIKE click #{self._clicks} at ({int(x)},{int(y)}) {where}", flush=True)

    def _resize_child(self):
        if not self._child:
            return
        w, h = self.get_width(), self.get_height()
        vh = h - CONTROL_STRIP if INSET else h
        user32.MoveWindow(self._child, 0, 0, w, max(vh, 1), True)

    def _report(self):
        p = self._player
        if not p:
            return False
        try:
            print(
                f"SPIKE hwdec={p.hwdec_current} vo={p.current_vo} "
                f"fps={p.estimated_vf_fps} drops={p.frame_drop_count} "
                f"delayed={p.vo_delayed_frame_count} pos={p.time_pos} "
                f"clicks={self._clicks} passthrough={PASSTHROUGH}",
                flush=True,
            )
        except Exception as exc:
            print("SPIKE stat err", exc, flush=True)
        return True


def main():
    app = Gtk.Application(application_id="dev.spike.hwnd")

    def activate(a):
        win = Spike(application=a)
        win.present()
        GLib.timeout_add_seconds(int(RUN_SECONDS), lambda: (a.quit(), False)[1])

    app.connect("activate", activate)
    app.run([])


main()

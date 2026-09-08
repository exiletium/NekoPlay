"""Spike: mpv in a child HWND, with the controls in a layered window on top.

NOT WIRED INTO THE APP. Second half of the child-HWND investigation; see
spike-child-hwnd.py for the first, and README.md for the write-up.

  python build-aux/windows/spike-layered-controls.py <clip> [seconds]

THE SHAPE

  main GTK window
    +-- child HWND (STATIC), owned by mpv, vo=gpu-next + d3d11 + zero-copy
  control GTK window (separate toplevel)
    - undecorated, WS_EX_LAYERED | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW
    - owned by the main window, so it floats above it and minimises with it
    - painted magenta, which SetLayeredWindowAttributes turns into a hole
    - kept over the main window's client area by a poll

WHY IT IS BUILT THIS WAY

  A Win32 child window paints above its parent's client area, so anything GTK
  draws under the video is invisible. Putting the controls in a SEPARATE
  toplevel gets them back on top of the picture.

  The transparency uses colour keying rather than per-pixel alpha because
  GTK4 on Win32 does not give a toplevel an alpha channel - a window with
  `background: transparent` renders opaque black.

  Colour keying only works while GTK is NOT presenting through Direct
  Composition; a DComp-presented window ignores
  SetLayeredWindowAttributes entirely. That is fine here, and is the neat
  part: once video lives in the child HWND it never touches GSK, so neither
  window needs the GPU renderer and both can render their little bit of
  chrome in software. GDK_WIN32_FORCE_DCOMP is deliberately NOT set.

  The keyed region is click-through as well as see-through, so clicks over
  the video fall through to the window underneath.

KNOWN COMPROMISE

  Colour keying is binary - a pixel is either fully opaque or fully gone.
  Control backgrounds therefore have to be opaque; the translucent OSD look
  the app uses today would blend against the key colour instead of the video.
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
RUN_SECONDS = int(sys.argv[2]) if len(sys.argv) > 2 else 120

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
user32.SetLayeredWindowAttributes.argtypes = [
    wintypes.HWND, wintypes.COLORREF, ctypes.c_ubyte, wintypes.DWORD
]
user32.GetWindowLongPtrW.restype = ctypes.c_longlong
user32.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
user32.SetWindowLongPtrW.restype = ctypes.c_longlong
user32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_longlong]
user32.SetWindowPos.argtypes = [
    wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
    ctypes.c_int, ctypes.c_int, wintypes.UINT,
]
user32.MoveWindow.argtypes = [
    wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wintypes.BOOL,
]


class RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


WS_CHILD = 0x40000000
WS_VISIBLE = 0x10000000
WS_CLIPCHILDREN = 0x02000000
GWL_EXSTYLE = -20
GWLP_HWNDPARENT = -8
WS_EX_LAYERED = 0x00080000
WS_EX_NOACTIVATE = 0x08000000
WS_EX_TOOLWINDOW = 0x00000080
LWA_COLORKEY = 0x00000001
SWP_NOACTIVATE = 0x0010
SWP_NOZORDER = 0x0004
HWND_TOP = 0

KEY_R, KEY_G, KEY_B = 255, 0, 255

# WS_EX_NOACTIVATE keeps the overlay from stealing focus, but it also seems
# to stop GTK dispatching clicks to it. SPIKE_ACTIVATABLE=1 drops the flag.
NOACTIVATE = not os.environ.get("SPIKE_ACTIVATABLE")

CSS = b"""
window.keyed { background: rgb(255, 0, 255); }
/* Opaque, because colour keying cannot blend. */
.osd-bar {
  background: #1b1b1b;
  border-radius: 16px;
  padding: 10px 18px;
  border: 1px solid #3a3a3a;
}
"""


def gpointer(obj):
    ctypes.pythonapi.PyCapsule_GetPointer.restype = ctypes.c_void_p
    ctypes.pythonapi.PyCapsule_GetPointer.argtypes = (ctypes.py_object, ctypes.c_char_p)
    return ctypes.pythonapi.PyCapsule_GetPointer(obj.__gpointer__, None)


def hwnd_of(window):
    return gtklib.gdk_win32_surface_get_handle(gpointer(window.get_surface()))


class Controls(Gtk.Window):
    """The overlay. Everything not painted by a widget is a hole."""

    def __init__(self, on_click):
        super().__init__()
        self.set_decorated(False)
        self.add_css_class("keyed")
        self.set_default_size(800, 450)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.set_valign(Gtk.Align.END)
        box.set_halign(Gtk.Align.CENTER)
        box.set_margin_bottom(28)

        bar = Gtk.Box(spacing=14)
        bar.add_css_class("osd-bar")
        for icon in ("media-skip-backward", "media-playback-pause", "media-skip-forward"):
            b = Gtk.Button(icon_name=icon)
            b.add_css_class("circular")
            b.connect("clicked", on_click)
            bar.append(b)
        self.time_label = Gtk.Label(label="0:00")
        bar.append(self.time_label)
        scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 100, 1)
        scale.set_size_request(360, -1)
        scale.set_draw_value(False)
        bar.append(scale)
        box.append(bar)
        self.set_child(box)

    def make_layered(self):
        h = hwnd_of(self)
        ex = user32.GetWindowLongPtrW(h, GWL_EXSTYLE)
        style = ex | WS_EX_LAYERED | WS_EX_TOOLWINDOW
        if NOACTIVATE:
            style |= WS_EX_NOACTIVATE
        user32.SetWindowLongPtrW(h, GWL_EXSTYLE, style)
        ok = user32.SetLayeredWindowAttributes(
            h, KEY_R | (KEY_G << 8) | (KEY_B << 16), 0, LWA_COLORKEY
        )
        print(
            f"LAYERED control hwnd={h} colour_key_applied={bool(ok)} "
            f"noactivate={NOACTIVATE}",
            flush=True,
        )
        return h


class Main(Gtk.ApplicationWindow):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.set_default_size(1280, 720)
        self.set_title("layered controls spike")
        self.set_child(Gtk.Label(label=""))

        self.child_hwnd = None
        self.player = None
        self.controls = None
        self.clicks = 0
        self.connect("realize", self._on_realize)

    def _on_click(self, _b):
        self.clicks += 1
        print(f"LAYERED control clicked (total {self.clicks})", flush=True)

    def _on_realize(self, *_a):
        parent = hwnd_of(self)
        print(f"LAYERED main hwnd={parent}", flush=True)

        w, h = self.get_width() or 1280, self.get_height() or 720
        self.child_hwnd = user32.CreateWindowExW(
            0, "STATIC", None, WS_CHILD | WS_VISIBLE | WS_CLIPCHILDREN,
            0, 0, w, h, parent, None, None, None,
        )
        print(f"LAYERED video child hwnd={self.child_hwnd}", flush=True)

        self.player = mpv.MPV(
            wid=str(int(self.child_hwnd)),
            vo="gpu-next", gpu_api="d3d11", hwdec="d3d11va",
            terminal=False, config=False, osc=False, really_quiet=True,
            keep_open="yes", loop_file="inf",
        )
        self.player.loadfile(CLIP, "replace")

        self.controls = Controls(self._on_click)
        self.controls.present()
        GLib.timeout_add(60, self._first_layer)
        GLib.timeout_add(100, self._sync)
        GLib.timeout_add_seconds(2, self._report)

    def _first_layer(self):
        if not self.controls.get_realized():
            return True
        ctrl = self.controls.make_layered()
        # Owned, not parented: floats above the main window and follows it
        # through minimise and restore, without becoming a child.
        user32.SetWindowLongPtrW(ctrl, GWLP_HWNDPARENT, hwnd_of(self))
        return False

    def _sync(self):
        """Keep the overlay exactly over the main window's client area."""
        if not (self.controls and self.controls.get_realized()):
            return True
        main = hwnd_of(self)
        ctrl = hwnd_of(self.controls)
        if not main or not ctrl:
            return True

        rect = RECT()
        user32.GetClientRect(main, ctypes.byref(rect))
        origin = POINT(0, 0)
        user32.ClientToScreen(main, ctypes.byref(origin))
        w, h = rect.right - rect.left, rect.bottom - rect.top

        user32.SetWindowPos(
            ctrl, HWND_TOP, origin.x, origin.y, w, h,
            SWP_NOACTIVATE,
        )
        if self.child_hwnd:
            user32.MoveWindow(self.child_hwnd, 0, 0, w, h, True)
        return True

    def _report(self):
        p = self.player
        if not p:
            return False
        try:
            print(
                f"LAYERED hwdec={p.hwdec_current} vo={p.current_vo} "
                f"fps={p.estimated_vf_fps} drops={p.frame_drop_count} "
                f"delayed={p.vo_delayed_frame_count} clicks={self.clicks}",
                flush=True,
            )
        except Exception as exc:
            print("LAYERED stat err", exc, flush=True)
        return True


def main():
    app = Gtk.Application(application_id="dev.spike.layered")

    def activate(a):
        provider = Gtk.CssProvider()
        provider.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )
        win = Main(application=a)
        win.present()
        GLib.timeout_add_seconds(RUN_SECONDS, lambda: (a.quit(), False)[1])

    app.connect("activate", activate)
    app.run([])


main()

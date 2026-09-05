# platform_compat.py
#
# Copyright 2026 Diego Povliuk
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
# SPDX-License-Identifier: GPL-3.0-or-later

"""Everything that differs between Linux and Windows.

Linux talks to X11/Wayland and resolves GL through EGL; Windows has neither
and resolves through WGL. Keeping the two apart here means the rest of the
app never has to ask which one it is running on.
"""

import ctypes
import logging
import os

logger = logging.getLogger(__name__)

IS_WINDOWS = os.name == "nt"

# mpv separates path lists with ':' on Unix and ';' on Windows, because a
# colon is part of every absolute path here.
MPV_PATH_SEP = ";" if IS_WINDOWS else ":"

# Folder created under the user config dir. 'cine' is what Cine has always
# used; on Windows it lands in %LOCALAPPDATA%, where a bare 'cine' sitting
# among vendor-named folders would be a mystery.
CONFIG_DIR_NAME = "NekoPlay" if IS_WINDOWS else "cine"

# Adwaita Sans is part of the GNOME runtime and is not packaged for Windows,
# so fall back to a face that ships with the OS.
DEFAULT_SUB_FONT = "Segoe UI Semibold" if IS_WINDOWS else "Adwaita Sans SemiBold"
DEFAULT_OSD_FONT = "Segoe UI" if IS_WINDOWS else "Adwaita Sans"

# Keeps a console window from flashing up when we shell out to ffprobe.
SUBPROCESS_FLAGS = 0x08000000 if IS_WINDOWS else 0  # CREATE_NO_WINDOW


# --- Flatpak ---------------------------------------------------------------

if IS_WINDOWS:

    def is_document_portal_path(path) -> bool:
        """There is no Flatpak document portal off Linux."""
        return False

else:
    _DOC_PORTAL_PREFIX = f"/run/user/{os.getuid()}/doc/"

    def is_document_portal_path(path) -> bool:
        """True for files handed over by the Flatpak document portal.

        Those are readable but their real folder is not, so anything that
        walks the containing directory has to be switched off.
        """
        return _DOC_PORTAL_PREFIX in str(path)


# --- OpenGL ----------------------------------------------------------------

GL_FRAMEBUFFER_BINDING = 0x8CA6

_GL_GET_INTEGERV = ctypes.CFUNCTYPE(None, ctypes.c_uint, ctypes.POINTER(ctypes.c_int))

if IS_WINDOWS:
    _opengl32 = ctypes.WinDLL("opengl32.dll")
    _kernel32 = ctypes.WinDLL("kernel32.dll")

    _wgl_get_proc_address = _opengl32.wglGetProcAddress
    _wgl_get_proc_address.restype = ctypes.c_void_p
    _wgl_get_proc_address.argtypes = [ctypes.c_char_p]

    _get_proc_address = _kernel32.GetProcAddress
    _get_proc_address.restype = ctypes.c_void_p
    _get_proc_address.argtypes = [ctypes.c_void_p, ctypes.c_char_p]

    # Some drivers hand back these instead of NULL for an unknown symbol.
    _WGL_FAILURE = frozenset((None, 0, 1, 2, 3, -1, 0xFFFFFFFFFFFFFFFF))

    try:
        # Only present when GTK falls back to ANGLE instead of native WGL.
        _libegl = ctypes.CDLL("libEGL.dll")
        _egl_get_proc_address = _libegl.eglGetProcAddress
        _egl_get_proc_address.restype = ctypes.c_void_p
        _egl_get_proc_address.argtypes = [ctypes.c_char_p]
    except OSError:
        _egl_get_proc_address = None

    def gl_get_proc_address(name: bytes) -> int:
        """Resolve a GL symbol against the context GTK just made current.

        wglGetProcAddress only knows extension entry points, so the core
        GL 1.1 functions have to be pulled out of opengl32.dll by hand.
        """
        addr = _wgl_get_proc_address(name)
        if addr not in _WGL_FAILURE:
            return addr

        addr = _get_proc_address(_opengl32._handle, name)
        if addr:
            return addr

        if _egl_get_proc_address is not None:
            return _egl_get_proc_address(name) or 0

        return 0

    _gl_get_integerv = None

    def gl_get_integerv(pname, out) -> None:
        # Resolved on first use, which is the first render, by which point
        # a context is current. Under ANGLE the opengl32 export would be
        # querying a different context than the one we draw into.
        global _gl_get_integerv
        if _gl_get_integerv is None:
            addr = gl_get_proc_address(b"glGetIntegerv")
            if addr:
                _gl_get_integerv = _GL_GET_INTEGERV(addr)
            else:
                _gl_get_integerv = _opengl32.glGetIntegerv
                _gl_get_integerv.argtypes = _GL_GET_INTEGERV._argtypes_

        _gl_get_integerv(pname, out)

else:
    _libegl = ctypes.CDLL("libEGL.so.1")
    _egl_get_proc_address = _libegl.eglGetProcAddress
    _egl_get_proc_address.restype = ctypes.c_void_p
    _egl_get_proc_address.argtypes = [ctypes.c_char_p]

    _libgl = ctypes.CDLL("libGL.so.1")
    _gl_get_integerv = _libgl.glGetIntegerv
    _gl_get_integerv.argtypes = _GL_GET_INTEGERV._argtypes_

    def gl_get_proc_address(name: bytes) -> int:
        return _egl_get_proc_address(name) or 0

    def gl_get_integerv(pname, out) -> None:
        _gl_get_integerv(pname, out)


# --- Display handle --------------------------------------------------------


def get_display_param() -> dict:
    """The native display handle mpv needs to share our GL context.

    Windows has none to give: a WGL context is found through the calling
    thread, so mpv works it out for itself and we pass nothing.
    """
    if IS_WINDOWS:
        return {}

    # see https://gist.github.com/omnp/6ac3385e2b3f6cab987d84e6477e636a

    import gi

    gi.require_version("Gdk", "4.0")
    gi.require_version("GdkX11", "4.0")
    gi.require_version("GdkWayland", "4.0")
    from gi.repository import (  # pyright: ignore[reportAttributeAccessIssue]
        Gdk,
        GdkWayland,
        GdkX11,
    )

    param = {}

    def get_pointer(display):
        ctypes.pythonapi.PyCapsule_GetPointer.restype = ctypes.c_void_p
        ctypes.pythonapi.PyCapsule_GetPointer.argtypes = (ctypes.py_object,)
        return ctypes.pythonapi.PyCapsule_GetPointer(display.__gpointer__, None)

    try:
        gtk = ctypes.CDLL("libgtk-4.so.1")
        display = Gdk.Display.get_default()

        if isinstance(display, GdkWayland.WaylandDisplay):
            gtk.gdk_wayland_display_get_wl_display.restype = ctypes.c_void_p
            gtk.gdk_wayland_display_get_wl_display.argtypes = [ctypes.c_void_p]
            ptr = gtk.gdk_wayland_display_get_wl_display(get_pointer(display))
            if ptr:
                param["wl_display"] = ptr
        elif isinstance(display, GdkX11.X11Display):
            gtk.gdk_x11_display_get_xdisplay.restype = ctypes.c_void_p
            gtk.gdk_x11_display_get_xdisplay.argtypes = [ctypes.c_void_p]
            ptr = gtk.gdk_x11_display_get_xdisplay(get_pointer(display))
            if ptr:
                param["x11_display"] = ptr
    except Exception:
        logger.exception("get_display_param failed")

    return param


# --- Idle inhibition -------------------------------------------------------

if IS_WINDOWS:
    ES_CONTINUOUS = 0x80000000
    ES_SYSTEM_REQUIRED = 0x00000001
    ES_DISPLAY_REQUIRED = 0x00000002

    _set_thread_execution_state = ctypes.windll.kernel32.SetThreadExecutionState
    _set_thread_execution_state.restype = ctypes.c_uint
    _set_thread_execution_state.argtypes = [ctypes.c_uint]

    def inhibit_idle(app, window, reason) -> int:
        """Keep the machine and its display awake while something plays.

        GtkApplication.inhibit() needs a session manager answering on
        D-Bus, so on Windows it always returns 0 and we ask the OS
        ourselves. The flag is per-thread, and this runs on the main loop.
        """
        try:
            _set_thread_execution_state(
                ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED
            )
        except Exception:
            logger.exception("SetThreadExecutionState failed")
            return 0
        return 1  # a cookie the caller can test, not a real handle

    def uninhibit_idle(app, cookie) -> None:
        try:
            _set_thread_execution_state(ES_CONTINUOUS)
        except Exception:
            logger.exception("SetThreadExecutionState failed")

else:

    def inhibit_idle(app, window, reason) -> int:
        import gi

        gi.require_version("Gtk", "4.0")
        from gi.repository import Gtk

        return app.inhibit(window, Gtk.ApplicationInhibitFlags.IDLE, reason)

    def uninhibit_idle(app, cookie) -> None:
        app.uninhibit(cookie)

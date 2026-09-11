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
import subprocess

logger = logging.getLogger(__name__)

IS_WINDOWS = os.name == "nt"

# Set NEKOPLAY_TRACE=1 for a timestamped startup trace; see the end of this file.
TRACE = bool(os.environ.get("NEKOPLAY_TRACE"))

# mpv separates path lists with ':' on Unix and ';' on Windows, because a
# colon is part of every absolute path here.
MPV_PATH_SEP = ";" if IS_WINDOWS else ":"

# Folder created under the user config dir. 'cine' is what Cine has always
# used; on Windows it lands in %LOCALAPPDATA%, where a bare 'cine' sitting
# among vendor-named folders would be a mystery.
CONFIG_DIR_NAME = "NekoPlay" if IS_WINDOWS else "cine"

# Keeps a console window from flashing up when we shell out to ffprobe.
SUBPROCESS_FLAGS = 0x08000000 if IS_WINDOWS else 0  # CREATE_NO_WINDOW

# Whether mpv's render call should sit and wait for the frame's target time.
#
# It does by default, which is how mpv paces frames precisely. On Windows that
# wait dominates the main loop: 14.8ms of every 16.7ms frame at 4K60, so input
# and UI updates queue behind it and the whole player feels sluggish. Handing
# the wait back drops that to 1.9ms with no measured cost - frame drops and
# delayed frames both stayed at zero - because mpv already paces the update
# callbacks that drive rendering. Left alone on Linux, where the toolkit
# composites far more cheaply and this is untested.
RENDER_BLOCK_FOR_TARGET_TIME = not IS_WINDOWS

# Whether finishing a video should carry on into the rest of its folder.
# On Linux the Flatpak cannot read that folder without host permission, so
# this is off unless the user has asked for it; Windows has no such gate,
# which made every video roll on into whatever else happened to be sitting
# beside it. Off here matches what the Flatpak does out of the box.
AUTOCREATE_PLAYLIST = "no" if IS_WINDOWS else "filter"


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


# --- Window corners --------------------------------------------------------

if IS_WINDOWS:
    _DWMWA_WINDOW_CORNER_PREFERENCE = 33
    _DWMWA_BORDER_COLOR = 34
    _DWMWCP_ROUND = 2
    _DWMWA_COLOR_NONE = 0xFFFFFFFE

    def round_window_corners(window) -> None:
        """Round this window's corners, and drop the frame DWM draws round it.

        On Linux GTK draws the rounded corners itself and relies on the
        toplevel's alpha channel to hide everything outside them. Windows
        toplevels have no alpha, so that radius has to go or it leaves black
        wedges in the corners - and DWM does not round a borderless
        client-side-decorated window of its own accord. Asking it directly
        gives the rounding back, drawn by the compositor rather than by GTK.

        DWM also outlines rounded windows with a thin border of its own, which
        the original does not have, so that is turned off at the same time.
        """
        try:
            surface = window.get_surface()
            if surface is None:
                return

            gtk = ctypes.CDLL("libgtk-4-1.dll")
            gtk.gdk_win32_surface_get_handle.restype = ctypes.c_void_p
            gtk.gdk_win32_surface_get_handle.argtypes = [ctypes.c_void_p]

            ctypes.pythonapi.PyCapsule_GetPointer.restype = ctypes.c_void_p
            ctypes.pythonapi.PyCapsule_GetPointer.argtypes = (ctypes.py_object,)
            ptr = ctypes.pythonapi.PyCapsule_GetPointer(surface.__gpointer__, None)

            hwnd = gtk.gdk_win32_surface_get_handle(ptr)
            if hwnd:
                _round_hwnd(hwnd)
        except Exception:
            logger.exception("round_window_corners failed")

    def _round_hwnd(hwnd: int) -> bool:
        """Rounded corners and no DWM border for one native window."""
        dwm = ctypes.WinDLL("dwmapi.dll")
        dwm.DwmSetWindowAttribute.argtypes = [
            ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p, ctypes.c_uint,
        ]
        ok = True
        for attribute, value in (
            (_DWMWA_WINDOW_CORNER_PREFERENCE, _DWMWCP_ROUND),
            (_DWMWA_BORDER_COLOR, _DWMWA_COLOR_NONE),
        ):
            setting = ctypes.c_uint(value)
            # Rounded corners only exist from Windows 11 on; older versions
            # return an error and simply keep square ones.
            if dwm.DwmSetWindowAttribute(
                hwnd, attribute, ctypes.byref(setting), ctypes.sizeof(setting)
            ) != 0:
                ok = False
        return ok

    # Popovers, menus, dropdown lists and tooltips are native windows of
    # their own, created by GTK whenever one opens, and each has the same
    # problem as the toplevel: GTK expects an alpha channel and draws rounded
    # corners over what it assumes is transparency, and on Windows that is
    # black. There is no GTK signal for "a surface was created", but Windows
    # has one for "a window was shown", and only for that event: a WinEvent
    # hook on EVENT_OBJECT_SHOW. It is also the right moment - DWM rejects
    # the attribute while a window is still being created (a CBT hook at
    # HCBT_CREATEWND is too early) and accepts it once shown. Out-of-context,
    # because the in-context kind needs the callback in a DLL; this one is
    # delivered through our own message loop a moment after the show.

    _EVENT_OBJECT_DESTROY = 0x8001
    _EVENT_OBJECT_SHOW = 0x8002
    _OBJID_WINDOW = 0
    _WINEVENT_OUTOFCONTEXT = 0x0000
    _GWL_STYLE = -16
    _WS_CHILD = 0x40000000

    _WINEVENTPROC = ctypes.WINFUNCTYPE(
        None,
        ctypes.c_void_p,  # HWINEVENTHOOK
        ctypes.c_uint,  # event
        ctypes.c_void_p,  # hwnd
        ctypes.c_long,  # idObject
        ctypes.c_long,  # idChild
        ctypes.c_uint,  # idEventThread
        ctypes.c_uint,  # dwmsEventTime
    )
    _winevent_hook = None
    _winevent_proc = None  # the callback must outlive the hook
    _rounded: set[int] = set()

    def round_new_windows() -> None:
        """Round the corners of every window this thread shows from now on.

        Call once, from the GUI thread, before any popover can open. The
        toplevel is handled by round_window_corners on realize as well,
        which is harmless; this exists for the windows GTK creates on its
        own and never hands to the app.
        """
        global _winevent_hook, _winevent_proc
        if _winevent_hook is not None:
            return

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32")
        user32.SetWinEventHook.restype = ctypes.c_void_p
        user32.SetWinEventHook.argtypes = [
            ctypes.c_uint, ctypes.c_uint, ctypes.c_void_p, _WINEVENTPROC,
            ctypes.c_uint, ctypes.c_uint, ctypes.c_uint,
        ]
        user32.GetWindowLongW.restype = ctypes.c_long
        user32.GetWindowLongW.argtypes = [ctypes.c_void_p, ctypes.c_int]

        def proc(_hook, event, hwnd, id_object, id_child, _thread, _time):
            if not hwnd or id_object != _OBJID_WINDOW or id_child != 0:
                return
            try:
                if event == _EVENT_OBJECT_DESTROY:
                    _rounded.discard(hwnd)
                elif event == _EVENT_OBJECT_SHOW and hwnd not in _rounded:
                    # Child windows have no frame of their own to round.
                    if not user32.GetWindowLongW(hwnd, _GWL_STYLE) & _WS_CHILD:
                        if _round_hwnd(hwnd):
                            _rounded.add(hwnd)
            except Exception:
                logger.exception("Could not round a new window")

        _winevent_proc = _WINEVENTPROC(proc)
        _winevent_hook = user32.SetWinEventHook(
            _EVENT_OBJECT_DESTROY,
            _EVENT_OBJECT_SHOW,
            None,
            _winevent_proc,
            kernel32.GetCurrentProcessId(),
            kernel32.GetCurrentThreadId(),
            _WINEVENT_OUTOFCONTEXT,
        )
        if not _winevent_hook:
            logger.warning("SetWinEventHook failed (%d)", ctypes.get_last_error())

    def strip_popover_arrows(root) -> None:
        """Take the arrow off every popover under *root*.

        A popover with an arrow gets a surface tall enough for the tail, and
        the strip beside the tail is transparent in GTK's eyes - black here.
        The tail's size is a constant in GtkPopover, not CSS, so the only way
        to lose the strip is to lose the arrow. Menu buttons create their
        popovers up front, so a walk over the tree finds them all.
        """
        import gi

        gi.require_version("Gtk", "4.0")
        from gi.repository import Gtk

        stack = [root]
        while stack:
            widget = stack.pop()
            popover = None
            if isinstance(widget, Gtk.MenuButton):
                popover = widget.get_popover()
            elif isinstance(widget, Gtk.Popover):
                popover = widget
            if popover is not None:
                popover.set_has_arrow(False)
            child = widget.get_first_child()
            while child is not None:
                stack.append(child)
                child = child.get_next_sibling()

else:

    def round_window_corners(window) -> None:
        """The window manager handles this everywhere else."""

    def round_new_windows() -> None:
        """The window manager handles this everywhere else."""

    def strip_popover_arrows(root) -> None:
        """Popovers are transparent everywhere else."""


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


# --- Startup trace ---------------------------------------------------------
#
# Set NEKOPLAY_TRACE=1 to get a timestamped line per startup milestone. The
# clock starts at process creation rather than at the first line of Python,
# so the numbers include loading the interpreter and the ~200 DLLs behind
# GTK - which is where most of a cold launch actually goes.



def _process_start_time() -> float:
    """When this process was created, on the time.perf_counter() scale."""
    import time

    now = time.perf_counter()
    if IS_WINDOWS:
        try:
            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            k32.GetCurrentProcess.restype = ctypes.c_void_p
            p_u64 = ctypes.POINTER(ctypes.c_ulonglong)
            # Without argtypes the pseudo-handle is passed as a 32-bit int
            # and the call just fails, which reads as a zero-length startup.
            k32.GetProcessTimes.argtypes = [ctypes.c_void_p] + [p_u64] * 4
            times = [ctypes.c_ulonglong() for _ in range(4)]
            if k32.GetProcessTimes(
                k32.GetCurrentProcess(), *[ctypes.byref(t) for t in times]
            ):
                # FILETIME is 100 ns ticks since 1601, and so is "now" from
                # GetSystemTimePreciseAsFileTime, so the difference between
                # them is the age of the process.
                sys_now = ctypes.c_ulonglong()
                k32.GetSystemTimePreciseAsFileTime(ctypes.byref(sys_now))
                age = (sys_now.value - times[0].value) / 1e7
                return now - age
        except Exception:
            pass
    return now


_T0 = _process_start_time() if TRACE else 0.0


def trace(label: str) -> None:
    """Note that a startup milestone has been reached."""
    if not TRACE:
        return
    import time

    print("[trace] %7.1f ms  %s" % ((time.perf_counter() - _T0) * 1000, label))


# --- Process trees ---------------------------------------------------------
#
# video2x runs its decoder and encoder in child processes of its own, plus
# two ffmpegs. Killing the Python that started them leaves all of that
# running - still holding the output file, still burning the GPU, and still
# holding the stdout pipe open so the reader never sees EOF. What is wanted
# is "everything this command started, gone", including when this process
# itself goes away without asking.


class ProcessTree:
    """A subprocess whose whole tree dies when asked, or when we do.

    On Windows the child goes into a Job Object flagged to kill its members
    when the last handle to it closes; descendants inherit the job, and the
    handle lives as long as this object - or this process. Elsewhere the
    child leads a new session, and the whole process group gets the signal.
    """

    def __init__(self, cmd, **kwargs):
        self._job = None
        if IS_WINDOWS:
            self.proc = subprocess.Popen(cmd, **kwargs)
            self._job = self._make_job()
            if self._job:
                k32 = ctypes.WinDLL("kernel32", use_last_error=True)
                k32.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
                if not k32.AssignProcessToJobObject(self._job, int(self.proc._handle)):
                    logger.warning(
                        "AssignProcessToJobObject failed (%d)", ctypes.get_last_error()
                    )
        else:
            kwargs.setdefault("start_new_session", True)
            self.proc = subprocess.Popen(cmd, **kwargs)

    @staticmethod
    def _make_job():
        JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
        JobObjectExtendedLimitInformation = 9

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [(n, ctypes.c_ulonglong) for n in (
                "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
            )]

        class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", ctypes.c_uint32),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", ctypes.c_uint32),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", ctypes.c_uint32),
                ("SchedulingClass", ctypes.c_uint32),
            ]

        class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
                ("IoInfo", IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        try:
            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            k32.CreateJobObjectW.restype = ctypes.c_void_p
            k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
            k32.SetInformationJobObject.argtypes = [
                ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32,
            ]
            job = k32.CreateJobObjectW(None, None)
            if not job:
                return None
            info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
            info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not k32.SetInformationJobObject(
                job, JobObjectExtendedLimitInformation, ctypes.byref(info), ctypes.sizeof(info)
            ):
                logger.warning("SetInformationJobObject failed (%d)", ctypes.get_last_error())
            return job
        except Exception:
            logger.exception("Could not create a job object")
            return None

    def kill(self) -> None:
        """Terminate the process and everything it started."""
        if IS_WINDOWS:
            if self._job:
                k32 = ctypes.WinDLL("kernel32", use_last_error=True)
                k32.TerminateJobObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
                k32.TerminateJobObject(self._job, 1)
            try:
                self.proc.kill()
            except OSError:
                pass
        else:
            import signal

            try:
                os.killpg(self.proc.pid, signal.SIGKILL)
            except OSError:
                try:
                    self.proc.kill()
                except OSError:
                    pass

    def close(self) -> None:
        """Let go of the job once the tree has exited on its own."""
        if self._job:
            ctypes.WinDLL("kernel32").CloseHandle(self._job)
            self._job = None


# --- Single instance -------------------------------------------------------
#
# On Linux a second `nekoplay file.mp4` hands the file to the running
# instance over D-Bus and exits: GApplication does it, and it is how the
# desktop entry (Exec=nekoplay --new-window %U) is meant to work. Windows has
# no session bus, so GApplication quietly makes every launch a primary and a
# double-click in Explorer paid for a whole new process and window every
# time - about a second warm.
#
# A named pipe gives both halves of what D-Bus provided. Whoever creates it
# with FILE_FLAG_FIRST_PIPE_INSTANCE is the primary, atomically; anyone who
# finds it already there connects, writes its arguments and exits, well
# before GTK would have started loading. The default pipe ACL lets other
# users read but not write, so only this user's launches get through.


class SingleInstance:
    """Be the one running instance, or hand this launch's arguments to it."""

    def __init__(self, name: str):
        # A raw string cannot end in a backslash, hence the odd spelling.
        self.name = "\\\\.\\pipe\\" + name
        self._handle = None
        self._k32 = None

    def _kernel32(self):
        if self._k32 is None:
            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            k32.CreateNamedPipeW.restype = ctypes.c_void_p
            k32.CreateNamedPipeW.argtypes = [
                ctypes.c_wchar_p, ctypes.c_uint, ctypes.c_uint, ctypes.c_uint,
                ctypes.c_uint, ctypes.c_uint, ctypes.c_uint, ctypes.c_void_p,
            ]
            k32.CreateFileW.restype = ctypes.c_void_p
            k32.CreateFileW.argtypes = [
                ctypes.c_wchar_p, ctypes.c_uint, ctypes.c_uint, ctypes.c_void_p,
                ctypes.c_uint, ctypes.c_uint, ctypes.c_void_p,
            ]
            k32.WaitNamedPipeW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint]
            k32.ConnectNamedPipe.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
            k32.DisconnectNamedPipe.argtypes = [ctypes.c_void_p]
            k32.ReadFile.argtypes = [
                ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint,
                ctypes.POINTER(ctypes.c_uint), ctypes.c_void_p,
            ]
            k32.WriteFile.argtypes = [
                ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint,
                ctypes.POINTER(ctypes.c_uint), ctypes.c_void_p,
            ]
            k32.CloseHandle.argtypes = [ctypes.c_void_p]
            self._k32 = k32
        return self._k32

    # -- election -----------------------------------------------------------

    def claim(self) -> bool:
        """Try to become the primary. True if we are; False if one exists."""
        PIPE_ACCESS_INBOUND = 0x00000001
        FILE_FLAG_FIRST_PIPE_INSTANCE = 0x00080000
        PIPE_TYPE_MESSAGE = 0x00000004
        PIPE_READMODE_MESSAGE = 0x00000002
        PIPE_WAIT = 0x00000000
        INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

        k32 = self._kernel32()
        handle = k32.CreateNamedPipeW(
            self.name,
            PIPE_ACCESS_INBOUND | FILE_FLAG_FIRST_PIPE_INSTANCE,
            PIPE_TYPE_MESSAGE | PIPE_READMODE_MESSAGE | PIPE_WAIT,
            1,  # one instance: launches are rare and served in turn
            0,
            65536,
            0,
            None,
        )
        if handle is None or handle == INVALID_HANDLE_VALUE:
            # "Someone already has this name" arrives as ERROR_PIPE_BUSY (231)
            # when the one allowed instance exists, or ERROR_ACCESS_DENIED (5)
            # from the first-instance flag; measured here, it is 231. Anything
            # else is a surprise, and running standalone is the safe reaction.
            error = ctypes.get_last_error()
            if error in (5, 231):
                return False
            logger.warning("CreateNamedPipe failed (%d); running standalone", error)
            return True
        self._handle = handle
        return True

    def forward(self, payload: bytes, timeout_ms: int = 3000) -> bool:
        """Deliver *payload* to the primary. True if it was written."""
        GENERIC_WRITE = 0x40000000
        OPEN_EXISTING = 3
        INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
        ERROR_PIPE_BUSY = 231

        k32 = self._kernel32()
        deadline_tries = 3
        while True:
            handle = k32.CreateFileW(
                self.name, GENERIC_WRITE, 0, None, OPEN_EXISTING, 0, None
            )
            if handle is not None and handle != INVALID_HANDLE_VALUE:
                break
            error = ctypes.get_last_error()
            deadline_tries -= 1
            if error == ERROR_PIPE_BUSY and deadline_tries > 0:
                # Another launch is being served; wait for our turn.
                k32.WaitNamedPipeW(self.name, timeout_ms)
                continue
            return False

        try:
            written = ctypes.c_uint(0)
            ok = k32.WriteFile(handle, payload, len(payload), ctypes.byref(written), None)
            return bool(ok) and written.value == len(payload)
        finally:
            k32.CloseHandle(handle)

    # -- serving --------------------------------------------------------------

    def serve(self, callback) -> None:
        """Start handing each forwarded launch to *callback(bytes)*.

        Runs on its own thread; the callback is invoked there and must
        marshal to the main loop itself.
        """
        if self._handle is None:
            return
        import threading

        threading.Thread(
            target=self._serve, args=(callback,), name="single-instance", daemon=True
        ).start()

    def _serve(self, callback) -> None:
        ERROR_PIPE_CONNECTED = 535
        ERROR_MORE_DATA = 234
        k32 = self._kernel32()
        buffer = ctypes.create_string_buffer(65536)
        while True:
            if not k32.ConnectNamedPipe(self._handle, None):
                if ctypes.get_last_error() != ERROR_PIPE_CONNECTED:
                    # The handle is gone; nothing more to serve.
                    if ctypes.get_last_error() in (6, 232):  # INVALID_HANDLE, NO_DATA
                        return
                    logger.debug("ConnectNamedPipe failed (%d)", ctypes.get_last_error())
                    continue
            try:
                message = b""
                while True:
                    read = ctypes.c_uint(0)
                    ok = k32.ReadFile(
                        self._handle, buffer, len(buffer), ctypes.byref(read), None
                    )
                    message += buffer.raw[: read.value]
                    if ok:
                        break
                    if ctypes.get_last_error() != ERROR_MORE_DATA:
                        break
                if message:
                    try:
                        callback(message)
                    except Exception:
                        logger.exception("Forwarded launch could not be handled")
            finally:
                k32.DisconnectNamedPipe(self._handle)

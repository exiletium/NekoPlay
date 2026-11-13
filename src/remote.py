# remote.py
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

"""Hand a launch to the instance that is already running.

Imported by the launcher before anything else of ours, so it imports
nothing but os and ctypes: every millisecond spent here is spent before
the running instance even hears of the launch.
"""

import ctypes
import os


# What may stand for itself inside a quoted GVariant string below, and in
# a file URI path without escaping.
_URI_SAFE = frozenset(
    b"abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789/-_.~"
)
_GVARIANT_SAFE = frozenset(chr(c) for c in range(0x20, 0x7F)) - {"'", '"', "\\"}


def _file_uri(path: str) -> str:
    raw = os.fsencode(os.path.abspath(path))
    return "file://" + "".join(
        chr(b) if b in _URI_SAFE else "%%%02X" % b for b in raw
    )


def _uri_scheme(arg: str) -> bool:
    """Whether *arg* starts with a URI scheme, as GLib decides when it turns
    a command-line argument into a file."""
    scheme, sep, _rest = arg.partition(":")
    return bool(
        sep
        and scheme[:1].isascii()
        and scheme[:1].isalpha()
        and all(c.isascii() and (c.isalnum() or c in "+.-") for c in scheme)
    )


def forward_to_running_instance(app_id: str, argv: list) -> int | None:
    """Hand this launch to an instance that is already running, before GTK
    is loaded.

    GApplication does this too, but only once gi, GTK and the whole app are
    imported - about 200 ms before the running instance even hears of it.
    Here it is one D-Bus call made through libgio with ctypes, the same one
    GApplication would make: files become an Open, a bare --new-window an
    Activate. Returns the exit status if the launch was handed over, or
    None to start up as usual: when nothing is running, when the launch has
    an option this does not know, or has neither files nor --new-window
    (GApplication answers that one; see do_handle_local_options).
    """
    if os.name == "nt":
        return None
    new_window = False
    uris = []
    for arg in argv:
        if arg in ("-n", "--new-window"):
            new_window = True
        elif arg.startswith("-") or not arg:
            return None
        elif _uri_scheme(arg):
            if not set(arg) <= _GVARIANT_SAFE:
                return None
            uris.append(arg)
        else:
            uris.append(_file_uri(arg))
    if not new_window:
        return None

    # Lets the running instance raise its window over ours on Wayland and
    # X11; GApplication passes these on the same way.
    platform_data = []
    for key, env in (
        ("activation-token", "XDG_ACTIVATION_TOKEN"),
        ("desktop-startup-id", "DESKTOP_STARTUP_ID"),
    ):
        value = os.environ.get(env)
        if value and set(value) <= _GVARIANT_SAFE:
            platform_data.append("'%s': <'%s'>" % (key, value))
    data = "{%s}" % ", ".join(platform_data) if platform_data else "@a{sv} {}"
    if uris:
        method = "Open"
        params = "([%s], %s)" % (", ".join("'%s'" % u for u in uris), data)
    else:
        method = "Activate"
        params = "(%s,)" % data

    try:
        gio = ctypes.CDLL("libgio-2.0.so.0")
    except OSError:
        return None
    vp = ctypes.c_void_p
    gio.g_bus_get_sync.restype = vp
    gio.g_bus_get_sync.argtypes = [ctypes.c_int, vp, ctypes.POINTER(vp)]
    gio.g_variant_parse.restype = vp
    gio.g_variant_parse.argtypes = [vp, ctypes.c_char_p, vp, vp, ctypes.POINTER(vp)]
    gio.g_dbus_connection_call_sync.restype = vp
    gio.g_dbus_connection_call_sync.argtypes = [
        vp, ctypes.c_char_p, ctypes.c_char_p, ctypes.c_char_p, ctypes.c_char_p,
        vp, vp, ctypes.c_int, ctypes.c_int, vp, ctypes.POINTER(vp),
    ]

    error = vp()
    G_BUS_TYPE_SESSION = 2
    connection = gio.g_bus_get_sync(G_BUS_TYPE_SESSION, None, ctypes.byref(error))
    if not connection:
        return None
    variant = gio.g_variant_parse(None, params.encode(), None, None, ctypes.byref(error))
    if not variant:
        return None
    # Not started for us if it is not running: a launch from here should
    # become the instance itself, as it would without this.
    G_DBUS_CALL_FLAGS_NO_AUTO_START = 1
    reply = gio.g_dbus_connection_call_sync(
        connection,
        app_id.encode(),
        ("/" + app_id.replace(".", "/").replace("-", "_")).encode(),
        b"org.freedesktop.Application",
        method.encode(),
        variant,
        None,
        G_DBUS_CALL_FLAGS_NO_AUTO_START,
        -1,
        None,
        ctypes.byref(error),
    )
    return 0 if reply else None

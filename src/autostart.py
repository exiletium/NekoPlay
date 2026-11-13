# autostart.py
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

"""Start NekoPlay in the background when the user logs in.

Running in the background makes every video after the first open at once;
starting with the session makes the first one open at once too. Outside a
sandbox that is an XDG autostart entry, inside Flatpak a request to the
Background portal, which writes the entry for us once the user agrees.
"""

import logging
import os
import sys
from gettext import gettext as _

from gi.repository import Gio, GLib

from .mpris import APP_ID
from .utils import is_flatpak

logger = logging.getLogger(__name__)

# Marks the entry as ours, so that one the user or their distribution put
# there is never rewritten or removed.
_MARKER = "X-NekoPlay-Background=true"


def _entry_path() -> str:
    return os.path.join(GLib.get_user_config_dir(), "autostart", APP_ID + ".desktop")


def _exec_quote(arg: str) -> str:
    """Quote *arg* for an Exec key, as the Desktop Entry spec asks."""
    arg = arg.replace("%", "%%")
    if not any(c in arg for c in ' \t\n"\'\\><~|&;$*?#()`'):
        return arg
    for c in '\\"`$':
        arg = arg.replace(c, "\\" + c)
    return '"%s"' % arg


def _entry(launcher: str) -> str:
    return "\n".join(
        (
            "[Desktop Entry]",
            "Type=Application",
            "Name=NekoPlay",
            "Comment=" + _("Keeps NekoPlay ready to open videos instantly"),
            # Exec is a string value too, so its backslashes are escaped twice.
            "Exec=%s --gapplication-service" % _exec_quote(launcher).replace("\\", "\\\\"),
            "Icon=" + APP_ID,
            "NoDisplay=true",
            "X-GNOME-Autostart-enabled=true",
            _MARKER,
            "",
        )
    )


def _sync_entry(enabled: bool) -> None:
    path = _entry_path()
    try:
        with open(path, encoding="utf-8") as f:
            current = f.read()
    except FileNotFoundError:
        current = None
    except OSError:
        logger.exception("Could not read %s", path)
        return
    if current is not None and _MARKER not in current:
        return  # not ours

    if not enabled:
        if current is not None:
            try:
                os.remove(path)
            except OSError:
                logger.exception("Could not remove %s", path)
        return

    # The script that is running now: from a D-Bus activation that is the
    # installed one, and a launch from elsewhere should come back the same.
    wanted = _entry(os.path.abspath(sys.argv[0]))
    if current == wanted:
        return
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(wanted)
        os.replace(tmp, path)
    except OSError:
        logger.exception("Could not write %s", path)


def _request_portal(enabled: bool, settings: Gio.Settings) -> None:
    """Ask the Background portal; it may show the user a dialog, so only
    when what we want has changed since we last asked."""
    if settings.get_boolean("autostart-requested") == enabled:
        return
    try:
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    except GLib.Error:
        logger.exception("No session bus for the Background portal")
        return
    options = {
        "reason": GLib.Variant("s", _("Open videos instantly")),
        "autostart": GLib.Variant("b", enabled),
        "commandline": GLib.Variant("as", ["nekoplay", "--gapplication-service"]),
        "dbus-activatable": GLib.Variant("b", False),
    }

    def done(connection, result):
        try:
            connection.call_finish(result)
            settings.set_boolean("autostart-requested", enabled)
        except GLib.Error:
            logger.exception("The Background portal refused the request")

    bus.call(
        "org.freedesktop.portal.Desktop",
        "/org/freedesktop/portal/desktop",
        "org.freedesktop.portal.Background",
        "RequestBackground",
        GLib.Variant("(sa{sv})", ("", options)),
        None,
        Gio.DBusCallFlags.NONE,
        -1,
        None,
        done,
    )


def sync(enabled: bool, settings: Gio.Settings) -> None:
    """Start with the session when *enabled*, and stop doing so when not."""
    if os.name == "nt":
        return
    if is_flatpak:
        _request_portal(enabled, settings)
    else:
        _sync_entry(enabled)

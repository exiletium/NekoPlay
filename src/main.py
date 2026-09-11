# main.py
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

import logging
import os
import sys
from gettext import gettext as _
from typing import cast

import gi

gi.require_version("Adw", "1")
gi.require_version("Gdk", "4.0")
gi.require_version("Gio", "2.0")
gi.require_version("GLib", "2.0")
gi.require_version("Gtk", "4.0")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk

from . import probe
from .mpris import MPRIS
from .platform_compat import IS_WINDOWS, round_new_windows, trace
from .preferences import Preferences, settings
from .save_session import is_same_playlist
from .window import CineWindow

logger = logging.getLogger(__name__)

if IS_WINDOWS:
    # Naming the renderer saves GTK from trying the others first. It settles
    # on the GL renderer here anyway, but only after an attempt that costs
    # about 75 ms of a startup that is under a second to begin with.
    # setdefault, so GSK_RENDERER=ngl still works as an escape hatch.
    os.environ.setdefault("GSK_RENDERER", "gl")
else:
    os.environ["GSK_RENDERER"] = "gl"

    # Set the icon shown in gnome sound settings
    os.environ["PIPEWIRE_PROPS"] = '{application.icon-name="moe.nyarchlinux.nekoplay"}'

# Note: GDK_WIN32_FORCE_DCOMP cannot be set from here - importing
# gi.repository.Gtk above has already loaded GDK. It is set in nekoplay.in,
# before anything touches gi. See the comment there.


class CineApplication(Adw.Application):
    """The main application singleton class."""

    def __init__(self, single_instance=None):
        super().__init__(
            application_id="moe.nyarchlinux.nekoplay",
            flags=Gio.ApplicationFlags.HANDLES_OPEN,
            resource_base_path="/moe/nyarchlinux/nekoplay",
        )
        # The named pipe claimed in nekoplay.in, if this is Windows and we
        # are the primary; served once startup is done.
        self._single_instance = single_instance

        self.add_main_option(
            "new-window",
            ord("n"),
            GLib.OptionFlags.NONE,
            GLib.OptionArg.NONE,
            "Open a new window",
            None,
        )

        self.connect("shutdown", self._on_shutdown)

    # GTK reserves a margin around a client-side-decorated window for its drop
    # shadow, and expects the surface to have an alpha channel so the margin
    # stays invisible. Windows surfaces do not get one, so that margin paints
    # solid black and the window sits inside a thick black frame. The same
    # goes for every popover, menu and dropdown list, each of which is a
    # surface of its own. Dropping the shadows removes the frames; the
    # rounded corners come back from DWM, asked for per window in
    # platform_compat (round_window_corners, round_new_windows).
    WINDOWS_CSS = b"""
    window.csd {
      box-shadow: none;
      margin: -12px;
      border: none;
      border-radius: 0;
    }

    window.csd:backdrop {
      box-shadow: none;
    }

    popover > contents {
      box-shadow: none;
      border-radius: 8px;
    }

    /* The list under a dropdown is set 6px off its button by padding on
       the popover node itself, which is transparent - so black here. */
    dropdown popover.menu,
    combobox popover.menu {
      padding-top: 0;
    }
    """

    def do_startup(self):
        self.mpris = MPRIS(self)
        trace("mpris built")

        Adw.Application.do_startup(self)
        trace("Adw startup done")

        if IS_WINDOWS:
            round_new_windows()
            provider = Gtk.CssProvider()
            provider.load_from_data(self.WINDOWS_CSS)
            Gtk.StyleContext.add_provider_for_display(
                Gdk.Display.get_default(),
                provider,
                Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION + 1,
            )
        Adw.StyleManager.get_default().props.color_scheme = Adw.ColorScheme.FORCE_DARK

        self._create_action("new-window", lambda *a: self.activate(), ["<primary>n"])
        self._create_action("quit", lambda *a: self.quit(), ["<primary>q"])
        self._create_action("about", self._on_about_action)
        self._create_action(
            "preferences", self.on_preferences_action, ["<primary>comma"]
        )

        if self._single_instance is not None:
            self._single_instance.serve(self._on_forwarded_launch)

        trace("app started")

    def _on_forwarded_launch(self, payload: bytes) -> None:
        """A second launch sent us its arguments (pipe thread)."""
        import json

        try:
            message = json.loads(payload.decode("utf-8"))
            cwd = str(message.get("cwd") or "")
            argv = [str(a) for a in message.get("argv") or []]
        except (ValueError, AttributeError):
            logger.warning("Ignoring a malformed forwarded launch")
            return
        GLib.idle_add(self._open_forwarded, argv, cwd)

    def _open_forwarded(self, argv: list, cwd: str) -> bool:
        """Main loop: what GApplication does for a remote launch on Linux."""
        files = [
            Gio.File.new_for_commandline_arg_and_cwd(arg, cwd)
            for arg in argv
            if not arg.startswith("-")
        ]
        if files:
            self.open(files, "")
        elif "--new-window" in argv or "-n" in argv:
            self.activate()
        else:
            print("NekoPlay is running; to open a new window, use --new-window.")
        return False

    def do_activate(self):
        win = CineWindow(application=self, is_activate=True)
        win.present()
        trace("window presented")

    def do_open(self, files, n_files, hint):
        win: CineWindow = cast(CineWindow, self.props.active_window)
        open_new = settings.get_boolean("open-new-windows") or not win

        if open_new:
            win = CineWindow(application=self)
            win.start_page.set_visible(False)

            first_video_path = None
            for gfile in files:
                first_video_path = self.find_first_file(gfile)

                if first_video_path:
                    break

            trace("first file found")

            if first_video_path:
                # Sizes the window before the first frame arrives. Usually
                # already answered: probe.prefetch started this while the
                # process was still loading GTK.
                size = probe.video_size(first_video_path)
                if size:
                    win.set_window_size(*size)
                trace("size known")
            win.present()
            trace("window presented")
        else:
            win.present()
            if is_same_playlist(win.mpv.playlist):
                win.mpv.write_watch_later_config()
            win.mpv.stop()

        for gfile in files:
            path = gfile.get_path() or gfile.get_uri()
            if path:
                win.mpv.loadfile(path, "append-play")

        trace("loadfile issued")

        for window in self.get_windows():
            w = cast(CineWindow, window)
            # Pause previous opened windows
            w.mpv.pause = w != win

        win.hide_ui_timeout()

    def find_first_file(self, gfile, visited=None):
        """Local-only recursive search."""
        if gfile.get_uri_scheme() != "file":
            return None

        if visited is None:
            visited = set()

        path = gfile.get_path()
        if not path or path in visited:
            return None
        visited.add(path)

        try:
            info = gfile.query_info(
                "standard::type", Gio.FileQueryInfoFlags.NOFOLLOW_SYMLINKS, None
            )
            f_type = info.get_file_type()

            if f_type == Gio.FileType.REGULAR:
                return path

            if f_type == Gio.FileType.DIRECTORY:
                enumerator = gfile.enumerate_children(
                    "standard::name,standard::type",
                    Gio.FileQueryInfoFlags.NOFOLLOW_SYMLINKS,
                    None,
                )

                subdirectories = []
                for child in enumerator:
                    child_type = child.get_file_type()
                    name = child.get_name()

                    if name.startswith("."):
                        continue

                    if child_type == Gio.FileType.REGULAR:
                        return gfile.get_child(name).get_path()
                    elif child_type == Gio.FileType.DIRECTORY:
                        subdirectories.append(gfile.get_child(name))

                for folder in subdirectories:
                    found = self.find_first_file(folder, visited)
                    if found:
                        return found
        except Exception:
            logger.exception("find_first_file failed")
        return None

    # From showtime
    def do_handle_local_options(self, options: GLib.VariantDict):
        """Handle local command line arguments."""
        self.register()  # This is so props.is_remote works

        if self.props.is_remote:
            if options.contains("new-window"):
                return -1

            print("NekoPlay is running; to open a new window, use --new-window.")
            return 0

        return -1

    def on_preferences_action(self, *args):
        """Callback for the app.preferences action."""
        preferences = Preferences(self.props.active_window)
        preferences.present(self.props.active_window)

    def _on_about_action(self, *args):
        """Callback for the app.about action."""
        APP_VERSION = sys.modules["__main__"].VERSION
        about = Adw.AboutDialog(
            application_name=_("NekoPlay"),
            application_icon="moe.nyarchlinux.nekoplay",
            developer_name="Diego Povliuk",
            version=APP_VERSION,
            copyright="© 2026 Diego Povliuk",
            issue_url="https://github.com/NyarchLinux/NekoPlay/issues",
            license_type=Gtk.License.GPL_3_0,
        )
        try:
            # Translators: Replace "translator-credits" with your name/username, and optionally an email or URL.
            about.set_translator_credits(_("translator-credits"))
        except NameError:
            pass

        about.add_acknowledgement_section(
            None,
            [
                "MPV https://mpv.io/",
                "python-mpv https://pypi.org/project/python-mpv/",
                "Celluloid https://celluloid-player.github.io/",
                "Showtime https://apps.gnome.org/Showtime/",
                "Workbench https://apps.gnome.org/Workbench/",
            ],
        )

        about.add_link(
            "Sponsor on GitHub",
            "https://github.com/sponsors/diegopvlk",
        )

        about.add_link(
            "Donate (PayPal)",
            "https://www.paypal.com/donate?hosted_button_id=DVL7H35GA66X6",
        )

        about.add_link(
            "Doar (Pix): diego.pvlk@gmail.com",
            "diego.pvlk@gmail.com",
        )

        about.add_other_app(
            "io.github.diegopvlk.Dosage", "Dosage", "Keep track of your treatments"
        )

        about.add_other_app(
            "io.github.diegopvlk.Tomatillo", "Tomatillo", "Focus better, work smarter"
        )

        about.present(self.props.active_window)

    def _create_action(self, name, callback, shortcuts=None):
        """Add an application action."""
        action = Gio.SimpleAction.new(name, None)
        action.connect("activate", callback)
        self.add_action(action)
        if shortcuts:
            self.set_accels_for_action(f"app.{name}", shortcuts)

    def _on_shutdown(self, *args):
        for win in self.get_windows():
            win.close()


def main(version, single_instance=None):
    """The application's entry point."""
    app = CineApplication(single_instance)
    return app.run(sys.argv)

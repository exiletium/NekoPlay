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

from . import autostart, probe
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

# The app forces libadwaita's dark style (see do_startup), but the style
# manager is created following the system's preference, which is light on
# most desktops, and switching it then cost 30-55 ms of startup restyling
# every widget. Telling it to start dark makes the switch a no-op. Only
# libadwaita reads this, and only while starting up; do_startup removes it
# again so that nothing launched from here inherits it.
_PRESET_DARK = "ADW_DEBUG_COLOR_SCHEME" not in os.environ
if _PRESET_DARK:
    os.environ["ADW_DEBUG_COLOR_SCHEME"] = "prefer-dark"

# Note: GDK_WIN32_FORCE_DCOMP cannot be set from here - importing
# gi.repository.Gtk above has already loaded GDK. It is set in nekoplay.in,
# before anything touches gi. See the comment there.


# How long after startup, or after the last one was taken, to build the
# next window ahead of time: late enough to stay out of the way of the
# window just opened, which is still loading its file.
SPARE_WINDOW_DELAY_MS = 3000
# And how much later to realize it, in a main loop turn of its own.
SPARE_PREPARE_DELAY_MS = 50
# Settings a window reads when it is built that change in everyday use, and
# that the spare window catches up on when it is taken
# (CineWindow.refresh_from_settings). Any other change rebuilds it.
SPARE_REFRESHED_KEYS = frozenset(
    (
        "volume",
        "subtitle-scale",
        "loop-state",
        "show-remaining",
        "is-maximized",
        "autostart-requested",
    )
)


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

        # A window built ahead of time and kept hidden, out of the
        # application's list, for the next file opened (see _take_window).
        self._spare: CineWindow | None = None
        self._spare_id = 0
        # Whether we hold the application, which keeps it running with no
        # windows left (the run-in-background setting).
        self._background = False

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
        if _PRESET_DARK:
            os.environ.pop("ADW_DEBUG_COLOR_SCHEME", None)

        self._create_action("new-window", lambda *a: self.activate(), ["<primary>n"])
        self._create_action("quit", lambda *a: self.quit(), ["<primary>q"])
        self._create_action("about", self._on_about_action)
        self._create_action(
            "preferences", self.on_preferences_action, ["<primary>comma"]
        )

        if self._single_instance is not None:
            self._single_instance.serve(self._on_forwarded_launch)

        settings.connect("changed", self._on_setting_changed)
        self._sync_background()
        # Off the startup path: it touches the disk, or the session bus.
        GLib.timeout_add(SPARE_WINDOW_DELAY_MS, self._sync_autostart)

        trace("app started")

    def _sync_autostart(self):
        autostart.sync(settings.get_boolean("run-in-background"), settings)
        return GLib.SOURCE_REMOVE

    def _on_setting_changed(self, _settings, key):
        if key == "run-in-background":
            self._sync_background()
            self._sync_autostart()
        elif key not in SPARE_REFRESHED_KEYS:
            # The spare window was built with the old value.
            self._drop_spare()
            self._schedule_spare()

    def _sync_background(self):
        """Hold the application, and keep a window ready, while the
        run-in-background setting is on.

        Only when the desktop started us: GNOME, file managers and the like
        launch NekoPlay through D-Bus activation, as a service, and send
        every later launch to it there - so they all find it running. A
        launch from a terminal ends with its last window as it always did,
        rather than leave the terminal waiting on a process with nothing on
        screen; it hands its files to a running service if there is one.
        """
        wanted = settings.get_boolean("run-in-background") and bool(
            self.get_flags() & Gio.ApplicationFlags.IS_SERVICE
        )
        if wanted == self._background:
            return
        self._background = wanted
        if wanted:
            self.hold()
            self._schedule_spare()
        else:
            self._drop_spare()
            self.release()

    def _schedule_spare(self):
        if self._background and self._spare is None and not self._spare_id:
            self._spare_id = GLib.timeout_add(SPARE_WINDOW_DELAY_MS, self._build_spare)

    def _build_spare(self):
        """Build the next window now, while nobody is waiting for it."""
        self._spare_id = 0
        if not self._background or self._spare is not None:
            return GLib.SOURCE_REMOVE
        try:
            win = CineWindow(application=self)
            # Built as one of ours, then taken off the list until it is
            # shown: hidden, it would still count as the active window,
            # which is where files open and what the media controls follow.
            self.remove_window(win)
        except Exception:
            logger.exception("Could not build a window ahead of time")
            return GLib.SOURCE_REMOVE
        self._spare = win
        # Realized in a turn of its own, so that neither step holds up the
        # main loop for long if another window is playing. A timeout rather
        # than an idle: a window rendering video can keep idles waiting.
        GLib.timeout_add(SPARE_PREPARE_DELAY_MS, self._prepare_spare, win)
        return GLib.SOURCE_REMOVE

    def _prepare_spare(self, win):
        if self._spare is win:
            try:
                win.prepare()
            except Exception:
                logger.exception("Could not prepare a window ahead of time")
            else:
                GLib.timeout_add(SPARE_PREPARE_DELAY_MS, self._warm_spare, win)
        return GLib.SOURCE_REMOVE

    def _warm_spare(self, win):
        if self._spare is win:
            try:
                win.warm_up(lambda: trace("spare warmed up"))
            except Exception:
                logger.exception("Could not draw ahead of time")
        return GLib.SOURCE_REMOVE

    def _drop_spare(self):
        if self._spare_id:
            GLib.source_remove(self._spare_id)
            self._spare_id = 0
        win, self._spare = self._spare, None
        if win is not None:
            win.close()

    def _take_window(self, is_activate=False) -> CineWindow:
        """A window to show: the one built ahead of time if there is one,
        caught up with the settings, otherwise a new one."""
        win, self._spare = self._spare, None
        if win is not None and win.warming_up:
            # Still playing its clip for the fraction of a second that
            # takes; what it would hear of it is not for a window in use.
            win.close()
            win = None
        if win is not None and not win.mpv.core_shutdown:
            self.add_window(win)
            win.refresh_from_settings()
            if is_activate and settings.get_boolean("save-session"):
                win.restore_session()
        else:
            win = CineWindow(application=self, is_activate=is_activate)
        self._schedule_spare()
        return win

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
        win = self._take_window(is_activate=True)
        win.present()
        trace("window presented")

    def do_open(self, files, n_files, hint):
        win: CineWindow = cast(CineWindow, self.props.active_window)
        # A window whose player has shut down is only waiting to close.
        open_new = (
            settings.get_boolean("open-new-windows") or not win or win.mpv.core_shutdown
        )

        def queue_loads():
            for gfile in files:
                # Local paths go over as bytes: a name that is not valid
                # UTF-8 arrives here with surrogate escapes, and as a str
                # python-mpv fails to encode it.
                path = gfile.get_path()
                target = os.fsencode(path) if path else gfile.get_uri()
                if not target:
                    continue
                try:
                    win.mpv.command_async("loadfile", target, "append-play")
                except Exception:
                    # This can run before the window is shown, and nothing
                    # here may keep it from being shown.
                    logger.exception("Could not queue %r", target)
            trace("loadfile issued")

        loads_queued = False
        sized_by_mpv = False
        if open_new:
            win = self._take_window()
            win.start_page.set_visible(False)
            # A window prepared ahead of time is shown as soon as mpv,
            # which opens the files the moment it gets them, has the size.
            sized_by_mpv = win.can_size_from_mpv
            if sized_by_mpv:
                win.present_when_sized()

            # Before anything below, all of which can wait: mpv opens the
            # file while this thread finds the window's size and realizes
            # it, and holds the load just short of needing the render
            # context until that exists.
            if win.can_load_before_present:
                queue_loads()
                loads_queued = True

            if not sized_by_mpv:
                first_video_path = None
                for gfile in files:
                    first_video_path = self.find_first_file(gfile)

                    if first_video_path:
                        break

                trace("first file found")

                if first_video_path:
                    # Sizes the window before the first frame arrives.
                    # Usually already answered: probe.prefetch started this
                    # while the process was still loading GTK.
                    size = probe.video_size(first_video_path)
                    if size:
                        win.set_window_size(*size)
                    trace("size known")
                win.present()
                trace("window presented")
        else:
            win.present()
            # Everything below is queued rather than waited for: mpv runs
            # commands in the order they arrive, and this window may still be
            # busy opening the last file it was handed.
            if is_same_playlist(win.mpv.playlist):
                win.mpv.command_async("write-watch-later-config")
            win.mpv.command_async("stop")

        if not loads_queued:
            queue_loads()

        for window in self.get_windows():
            w = cast(CineWindow, window)
            if w.mpv.core_shutdown:
                continue
            # Pause previous opened windows
            w.mpv.command_async("set", "pause", "yes" if w != win else "no")

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
        self._drop_spare()
        for win in self.get_windows():
            win.close()


def main(version, single_instance=None):
    """The application's entry point."""
    app = CineApplication(single_instance)
    return app.run(sys.argv)

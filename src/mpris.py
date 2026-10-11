# mpris.py
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
from gettext import gettext as _

import gi
import mpv

gi.require_version("Adw", "1")
gi.require_version("Gio", "2.0")
gi.require_version("GLib", "2.0")
from gi.repository import Adw, Gio, GLib

from .platform_compat import IS_WINDOWS
from .utils import idle_add_once

logger = logging.getLogger(__name__)

APP_ID = "moe.nyarchlinux.nekoplay"
MEDIAPLAYER2_PLAYER = "org.mpris.MediaPlayer2.Player"

# some gnome extensions can spam properties which can cause frame drops
INTERFACE = """
<!DOCTYPE node PUBLIC
'-//freedesktop//DTD D-BUS Object Introspection 1.0//EN'
'http://www.freedesktop.org/standards/dbus/1.0/introspect.dtd'>
<node>
    <interface name='org.mpris.MediaPlayer2'>
        <method name='Raise'/>
        <method name='Quit'/>
        <property name='Identity' type='s' access='read'/>
        <property name='DesktopEntry' type='s' access='read'/>
        <property name='CanQuit' type='b' access='read'/>
        <property name='CanRaise' type='b' access='read'/>
        <property name='HasTrackList' type='b' access='read'/>
        <property name='SupportedUriSchemes' type='as' access='read'/>
        <property name='SupportedMimeTypes' type='as' access='read'/>
    </interface>
    <interface name='org.mpris.MediaPlayer2.Player'>
        <method name='Next'/>
        <method name='Previous'/>
        <method name='Pause'/>
        <method name='PlayPause'/>
        <method name='Stop'/>
        <method name='Play'/>
        <method name='Seek'>
            <arg direction='in' name='Offset' type='x'/>
        </method>
        <method name='SetPosition'>
            <arg direction='in' name='TrackId' type='o'/>
            <arg direction='in' name='Position' type='x'/>
        </method>
        <signal name='Seeked'>
            <arg name='Position' type='x'/>
        </signal>
        <property name='PlaybackStatus' type='s' access='read'/>
        <property name='Metadata' type='a{sv}' access='read'/>
        <property name='CanSeek' type='b' access='read'/>
        <property name='LoopStatus' type='s' access='readwrite'/>
        <property name='Volume' type='d' access='readwrite'/>
        <property name='Position' type='x' access='read'/>
        <property name='CanGoNext' type='b' access='read'/>
        <property name='CanGoPrevious' type='b' access='read'/>
        <property name='CanPlay' type='b' access='read'/>
        <property name='CanPause' type='b' access='read'/>
        <property name='CanControl' type='b' access='read'/>
        <property name='Shuffle' type='b' access='readwrite'/>
    </interface>
</node>
"""


class MPRIS:
    def __init__(self, app: Adw.Application) -> None:
        self._app = app
        self._bus_name = f"org.mpris.MediaPlayer2.{APP_ID}"
        self._path = "/org/mpris/MediaPlayer2"
        self._con = None

        if IS_WINDOWS:
            # MPRIS is a freedesktop spec spoken over the D-Bus session bus,
            # and Windows has neither. Every update_* method below already
            # returns early while _con is None, so the object stays usable
            # and callers do not have to know.
            return

        Gio.bus_get(Gio.BusType.SESSION, None, self._on_bus_acquired)

        self._app.connect("notify::active-window", self.update_props)

    def _on_bus_acquired(self, _source, res):
        def register():
            try:
                self._con = Gio.bus_get_finish(res)
                Gio.bus_own_name_on_connection(
                    self._con, self._bus_name, Gio.BusNameOwnerFlags.NONE, None, None
                )

                node_info = Gio.DBusNodeInfo.new_for_xml(INTERFACE)
                for interface in node_info.interfaces:
                    self._con.register_object_with_closures2(
                        object_path=self._path,
                        interface_info=interface,
                        method_call_closure=self._on_method_call,
                        get_property_closure=self._on_get_property,
                        set_property_closure=self._on_set_property,
                    )
            except Exception:
                logger.exception("MPRIS Bus failed")

        # Without idle_add some gnome mpris extensions can freeze the
        # whole shell for about a minute. Not unique to cine, it can also
        # happen with other players that uses mpris.
        idle_add_once(register)

    def _emit_props_changed(self, changed_props):
        if not self._con:
            return

        self._con.emit_signal(
            None,
            self._path,
            "org.freedesktop.DBus.Properties",
            "PropertiesChanged",
            GLib.Variant("(sa{sv}as)", (MEDIAPLAYER2_PLAYER, changed_props, [])),
        )

    def update_props(self, *args):
        """Notifies D-Bus that properties have changed."""
        if not self._con:
            return

        self._emit_props_changed(
            {
                "Identity": GLib.Variant("s", _("NekoPlay")),
                "DesktopEntry": GLib.Variant("s", APP_ID),
            },
        )

        if self._mpv:
            status = "Paused" if self._prop("pause") else "Playing"
            vol = self._prop("volume", 0)
            loop = self._get_loop_status()

            self._emit_props_changed(
                {
                    "PlaybackStatus": GLib.Variant("s", status),
                    "LoopStatus": GLib.Variant("s", loop),
                    "Metadata": self._get_metadata_variant()
                    if not self._prop("idle-active", True)
                    else GLib.Variant("a{sv}", {}),
                    "CanPlay": GLib.Variant("b", True),
                    "CanPause": GLib.Variant("b", True),
                    "CanSeek": GLib.Variant("b", True),
                    "CanControl": GLib.Variant("b", True),
                    "Volume": GLib.Variant("d", float(vol / 100.0)),
                    "CanGoPrevious": GLib.Variant("b", self._can_go_prev),
                    "CanGoNext": GLib.Variant("b", self._can_go_next),
                    "Shuffle": GLib.Variant("b", self._shuffle),
                },
            )

    @property
    def connected(self) -> bool:
        """Whether anyone can hear us. Never on Windows."""
        return self._con is not None

    @property
    def _mpv(self):
        win = self._app.props.active_window
        return getattr(win, "mpv", None)

    def _prop(self, name, default=None):
        """The active window's last known value of an mpv property.

        Everything here runs on the main thread, where asking mpv directly
        waits for its core - and D-Bus clients such as the shell's media
        controls ask for Position and Metadata whenever they like.
        """
        win = self._app.props.active_window
        prop = getattr(win, "prop", None)
        return prop(name, default) if prop else default

    @property
    def _can_go_prev(self):
        win = self._app.props.active_window
        return getattr(win, "can_go_prev", False)

    @property
    def _can_go_next(self):
        win = self._app.props.active_window
        return getattr(win, "can_go_next", False)

    @property
    def _shuffle(self):
        win = self._app.props.active_window
        return win.shuffle_toggle_btn.props.active if win else False  # type: ignore

    def _get_loop_status(self):
        try:
            p = self._mpv
            if not p:
                return "None"

            # mpv loop-playlist can be 'inf', 'no', or a number
            loop_playlist = self._prop("loop-playlist", "no")
            loop_file = self._prop("loop-file", "no")

            if loop_file == "inf":
                return "Track"
            if loop_playlist == "inf":
                return "Playlist"
        except mpv.ShutdownError:
            pass

        return "None"

    def update_playback_status(self, paused):
        if not self._con:
            return
        status = "Paused" if paused else "Playing"
        self._emit_props_changed({"PlaybackStatus": GLib.Variant("s", status)})

    def update_volume(self, value):
        if not self._con:
            return
        vol = value / 100.0
        self._emit_props_changed({"Volume": GLib.Variant("d", float(vol))})

    def update_metadata(self):
        if not self._con:
            return
        metadata = self._get_metadata_variant()
        self._emit_props_changed({"Metadata": metadata})

    def update_loop(self):
        if not self._con:
            return
        current_loop = self._get_loop_status()
        self._emit_props_changed({"LoopStatus": GLib.Variant("s", current_loop)})

    def update_can_prev_next(self, can_prev, can_next):
        if not self._con:
            return
        self._emit_props_changed({"CanGoPrevious": GLib.Variant("b", can_prev)})
        self._emit_props_changed({"CanGoNext": GLib.Variant("b", can_next)})

    def update_shuffle(self, shuffle_active):
        self._emit_props_changed({"Shuffle": GLib.Variant("b", shuffle_active)})

    def _get_metadata_variant(self):
        """Constructs the MPRIS Metadata dictionary."""

        metadata = {}

        try:
            duration = self._prop("duration", 0)

            metadata = {
                "mpris:trackid": GLib.Variant("o", "/org/mpris/MediaPlayer2/Track/0"),
                "mpris:length": GLib.Variant("x", int(duration * 1_000_000)),
            }

            win = self._app.props.active_window
            window_title = win.props.title if win else None
            title = window_title or self._prop("media-title")

            if title:
                metadata["xesam:title"] = GLib.Variant("s", str(title))

            md = self._prop("metadata", {})

            if artist := (md or {}).get("artist"):
                metadata["xesam:artist"] = GLib.Variant("as", [str(artist)])
        except mpv.ShutdownError:
            pass

        return GLib.Variant("a{sv}", metadata)

    def _on_method_call(
        self, _con, _sender, _path, interface, method, params, invocation
    ):
        idle_add_once(self._handle_method, method, params)
        invocation.return_value(None)

    def _handle_method(self, method, params):
        try:
            win = self._app.props.active_window
            p = self._mpv
            assert p and win

            win.skip_pause_obs_count = 0  # type: ignore

            if method == "PlayPause":
                p.command_async("cycle", "pause")
            elif method == "Pause":
                p.command_async("set", "pause", "yes")
            elif method == "Play":
                p.command_async("set", "pause", "no")
            elif method == "Previous":
                if win.can_go_prev:  # type: ignore
                    win.on_previous_clicked()  # type: ignore
            elif method == "Next":
                if win.can_go_next:  # type: ignore
                    win.on_next_clicked()  # type: ignore
            elif method == "Stop":
                p.command_async("stop")
                self.update_props()
            elif method == "Seek":
                offset_usec = params.get_child_value(0).get_int64()
                target = self._prop("time-pos", 0) + (offset_usec / 1_000_000.0)
                p.command_async("seek", target, "absolute")
                self.emit_seeked(target)
            elif method == "SetPosition":
                pos_usec = params.get_child_value(1).get_int64()
                target = pos_usec / 1_000_000.0
                p.command_async("seek", target, "absolute")
                self.emit_seeked(target)
            elif method == "Raise":
                win.present()
            elif method == "Quit":
                self._app.quit()
        except mpv.ShutdownError:
            pass
        except Exception:
            logger.exception("_handle_method failed")

    def emit_seeked(self, raw_pos=None):
        try:
            if not self._con or not self._mpv:
                return
            if raw_pos is None:
                raw_pos = self._prop("time-pos", 0)
            pos_usec = int(raw_pos * 1_000_000)
            self._con.emit_signal(
                None,
                self._path,
                MEDIAPLAYER2_PLAYER,
                "Seeked",
                GLib.Variant("(x)", (pos_usec,)),
            )
        except mpv.ShutdownError:
            pass

    def _on_get_property(self, _con, _sender, _path, interface, prop):
        try:
            p = self._mpv

            if interface == MEDIAPLAYER2_PLAYER:
                if prop == "CanGoPrevious":
                    return GLib.Variant("b", self._can_go_prev)
                elif prop == "CanGoNext":
                    return GLib.Variant("b", self._can_go_next)
                elif prop in ["CanPlay", "CanPause", "CanControl", "CanSeek"]:
                    return GLib.Variant("b", True)
                elif prop == "Volume":
                    vol = self._prop("volume", 0) / 100.0 if p else 0.0
                    return GLib.Variant("d", float(vol))
                elif prop == "PlaybackStatus":
                    status = "Paused" if (p and self._prop("pause")) else "Playing"
                    return GLib.Variant("s", status)
                elif prop == "LoopStatus":
                    return GLib.Variant("s", self._get_loop_status())
                elif prop == "Position":
                    raw_pos = self._prop("time-pos", 0)
                    pos = int(raw_pos * 1_000_000)
                    return GLib.Variant("x", pos)
                elif prop == "Metadata":
                    return self._get_metadata_variant()
                elif prop == "Shuffle":
                    return GLib.Variant("b", self._shuffle)

            elif interface == "org.mpris.MediaPlayer2":
                if prop == "Identity":
                    return GLib.Variant("s", _("NekoPlay"))
                elif prop == "DesktopEntry":
                    return GLib.Variant("s", APP_ID)
                elif prop in ["CanQuit", "CanRaise"]:
                    return GLib.Variant("b", True)
                elif prop == "HasTrackList":
                    return GLib.Variant("b", False)
                elif prop in ["SupportedUriSchemes", "SupportedMimeTypes"]:
                    return GLib.Variant("as", [])
        except mpv.ShutdownError:
            pass

        return None

    def _on_set_property(self, _con, _sender, _path, interface, prop, value):
        try:
            p = self._mpv
            if not p or interface != MEDIAPLAYER2_PLAYER:
                return False

            if prop == "Volume":
                new_vol = value.get_double()
                p.command_async("set", "volume", new_vol * 100.0)
                self._emit_props_changed({"Volume": GLib.Variant("d", float(new_vol))})
                return True

            if prop == "LoopStatus":
                new_loop = value.get_string()
                loop_file, loop_playlist = {
                    "Track": ("inf", "no"),
                    "Playlist": ("no", "inf"),
                }.get(new_loop, ("no", "no"))
                p.command_async("set", "loop-file", loop_file)
                p.command_async("set", "loop-playlist", loop_playlist)
                self._emit_props_changed({"LoopStatus": GLib.Variant("s", new_loop)})
                return True

            if prop == "Shuffle":
                new_shuffle = value.get_boolean()
                p._shuffle = new_shuffle
                if win := self._app.props.active_window:
                    btn = win.shuffle_toggle_btn  # type: ignore
                    btn.props.active = new_shuffle
                self._emit_props_changed({"Shuffle": GLib.Variant("b", new_shuffle)})
                return True
        except mpv.ShutdownError:
            pass

        return False

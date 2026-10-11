# window.py
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
# SPDX-License-Identifier: GPL-3.0-or-later

import bisect
import logging
import os
import shlex
import threading
from gettext import gettext as _
from typing import cast
from urllib.parse import urlparse

import gi
import mpv

gi.require_version("Adw", "1")
gi.require_version("Gio", "2.0")
gi.require_version("Gdk", "4.0")
gi.require_version("GLib", "2.0")
gi.require_version("Gtk", "4.0")
gi.require_version("GObject", "2.0")
from gi.repository import Adw, Gdk, Gio, GLib, GObject, Gtk

from .video2x import Video2X
from .history import HistoryDialog
from .mpris import MPRIS
from .mpv_gl_area import ThumbPreviewGLArea, VideoGLArea
from .options import OptionsMenuButton
from .platform_compat import (
    AUTOCREATE_PLAYLIST,
    MPV_PATH_SEP,
    round_window_corners,
    inhibit_idle,
    is_document_portal_path,
    strip_popover_arrows,
    TRACE,
    trace,
    uninhibit_idle,
)
from .playlist import Playlist, PlaylistItemObj
from .preferences import settings, sync_mpv_with_settings
from .save_session import (
    is_same_playlist,
    restore_last_playlist,
    save_last_playlist_file,
)
from .shortcuts import INTERNAL_BINDINGS, populate_shortcuts_dialog_mpv
from .utils import (
    CONFIG_DIR,
    INPUT_CONF,
    KEY_REMAP,
    MBTN_MAP,
    SCREENSHOT_DIR,
    SUB_EXTS,
    WATCH_HISTORY_JSONL,
    PrimaryClick,
    SecondaryClick,
    append_modifiers,
    display,
    format_time,
    get_mouse_bindings,
    has_host_permission,
    idle_add_once,
    is_local_path,
    parse_bindings,
    timeout_add_once,
    timeout_add_seconds_once,
)

logger = logging.getLogger(__name__)

gtk_setts: Gtk.Settings | None = Gtk.Settings.get_default()

DEFAULT_WIDTH, DEFAULT_HEIGHT = 1120, 630

# What the main thread needs to know about the player, kept current by
# observers so it never has to ask. See CineWindow.prop().
STATE_PROPS = (
    "chapter",
    "cursor-autohide-fs-only",
    "duration",
    "idle-active",
    "loop-file",
    "loop-playlist",
    "media-title",
    "metadata",
    "mute",
    "pause",
    "playlist-count",
    "playlist-pos",
    "sid",
    "speed",
    "time-pos",
    "volume",
)

# How long after the first frame the seek-bar thumbnail player opens its copy
# of the file, unless the pointer reaches the bar first. It is a second mpv
# with its own GL context, demuxer and decoder, and starting it alongside the
# main one put all of that in front of the first frame.
THUMB_PREVIEW_DELAY_MS = 1000


class MPV(mpv.MPV):
    """python-mpv, observing each property once however many handlers it has.

    python-mpv asks mpv to observe a property again for every handler added,
    mpv sends one event per observation, and python-mpv hands every event to
    every handler. Two handlers on time-pos made two events a frame and four
    calls; one observation per name makes it one event and two calls.
    """

    def observe_property(self, name, handler):
        handlers = self._property_handlers[name]
        if not handlers:
            super().observe_property(name, handler)
            return
        handlers.append(handler)
        # mpv sends a property's current value once, when it is first
        # observed - possibly before this handler was in the list. Observing
        # it afresh sends the value again, to every handler, and still
        # leaves one observation behind.
        key = hash(name) & 0xFFFFFFFFFFFFFFFF
        mpv._mpv_unobserve_property(self._event_handle, key)
        mpv._mpv_observe_property(
            self._event_handle, key, name.encode("utf-8"), mpv.MpvFormat.NODE
        )

    def __init__(self, *args, **kwargs):
        self._alive_lock = threading.Lock()
        self._dead = False
        super().__init__(*args, **kwargs)

        # python-mpv runs event callbacks before it frees the handle async
        # commands go through, so once this has run no command can start, and
        # while a command is under way this waits for it.
        @self.event_callback("shutdown")
        def on_shutdown(_event):
            with self._alive_lock:
                self._dead = True

    def command_async(self, name, *args, **kwargs):
        # python-mpv frees that handle as soon as the core shuts down and does
        # not check before using it, so a callback still queued when a window
        # closes would call into freed memory. The synchronous calls these
        # replaced raised ShutdownError there instead; so does this.
        with self._alive_lock:
            if self._dead:
                raise mpv.ShutdownError("libmpv core has been shutdown")
            return super().command_async(name, *args, **kwargs)


@Gtk.Template(resource_path="/moe/nyarchlinux/nekoplay/window.ui")
class CineWindow(Adw.ApplicationWindow):
    __gtype_name__ = "CineWindow"

    window_handle: Gtk.WindowHandle = Gtk.Template.Child()
    toast_overlay: Adw.ToastOverlay = Gtk.Template.Child()
    video_overlay: Gtk.Overlay = Gtk.Template.Child()
    start_page: Adw.StatusPage = Gtk.Template.Child()
    revealer_icon_indicator: Gtk.Revealer = Gtk.Template.Child()
    icon_indicator: Gtk.Image = Gtk.Template.Child()
    title_widget: Adw.WindowTitle = Gtk.Template.Child()
    headerbar: Adw.HeaderBar = Gtk.Template.Child()
    controls_box: Gtk.Box = Gtk.Template.Child()
    controls_wrap_box: Adw.WrapBox = Gtk.Template.Child()
    controls_separator: Gtk.Separator = Gtk.Template.Child()
    audio_only_icon: Gtk.Image = Gtk.Template.Child()
    revealer_ui: Gtk.Revealer = Gtk.Template.Child()
    revealer_drop_indicator: Gtk.Revealer = Gtk.Template.Child()
    drop_label: Gtk.Label = Gtk.Template.Child()
    drop_icon: Gtk.Image = Gtk.Template.Child()
    spinner: Adw.Spinner = Gtk.Template.Child()
    context_popover_menu: Gtk.PopoverMenu = Gtk.Template.Child()

    open_menu_btn: Gtk.MenuButton = Gtk.Template.Child()
    primary_menu_btn: Gtk.MenuButton = Gtk.Template.Child()
    previous_btn: Gtk.Button = Gtk.Template.Child()
    play_pause_btn: Gtk.Button = Gtk.Template.Child()
    next_btn: Gtk.Button = Gtk.Template.Child()
    skip_90_btn: Gtk.Button = Gtk.Template.Child()
    volume_menu_btn: Gtk.MenuButton = Gtk.Template.Child()
    mute_toggle_btn: Gtk.ToggleButton = Gtk.Template.Child()
    volume_box: Gtk.Box = Gtk.Template.Child()
    volume_scale: Gtk.Scale = Gtk.Template.Child()
    volume_scale_adj: Gtk.Adjustment = Gtk.Template.Child()
    subtitles_menu_btn: Gtk.MenuButton = Gtk.Template.Child()
    subtitles_menu: Gio.Menu = Gtk.Template.Child()
    audio_tracks_menu_btn: Gtk.MenuButton = Gtk.Template.Child()
    audio_tracks_menu: Gio.Menu = Gtk.Template.Child()
    video_tracks_menu_btn: Gtk.MenuButton = Gtk.Template.Child()
    video_tracks_menu: Gio.Menu = Gtk.Template.Child()
    chapters_menu_btn: Gtk.MenuButton = Gtk.Template.Child()
    chapters_menu: Gio.Menu = Gtk.Template.Child()
    options_menu_btn: OptionsMenuButton = Gtk.Template.Child()
    shuffle_toggle_btn: Gtk.ToggleButton = Gtk.Template.Child()
    ab_loop_btn: Gtk.ToggleButton = Gtk.Template.Child()
    loop_btn: Gtk.ToggleButton = Gtk.Template.Child()
    fullscreen_btn: Gtk.Button = Gtk.Template.Child()
    time_elapsed_label: Gtk.Label = Gtk.Template.Child()
    progress_box: Gtk.Box = Gtk.Template.Child()
    vid_progress_scale_box: Gtk.Box = Gtk.Template.Child()
    video_progress_scale: Gtk.Scale = Gtk.Template.Child()
    video_progress_adj: Gtk.Adjustment = Gtk.Template.Child()
    time_total_label: Gtk.Label = Gtk.Template.Child()

    def __init__(self, is_activate=False, **kwargs):
        super().__init__(**kwargs)
        trace("window template built")
        self.app: Adw.Application = cast(Adw.Application, kwargs.get("application"))
        self._mpris: MPRIS = self.app.mpris  # type: ignore

        Gtk.WindowGroup().add_window(self)

        self._visible_dialog: Adw.Dialog | None = None
        self.playlist_ls: Gio.ListStore = Gio.ListStore.new(PlaylistItemObj)
        self._playlist_debounce_id: int = 0
        self._playlist_prev_pos: int
        self.prev_shuffle: bool = False
        self.playlist_changed: bool = False
        self.has_some_doc_path: bool = False
        self.can_go_prev: bool = False
        self.can_go_next: bool = False
        self._loop_mode: str = "off"
        self._chapters: list = []
        self._curr_chapter_time = None
        self._actions: dict[str, Gio.SimpleAction] = {}
        self._prev_motion_xy: tuple = (0, 0)
        self._hover_time: float = 0.0
        self._show_remaining: bool = settings.get_boolean("show-remaining")
        self._prev_prog_time: float = -1.0
        self._prev_prog_motion_xy: tuple = (0, 0)
        self._inhibit_cookie: int = 0
        self._video_path: str | None = None
        self._is_startup: bool = True
        self._is_audio: bool = False
        self._space_hold_id: int = 0
        self._space_holding: bool = False
        self._space_pressed: bool = False
        self._left_clk: int = settings.get_int("left-click")
        self._right_clk: int = settings.get_int("right-click")
        self._click_delay_id: int = 0
        _ck_time: int = gtk_setts.props.gtk_double_click_time if gtk_setts else 400
        self._click_time: int = max(_ck_time, min(200, 425))
        self._click_holding: bool = False
        self._prev_speed: float = 1.0
        self._wheel_accum_x: float = 0.0
        self._wheel_accum_y: float = 0.0
        self._hide_icon_indicator: bool = True
        self.skip_pause_obs_count: int = 0
        self._playing_on_press: bool = False
        self.thumb_area: ThumbPreviewGLArea | None = None
        self._thumb_w: int = 1280
        self.is_local_path: bool = True
        self._prog_fine_tune: bool = False
        self._dragging_progress: bool = False
        self._skip_reset_id: int = 0
        self._drag_seek_target: float | None = None
        self._error_count: int = 0
        self._pressed_combos: set[str] = set()
        self._hide_timeout_id: int = 0
        self._is_fullscreen: bool = False
        self._is_inactive: bool = False
        self._loop_seams: list[float] = []  # see _loop_probe
        self._mpv_ctx: mpv.MpvRenderContext
        # The seek-bar tenth last handed to the main loop, and whether that
        # update is still waiting to run. See on_time_change.
        self._sent_prog_tenth: float | None = None
        self._prog_update_queued: bool = False
        # What the thumbnail player needs to open the current file, captured
        # on mpv's event thread once its first frame is up; None when there
        # is nothing for it to do. See _ensure_thumb_preview.
        self._thumb_pending: tuple | None = None
        self._thumb_timeout_id: int = 0
        # Files loaded so far, counted on mpv's event thread, and the count
        # the thumbnail player last caught up with: it shows the playing file
        # only while the two agree. A count rather than a flag, so that a
        # hand-off finishing after the next file has started cannot pass
        # for current.
        self._load_gen: int = 0
        self._thumb_gen: int = -1
        # Where the pointer is on the seek bar, or None while it is off it.
        self._bar_pointer: tuple | None = None
        # Set once the window starts closing; nothing new is started after.
        self._closing: bool = False
        # The last value mpv reported for each of STATE_PROPS. The main thread
        # reads these through prop() instead of asking mpv: a synchronous read
        # waits for mpv's core thread, which can be busy for a long time while
        # a file opens - in one measured startup a single read of
        # playlist-count waited 160 ms, and the first frame waited with it,
        # because mpv needs the main thread to draw that frame.
        self._state: dict[str, object] = {
            "idle-active": True,
            "playlist-count": 0,
            "playlist-pos": -1,
        }

        self.mpv = MPV(
            # terminal=True,
            # log_handler=print,
            loglevel="info",
            audio_client_name=_("NekoPlay"),
            screenshot_directory=SCREENSHOT_DIR,
            screenshot_template="nekoplay_%n",
            config=True,
            config_dir=CONFIG_DIR,
            input_builtin_bindings=False,
            input_vo_keyboard=True,
            load_scripts=True,
            # mpv starts each of its own scripts on a thread with a Lua state
            # of its own, as soon as it is created. These four are never
            # used here - the OSC and the console are switched off below,
            # and select and commands only draw through the console - so
            # they are not started at all: 3 ms off creating the player, and
            # as much again off opening each file. (A user's mpv.conf is read
            # after this, and can still turn them on.)
            osc=False,
            load_console=False,
            load_select=False,
            load_commands=False,
            audio_display="embedded-first",
            audio_file_auto="fuzzy",
            sub_auto="fuzzy",
            sub_file_paths=MPV_PATH_SEP.join(
                (
                    "sub",
                    "subs",
                    "subtitles",
                    "Sub",
                    "Subs",
                    "Subtitles",
                    "srt",
                    "srts",
                    "Srt",
                    "Srts",
                )
            ),
            sub_border_size=2,
            sub_shadow_offset=0.6,
            sub_border_color="#B6000000",
            sub_shadow_color="#97000000",
            sub_color="#ebebeb",
            sub_use_margins=False,
            sub_font="Adwaita Sans SemiBold",
            osd_font="Adwaita Sans",
            osd_bold=True,
            osd_bar=False,
            osd_blur=1,
            osd_border_size=1.5,
            osd_shadow_offset=0.6,
            osd_border_color="#BE000000",
            osd_shadow_color="#1B000000",
            osd_margin_x=66,
            osd_margin_y=66,
            volume_max=150,
            keep_open=True,
            ytdl=True,
            ytdl_raw_options="yes-playlist=",
            cursor_autohide_fs_only=True,
            autocreate_playlist=AUTOCREATE_PLAYLIST,
            # Where the auto playlist is on, 'filter' queues every
            # neighbouring file mpv considers media, images included, so an
            # episode was followed by the screenshots next to it. Emptying
            # the list keeps them out; an image opened deliberately still
            # plays, because this only classifies extensions.
            image_exts="",
            save_watch_history=True,
            watch_history_path=WATCH_HISTORY_JSONL,
        )

        trace("mpv created")

        # Registered before every other observer, so that by the time any of
        # them hands work to the main loop the state it reads is current.
        def remember(name, value):
            self._state[name] = value

        for name in STATE_PROPS:
            self.mpv.observe_property(name, remember)

        self._video_area = VideoGLArea(self.mpv)
        self.offload: Gtk.GraphicsOffload = Gtk.GraphicsOffload(child=self._video_area)
        self.offload.set_black_background(True)
        self.video_overlay.set_child(self.offload)

        self.offload.set_enabled(
            Gtk.GraphicsOffloadEnabled.ENABLED
            if settings.get_boolean("graphics-offload")
            else Gtk.GraphicsOffloadEnabled.DISABLED
        )

        if self.mpv["window-maximized"] or settings.get_boolean("is-maximized"):
            self.maximize()

        self.conf_hwdec = list(filter(lambda x: x != "no", cast(list, self.mpv.hwdec)))
        self.mpv["vo"] = "libmpv"
        self.mpv["osc"] = "no"
        self.mpv["load-console"] = "no"
        self.mpv.command("change-list", "watch-later-options", "remove", "vid")
        self.mpv.command("change-list", "watch-later-options", "remove", "aid")
        self.mpv.command("change-list", "watch-later-options", "remove", "volume")
        self.mpv.command("change-list", "watch-later-options", "remove", "sub-scale")
        self.mpv.command("change-list", "watch-later-options", "remove", "ab-loop-a")
        self.mpv.command("change-list", "watch-later-options", "remove", "ab-loop-b")

        self._setup_actions()
        self._setup_widgets()
        # Every window opens with its controls showing, and hides them a
        # moment into playback. That used to fall out of mpv first reporting
        # itself idle, which no longer happens when the file is handed over
        # before the window is shown (see CineApplication.do_open).
        self.revealer_ui.set_reveal_child(True)
        strip_popover_arrows(self)
        self._setup_observers()
        self.video2x = Video2X(self, settings)

        try:
            self.mpv.command("load-input-conf", f"memory://{INTERNAL_BINDINGS}")
            self.mpv.command("load-input-conf", INPUT_CONF)
        except Exception:
            logger.exception("load-input-conf failed")

        self._input_bindings = cast(dict, self.mpv.input_bindings)
        self._mouse_binds: dict = get_mouse_bindings(self._input_bindings)
        self._nonrepeat_keys, self._has_enter_binding, self._has_kp_enter_binding = (
            parse_bindings(self._input_bindings)
        )

        sync_mpv_with_settings(self)

        if settings.get_boolean("save-session") and is_activate:
            restore_last_playlist(self, self.app, self.mpv)

        # Needs the native window, which only exists once realized.
        self.connect("realize", lambda w: round_window_corners(w))

        trace("window built")
        if TRACE:
            # Realizing the window is where the GDK surface, the D3D device
            # and the GSK renderer are built, and that is the largest single
            # step left in startup, so it gets its own marks.
            self.connect("realize", lambda w: trace("window realized"))
            self.connect("map", lambda w: trace("window mapped"))

    def _setup_actions(self):
        self._create_action("clear-and-add", self._on_clear_and_add)
        self._create_action_stateful("select-subtitle", self._on_subtitle_selected, "i")
        self._create_action_stateful("select-audio", self._on_audio_selected, "i")
        self._create_action_stateful("select-video", self._on_video_selected, "i")
        self._create_action_stateful("select-chapter", self._on_chapter_selected, "i")
        self._create_action("add-sub-tracks", self._on_add_sub_dialog)
        self._create_action("add-audio-tracks", self._on_add_audio_dialog)
        self._create_action("add-playlist-files", self.on_add_playlist_dialog)
        self._create_action("open-folder", self.on_open_folder_dialog)
        self._create_action("open-url", self._on_open_url)
        self._create_action("add-url", self.on_add_url)
        self._create_action("open-history", self._present_history)
        self._create_action("add-playlist-folder", self.on_open_folder_dialog)
        self._create_action("open-playlist-dialog", self._on_open_playlist)
        self._create_action("open-sub-menu", self._on_open_sub_menu)
        self._create_action("open-audio-menu", self._on_open_audio_menu)
        self._create_action("open-chapters-menu", self._on_open_chapters_menu)
        self._create_action("save-session", self._on_save_session)
        self._create_action(
            "save-session-close", lambda *a: self._on_save_session(close=True)
        )

        self._create_action("skip-90", self.on_skip_90_clicked)
        self.app.set_accels_for_action("win.open-folder", ["<primary>i"])
        self.app.set_accels_for_action("win.open-url", ["<primary>u"])
        self.app.set_accels_for_action("win.add-url", ["<shift><primary>u"])
        self.app.set_accels_for_action("win.open-history", ["<primary>h"])
        self.app.set_accels_for_action("win.add-playlist-folder", ["<shift><primary>i"])
        self.app.set_accels_for_action("win.open-playlist-dialog", ["<primary>p"])
        self.app.set_accels_for_action("win.clear-and-add", ["<primary>o"])
        self.app.set_accels_for_action("win.add-playlist-files", ["<shift><primary>o"])
        self.app.set_accels_for_action("win.open-sub-menu", ["<primary>s"])
        self.app.set_accels_for_action("win.open-audio-menu", ["<primary>a"])
        self.app.set_accels_for_action("win.open-chapters-menu", ["<primary>c"])
        self.app.set_accels_for_action("win.save-session", ["<shift><primary>s"])
        self.app.set_accels_for_action("win.save-session-close", ["<shift>q"])
        self.app.set_accels_for_action("win.skip-90", ["<alt>Right"])

        self._create_action("quit", lambda *a: self.close())
        self.app.set_accels_for_action("win.quit", ["q", "<primary>w"])

        self._create_action("custom-shortcuts", self._present_shortcuts)
        self.app.set_accels_for_action("win.custom-shortcuts", ["<primary>question"])
        self.app.set_accels_for_action("app.shortcuts", [])

        self._create_action("play-pause", self._cycle_pause)
        self._create_action("previous", self.on_previous_clicked)
        self._create_action("next", self.on_next_clicked)

    def _present_shortcuts(self, *args):
        builder = Gtk.Builder.new_from_resource(
            "/moe/nyarchlinux/nekoplay/shortcuts-dialog.ui"
        )
        self.shortcuts_dialog = cast(
            Adw.ShortcutsDialog,  # pyright: ignore[reportAttributeAccessIssue]
            builder.get_object("shortcuts_dialog"),
        )
        populate_shortcuts_dialog_mpv(self.shortcuts_dialog, self._input_bindings)
        self.shortcuts_dialog.present(self)

    def _present_history(self, *args):
        history_dialog = HistoryDialog(self)
        history_dialog.present(self)

    def _setup_widgets(self):
        self.set_default_size(DEFAULT_WIDTH, DEFAULT_HEIGHT)

        for widget in [
            self.controls_wrap_box,
            self.volume_box,
            self.volume_scale,
            self.progress_box,
            self.vid_progress_scale_box,
            self.video_progress_scale,
            self.time_elapsed_label,
        ]:
            widget.set_direction(Gtk.TextDirection.LTR)

        max_vol = cast(int, self.mpv.volume_max)
        self._volume_max = float(max_vol)
        self.volume_scale_adj.set_upper(max_vol)

        self.mute_handler_id = self.mute_toggle_btn.connect(
            "toggled",
            lambda btn: self.mpv.command_async("set", "mute", btn.get_active()),
        )

        vol_mid_click = Gtk.GestureClick(button=2)
        vol_mid_click.connect(
            "pressed",
            lambda *a: self.mpv.command_async("cycle", "mute"),
        )
        self.volume_menu_btn.add_controller(vol_mid_click)

        self.fullscreen_btn.connect(
            "clicked",
            lambda *a: self.mpv.command_async("cycle", "fullscreen"),
        )

        vc_adj = self.volume_scale_adj
        self.volume_handler_id = self.volume_scale.connect(
            "value-changed",
            lambda *a: self.mpv.command_async("set", "volume", vc_adj.props.value),
        )

        if max_vol > 100:
            self.volume_scale.add_mark(100.0, Gtk.PositionType.BOTTOM, None)

        self.ab_loop_btn.connect("toggled", self._on_ab_loop_btn_toggled)
        self.loop_btn.connect("toggled", self._on_loop_toggled)

        self.video_progress_adj.connect("value-changed", self._on_progress_adjusted)
        self.popover_content_box = Gtk.Box()
        self.popover_content_box.props.orientation = Gtk.Orientation.VERTICAL

        self.time_tooltip_label = Gtk.Label()
        self.time_tooltip_label.set_use_markup(True)
        self.time_tooltip_label.set_justify(Gtk.Justification.CENTER)
        self.time_tooltip_label.add_css_class("numeric")

        def create_layer_and_revealer(child, margin_bottom):
            layer = Gtk.Fixed(
                valign=Gtk.Align.END,
                margin_bottom=margin_bottom,
                can_focus=False,
                can_target=False,
            )
            revealer = Gtk.Revealer(
                css_name="time-tooltip",
                transition_duration=175,
                transition_type=Gtk.RevealerTransitionType.CROSSFADE,
            )
            revealer.set_child(child)
            layer.put(revealer, 0, 0)
            self.video_overlay.add_overlay(layer)
            return layer, revealer

        self.tooltip_label_layer, self.tooltip_label_revealer = (
            create_layer_and_revealer(self.time_tooltip_label, 38)
        )

        self.thumb_frame = Gtk.Frame()
        self.tooltip_thumb_layer, self.tooltip_thumb_revealer = (
            create_layer_and_revealer(self.thumb_frame, 72)
        )

        self._set_time_margin()
        self._set_time_tooltip()

        key_controller = Gtk.EventControllerKey()
        key_controller.connect("key-pressed", self._on_key_event, "keypress")
        key_controller.connect("key-released", self._on_key_event, "keyup")
        key_controller.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        self.add_controller(key_controller)

        progress_hover = Gtk.EventControllerMotion()
        progress_hover.connect("enter", self._on_progress_enter)
        progress_hover.connect("motion", self._on_progress_motion)
        progress_hover.connect("leave", self._on_progress_leave)
        self.video_progress_scale.add_controller(progress_hover)

        prog_mid_click = Gtk.GestureClick(button=2)
        prog_mid_click.connect("pressed", self._go_to_chapter_start)
        self.video_progress_scale.add_controller(prog_mid_click)

        for c in self.video_progress_scale.observe_controllers():
            if isinstance(c, Gtk.GestureDrag):
                c.connect("drag-begin", self._on_progress_pressed)
                c.connect("drag-end", self._on_progress_released)
            if isinstance(c, Gtk.GestureLongPress):
                c.connect("pressed", lambda *a: setattr(self, "_prog_fine_tune", True))
                c.connect("end", lambda *a: setattr(self, "_prog_fine_tune", False))

        ecs_flags = Gtk.EventControllerScrollFlags

        progress_ecs = Gtk.EventControllerScroll.new(ecs_flags.VERTICAL)
        progress_ecs.connect("scroll", self._on_progress_scroll)
        self.video_progress_scale.add_controller(progress_ecs)

        overlay_ecs = Gtk.EventControllerScroll.new(ecs_flags.BOTH_AXES)
        volume_ecs = Gtk.EventControllerScroll.new(ecs_flags.VERTICAL)
        self.video_overlay.add_controller(overlay_ecs)
        overlay_ecs.connect("scroll", self._on_mouse_scroll)
        self.volume_scale.add_controller(volume_ecs)
        volume_ecs.connect("scroll", self._on_mouse_scroll_volume)

        self.clk_rect = Gdk.Rectangle()
        for btn_num, MBTN in MBTN_MAP.items():
            click_gesture = Gtk.GestureClick(button=btn_num)
            click_gesture.connect("pressed", self._on_click_pressed, MBTN)
            click_gesture.connect("released", self._on_click_released, MBTN)
            self.video_overlay.add_controller(click_gesture)

        long_press = Gtk.GestureLongPress.new()
        long_press.connect("pressed", self._on_click_hold)
        long_press.connect("end", self._cancel_click_hold)
        long_press.connect("cancelled", self._cancel_click_hold)
        self.window_handle.add_controller(long_press)

        @self._connect("notify::visible-dialog")
        def on_vis_dialog_change(*args):
            if dialog := self.get_visible_dialog():
                self._visible_dialog = dialog
                self.set_cursor_from_name(None)
                self._cancel_click_hold()
                self._space_holding = False
                self._set_space_holding(False)
            else:
                self._visible_dialog = None
            self.hide_ui_timeout()

        @self._connect("notify::is-active")
        def on_is_active_change(*args):
            if self.props.is_active:
                timeout_add_once(200, setattr, self, "_is_inactive", False)
            else:
                self._cancel_click_hold()
                self._space_holding = False
                self._set_space_holding(False)
                self._is_inactive = True

        drop_target = Gtk.DropTarget.new(Gdk.FileList, Gdk.DragAction.COPY)
        drop_target.set_gtypes([Gdk.FileList, GObject.TYPE_STRING])
        drop_target.connect("enter", self._on_drop_enter)
        drop_target.connect("leave", self._on_drop_leave)
        drop_target.connect("drop", self._on_drop)
        self.video_overlay.add_controller(drop_target)

        self.motion_header_controls = Gtk.EventControllerMotion()
        self.motion_header_controls.connect("motion", self._on_mouse_motion)
        self.revealer_ui.add_controller(self.motion_header_controls)

        self.motion_header = Gtk.EventControllerMotion()
        self.motion_controls = Gtk.EventControllerMotion()
        self.headerbar.add_controller(self.motion_header)
        self.controls_box.add_controller(self.motion_controls)

        self.motion_controls_separator = Gtk.EventControllerMotion()
        self.controls_separator.add_controller(self.motion_controls_separator)

        @self._connect("notify::maximized")
        def on_maximized_change(*args):
            settings.set_boolean("is-maximized", self.is_maximized())

        self.connect("notify::fullscreened", self._set_fs_state)

        buttons = [
            self.primary_menu_btn,
            self.open_menu_btn,
            self.options_menu_btn,
            self.volume_menu_btn,
            self.subtitles_menu_btn,
            self.audio_tracks_menu_btn,
            self.video_tracks_menu_btn,
            self.chapters_menu_btn,
        ]
        for btn in buttons:
            popover = btn.props.popover
            popover.connect("closed", self.hide_ui_timeout)

            if btn == self.open_menu_btn:

                def on_popv_closed(*args):
                    if is_same_playlist(self.mpv.playlist):
                        self.mpv.write_watch_later_config()

                popover.connect("closed", on_popv_closed)

        # TODO: remove for gnome 51
        # Somehow because the options menu contains other menus popovers inside,
        # when closing it, contains_pointer from header/controls still returns True,
        # even if not hovering; setting Gtk.PropagationLimit.NONE seems to be the only way to fix it
        # Also affects click behaviour, when closing without the workaround, click gesture wont work
        # and will activate the window_handle clicks instead, until controls are hovered again

        def set_p_limit_workaround(btn, _gparam):
            limit = (
                Gtk.PropagationLimit.NONE
                if btn.props.active
                else Gtk.PropagationLimit.SAME_NATIVE
            )
            self.motion_controls.set_propagation_limit(limit)

        self.options_menu_btn.connect("notify::active", set_p_limit_workaround)

    def _set_fs_state(self, _window, _gparam):
        is_fullscreen = self.props.fullscreened

        try:
            if not is_fullscreen:
                self.mpv.command_async("set", "fullscreen", "no")
        except mpv.ShutdownError:
            pass

        if not gtk_setts:
            return

        layout = gtk_setts.get_property("gtk-decoration-layout")

        if is_fullscreen:
            left_side, _colon, _right_side = layout.partition(":")
            layout = "close:" if "close" in left_side else ":close"

        self.headerbar.set_decoration_layout(layout)

    def _show_ui(self):
        self.set_cursor_from_name(None)
        self.revealer_ui.set_reveal_child(True)

    def hide_ui_timeout(self, *args, s=2):
        if self._hide_timeout_id:
            GLib.source_remove(self._hide_timeout_id)
        self._hide_timeout_id = timeout_add_seconds_once(s, self._hide_ui)

    def _hide_ui(self, *args):
        try:
            self._hide_timeout_id = 0
            controls_hover = self.motion_controls.props.contains_pointer
            header_hover = self.motion_header.props.contains_pointer

            active_or_hover = (
                self.prop("idle-active")
                or header_hover
                or controls_hover
                or self.primary_menu_btn.props.active
                or self.open_menu_btn.props.active
                or self.options_menu_btn.props.active
                or self.volume_menu_btn.props.active
                or self.subtitles_menu_btn.props.active
                or self.audio_tracks_menu_btn.props.active
                or self.video_tracks_menu_btn.props.active
                or self.chapters_menu_btn.props.active
            )
            if not active_or_hover:
                self.revealer_ui.set_reveal_child(False)
                self._hide_time_tooltip()

            if (
                (self._is_fullscreen or not self.prop("cursor-autohide-fs-only"))
                and not active_or_hover
                and not self.props.dialogs
            ):
                self.set_cursor_from_name("none")
        except mpv.ShutdownError:
            return

    def _on_mouse_motion(self, controller: Gtk.EventControllerMotion, x, y):
        if None not in (x, y):
            if (x, y) == self._prev_motion_xy or self._click_holding:
                return

            self._prev_motion_xy = (x, y)
            self._show_ui()
            self.hide_ui_timeout()

            if event := controller.get_current_event():
                state = event.get_modifier_state()
                if state & Gdk.ModifierType.CONTROL_MASK:
                    mpv_x = int(x * self.props.scale_factor)
                    mpv_y = int(y * self.props.scale_factor)
                    self.mpv.command_async("mouse", mpv_x, mpv_y)

    def _update_track_menus(self, track_list):
        self.subtitles_menu.remove_all()
        self.subtitles_menu.append(_("Add Subtitle Track"), "win.add-sub-tracks")

        item_none_sub = Gio.MenuItem.new(_("None"), None)
        item_none_sub.set_action_and_target_value(
            "win.select-subtitle", GLib.Variant("i", 0)
        )
        self.subtitles_menu.append_item(item_none_sub)

        self.audio_tracks_menu.remove_all()
        self.audio_tracks_menu.append(_("Add Audio Track"), "win.add-audio-tracks")

        item_none_audio = Gio.MenuItem.new(_("None"), None)
        item_none_audio.set_action_and_target_value(
            "win.select-audio", GLib.Variant("i", 0)
        )
        self.audio_tracks_menu.append_item(item_none_audio)

        self.video_tracks_menu.remove_all()

        video_count = 0
        for track in track_list:
            self._add_track_to_menu(track)
            track_type = track.get("type")
            if track_type == "video" and not track.get("albumart"):
                video_count += 1

        self._is_audio = video_count == 0
        self.video_tracks_menu_btn.set_visible(video_count > 1)

        def hide_box_first_model_btn(menu_btn):
            """Hide the space before add track label"""
            target = menu_btn.get_popover()
            for _i in range(8):
                if target:
                    target = target.get_first_child()
            if target:
                target.set_visible(False)

        hide_box_first_model_btn(self.subtitles_menu_btn)
        hide_box_first_model_btn(self.audio_tracks_menu_btn)

    def _add_track_to_menu(self, track):
        track_id = int(track.get("id", 0))
        track_type = track.get("type")
        lang = track.get("lang")
        title = track.get("title")

        label_parts = [p for p in (title, lang) if p]
        label = (
            " – ".join(label_parts) if label_parts else (_("Track") + f" {track_id}")
        )

        if track_type == "sub":
            menu = self.subtitles_menu
            action = "win.select-subtitle"
        elif track_type == "audio":
            menu = self.audio_tracks_menu
            action = "win.select-audio"
        else:
            menu = self.video_tracks_menu
            action = "win.select-video"

        item = Gio.MenuItem.new(label, None)
        item.set_action_and_target_value(action, GLib.Variant("i", track_id))
        menu.append_item(item)

    def _create_action(self, name, callback):
        action = Gio.SimpleAction.new(name, None)
        action.connect("activate", callback)
        self.add_action(action)
        self._actions[name] = action

    def _create_action_stateful(self, name, callback, target_type):
        if target_type != "i":
            raise TypeError("_create_action_stateful int only")
        action = Gio.SimpleAction.new_stateful(
            name,
            GLib.VariantType.new(target_type),
            GLib.Variant("i", 0),
        )
        action.connect("activate", callback)
        self.add_action(action)
        self._actions[name] = action

    def _on_open_playlist(self, *args):
        if self.prop("idle-active"):
            return
        playlist = Playlist(self)
        strip_popover_arrows(playlist)
        playlist.present(self)

    def on_open_folder_dialog(self, action, *args):
        add_mode = action.props.name != "open-folder"
        title = _("Add Folder") if add_mode else _("Open Folder")
        dialog = Gtk.FileDialog(title=title)
        curr_path = self._video_path

        if isinstance(curr_path, str) and os.path.exists(curr_path):
            folder_path = os.path.dirname(curr_path)
            dialog.set_initial_folder(Gio.File.new_for_path(folder_path))

        def on_open_response(dialog, result):
            try:
                folder = dialog.select_folder_finish(result)

                if not add_mode:
                    self.mpv.stop()
                    self.mpv.pause = False
                    self.shuffle_toggle_btn.set_active(False)

                path = folder.get_path()
                self.mpv.loadfile(path, "append-play")

            except GLib.Error as e:
                logger.warning(f"Dialog error: {e}")

        dialog.select_folder(self, None, on_open_response)
        return Gdk.EVENT_STOP  # so "<shift><primary>i" doesn't trigger inspector

    def _on_clear_and_add(self, _action, _param):
        self._open_add_dialog(_("Open Files"), "clear-and-add")

    def on_add_playlist_dialog(self, _action, _param):
        self._open_add_dialog(_("Add Files"), "playlist-add")
        return Gdk.EVENT_STOP

    def _on_add_sub_dialog(self, _action, _param):
        self._open_add_dialog(_("Add Subtitle"), "sub-add")

    def _on_add_audio_dialog(self, _action, _param):
        self._open_add_dialog(_("Add Audio"), "audio-add")

    def _open_add_dialog(self, title, mode):
        filter = Gtk.FileFilter()
        dialog = Gtk.FileDialog(title=title)
        filters_list = Gio.ListStore.new(Gtk.FileFilter)
        filters_list.append(filter)
        dialog.set_filters(filters_list)
        dialog.set_default_filter(filter)

        curr_path = self._video_path
        if isinstance(curr_path, str) and os.path.exists(curr_path):
            folder_path = os.path.dirname(curr_path)
            dialog.set_initial_folder(Gio.File.new_for_path(folder_path))

        if mode == "sub-add":
            filter.set_name(_("Subtitles"))
            for sub in SUB_EXTS:
                s = sub.lstrip(".")
                filter.add_suffix(s)
        elif mode == "audio-add":
            filter.set_name(_("Audio"))
            for m in ["video/*", "audio/*"]:
                filter.add_mime_type(m)
        else:
            filter.set_name(_("Media"))
            for m in ["video/*", "audio/*", "image/*"]:
                filter.add_mime_type(m)

        dialog.open_multiple(
            self,
            None,
            lambda d, res: self._on_open_response(d, res, mode),
        )

        if isinstance(self._visible_dialog, Playlist):
            self._visible_dialog.spinner.set_visible(True)

    def _on_open_response(self, dialog, result, mode):
        try:
            files = dialog.open_multiple_finish(result)

            if mode == "clear-and-add":
                self.mpv.stop()
                self.shuffle_toggle_btn.set_active(False)

            for file in files:
                path = file.get_path() or file.get_uri()

                if mode == "sub-add":
                    self.mpv.sub_add(path)
                elif mode == "audio-add":
                    self.mpv.audio_add(path)
                else:
                    self.mpv.loadfile(path, "append-play")

            if mode == "clear-and-add":
                self.mpv.pause = False
        except GLib.Error as e:
            logger.warning(f"Dialog error: {e}")
        except Exception:
            logger.exception("Failed to add files")
        finally:
            if isinstance(self._visible_dialog, Playlist):
                self._visible_dialog.spinner.set_visible(False)

    def _on_open_sub_menu(self, *args):
        self._show_ui()
        self.subtitles_menu_btn.popup()

    def _on_open_audio_menu(self, *args):
        self._show_ui()
        self.audio_tracks_menu_btn.popup()

    def _on_open_chapters_menu(self, *args):
        if not self._chapters:
            return
        self._show_ui()
        self.chapters_menu_btn.popup()

    def _on_save_session(self, *args, close=False):
        try:
            settings.set_boolean("save-session", True)
            save_last_playlist_file(self.mpv)
            if close:
                self.close()
                return
            self.mpv.write_watch_later_config()
            idle_add_once(self.show_toast, _("Session Saved"))
        except Exception:
            logger.exception("_on_save_session failed")

    def _on_open_url(self, *args, add=False):
        mode = "append-play" if add else "replace"
        view = Adw.ToolbarView()
        header_bar = Adw.HeaderBar()
        h_title = _("Add URL") if add else _("Open URL")
        header_bar.set_title_widget(Adw.WindowTitle(title=h_title))
        view.add_top_bar(header_bar)

        content_box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=18,
            margin_bottom=18,
            margin_start=18,
            margin_end=18,
        )

        view.set_content(content_box)
        entry_row = Adw.EntryRow(title=_("URL"), activates_default=True)
        list_box = Gtk.ListBox(
            selection_mode=Gtk.SelectionMode.NONE, css_classes=["boxed-list"]
        )
        list_box.append(entry_row)
        content_box.append(list_box)

        btn_open = Gtk.Button(
            label=_("Add") if add else _("Open"),
            css_classes=["pill", "suggested-action"],
            halign=Gtk.Align.CENTER,
            sensitive=False,
        )

        content_box.append(btn_open)
        dialog = Adw.Dialog(content_width=450, child=view, default_widget=btn_open)
        self.url = ""
        protocol_list = cast(list, self.mpv.protocol_list)

        def is_valid_input(text):
            url = text.strip()
            parsed = urlparse(url)
            path_exists = os.path.exists(url)
            if parsed.scheme in protocol_list or path_exists:
                self.url = url
                return True
            elif url:
                self.url = f"https://{url}"
                return True
            return False

        def on_text_changed(*args):
            is_valid = is_valid_input(entry_row.get_text())
            btn_open.set_sensitive(is_valid)

        entry_row.connect("notify::text", on_text_changed)

        def open_url(*args):
            dialog.close()
            try:
                self.mpv.loadfile(self.url, mode)
                if mode == "replace":
                    self.mpv.pause = False
                    self.shuffle_toggle_btn.set_active(False)
            except mpv.ShutdownError:
                pass

        def on_clipboard_read(clipboard: Gdk.Clipboard, result):
            if not (text := clipboard.read_text_finish(result)):
                return

            if urlparse(text).scheme in protocol_list:
                entry_row.insert_text(text, 0)

        if display and (clipboard := display.get_clipboard()):
            clipboard.read_text_async(None, on_clipboard_read)

        btn_open.connect("clicked", open_url)
        dialog.present(self)

    def on_add_url(self, *args):
        self._on_open_url(add=True)
        return Gdk.EVENT_STOP

    def _wants_thumb_preview(self) -> bool:
        return settings.get_boolean("thumbnail-preview") and self.is_local_path

    def _thumb_source(self) -> tuple | None:
        """What the thumbnail player needs from this one: the file, the
        decoder setting, and the video's size if it has one.

        Every item is a read that waits on mpv, so outside of the preferences
        switch this runs on mpv's event thread, after the first frame.
        """
        try:
            has_video = bool(self.mpv.vid)
            return (
                self.mpv.path,
                self.mpv.hwdec,
                self.mpv.width if has_video else None,
                self.mpv.height if has_video else None,
            )
        except mpv.ShutdownError:
            return None
        except Exception:
            logger.exception("Could not read what the thumbnail player needs")
            return None

    def _schedule_thumb_preview(self):
        """Open the thumbnail player a moment after the first frame, once
        playback has settled - or sooner, if the pointer reaches the bar."""
        if self._closing:
            return
        if self._thumb_timeout_id:
            GLib.source_remove(self._thumb_timeout_id)
            self._thumb_timeout_id = 0
        if self._thumb_pending is None:
            return
        if not self._wants_thumb_preview():
            self._thumb_pending = None
            return

        def on_timeout():
            self._thumb_timeout_id = 0
            self._ensure_thumb_preview()
            return GLib.SOURCE_REMOVE

        # Default priority, not idle: a renderer that keeps the main loop
        # busy every frame would starve an idle source indefinitely.
        self._thumb_timeout_id = GLib.timeout_add(THUMB_PREVIEW_DELAY_MS, on_timeout)

    def _thumb_current(self) -> bool:
        """Whether the thumbnail player holds the file that is playing."""
        return self._thumb_gen == self._load_gen

    def _ensure_thumb_preview(self) -> bool:
        """Give the thumbnail player the current file, if it is still
        waiting for it. True if it was given."""
        pending, self._thumb_pending = self._thumb_pending, None
        if self._thumb_timeout_id:
            GLib.source_remove(self._thumb_timeout_id)
            self._thumb_timeout_id = 0
        if pending is None or self._closing or not self._wants_thumb_preview():
            return False
        gen, source = pending
        if gen != self._load_gen:
            return False  # for a file that has been replaced since
        self.setup_thumb_preview(source, gen)
        return True

    def setup_thumb_preview(self, source=None, gen=None):
        """Size the seek-bar thumbnail and open the current file in it.

        *source* is what _thumb_source() captured, and *gen* the load it
        belongs to; without them (the setting was just switched on) they are
        read here.
        """
        if self._closing:
            return
        if source is None:
            source = self._thumb_source()
            if source is None:
                return
        if gen is None:
            gen = self._load_gen
        path, hwdec, width, height = source

        if not self.thumb_area:
            self.thumb_area = ThumbPreviewGLArea(hwdec)
            self.thumb_frame.set_child(self.thumb_area)
            self.thumb_area.realize()

        v_width, v_height = 1280, 720
        if width and height:
            v_width, v_height = int(width), int(height)

        if v_width >= v_height:  # Horizontal or square
            width = 200
            height = int((v_height / v_width) * width)
            if height == width:
                width, height = 168, 168
        else:  # Vertical
            height = 168
            width = int((v_width / v_height) * height)

        self._thumb_w = width
        self.thumb_area.set_size_request(width, height)
        a = self.thumb_area
        if self._is_audio:
            a.stop()
            self._thumb_gen = gen
        # Opened where the pointer rests, if it does: a seek sent to a file
        # that is still opening would be refused.
        elif a.load_file(path, self._resting_hover_time()):
            self._thumb_gen = gen

        # A pointer resting on the bar gets the thumbnail where it is.
        if self._bar_pointer is not None:
            x, y = self._bar_pointer
            self._set_time_tooltip()
            self._on_progress_motion(None, x, y, seek_thumb=False)

    def _resting_hover_time(self) -> float | None:
        """The time under a pointer resting on the seek bar, if one is."""
        if self._bar_pointer is None:
            return None
        width = self.video_progress_scale.get_width()
        duration = float(self.prop("duration", 0))
        if width <= 0 or duration <= 0:
            return None
        return max(0.0, min(1.0, self._bar_pointer[0] / width)) * duration

    def _on_progress_enter(self, _controller=None, x=None, y=None):
        if x is not None:
            self._bar_pointer = (x, y)
        self._set_time_tooltip()
        self._ensure_thumb_preview()

    def _on_progress_leave(self, *args):
        self._bar_pointer = None
        self._hide_time_tooltip()

    def _hide_time_tooltip(self, *args):
        self.prev_reveal = False
        self.tooltip_thumb_revealer.set_reveal_child(False)
        self.tooltip_label_revealer.set_reveal_child(False)

    def _set_time_tooltip(self, *args):
        self._prev_prog_motion_xy = (-1, -1)  # triggers _on_progress_motion
        self.width = self.get_width()
        self.prog_width = self.video_progress_scale.get_width()
        self.duration = float(self.prop("duration", 0))
        self.prev_reveal = False

    def _move_time_tooltip(self, revealer, layer: Gtk.Fixed, x, tooltip_w):
        x_pos = max(0, min(x - (tooltip_w / 2) + 23, self.width - tooltip_w))
        layer.move(revealer, x_pos, 0)

    def _on_progress_motion(self, _controller, x, y, seek_thumb=True):
        if (x, y) == self._prev_prog_motion_xy:
            return
        self._prev_prog_motion_xy = (x, y)
        self._bar_pointer = (x, y)

        if self._prog_fine_tune:
            self._hide_time_tooltip()
            return

        # The pointer may have been on the bar since before this file
        # started, with no enter event to hand the thumbnail player its file;
        # handing it over redraws the tooltip here.
        if self._thumb_pending is not None and self._ensure_thumb_preview():
            return
        current = self._thumb_current()
        show_thumb = self.thumb_area is not None and not self._is_audio and current

        if not self.prev_reveal:
            self.tooltip_thumb_revealer.set_reveal_child(show_thumb)
            self.tooltip_label_revealer.set_reveal_child(True)
            self.prev_reveal = True
        elif self.tooltip_thumb_revealer.get_reveal_child() != show_thumb:
            self.tooltip_thumb_revealer.set_reveal_child(show_thumb)

        if self.prog_width <= 0:
            return

        percentage = max(0, min(1, x / self.prog_width))
        self._hover_time = percentage * self.duration

        title = None
        if self._chapters:
            idx = bisect.bisect_right(self.chapter_times, self._hover_time) - 1
            if idx >= 0:
                self._curr_chapter_time = self.chapter_times[idx]
                title = self.chapter_titles[idx]
        else:
            self._curr_chapter_time = None

        time_str = format_time(self._hover_time)
        text = f"{time_str} ‐ <b>{title}</b>" if title else time_str
        self.time_tooltip_label.set_markup(text)
        label_w = self.tooltip_label_revealer.get_preferred_size()[1].width

        self._move_time_tooltip(
            self.tooltip_label_revealer, self.tooltip_label_layer, x, label_w
        )

        if self.thumb_area is None:
            return

        self._move_time_tooltip(
            self.tooltip_thumb_revealer, self.tooltip_thumb_layer, x, self._thumb_w + 12
        )

        if seek_thumb and current:
            idle_add_once(self.thumb_area.seek, self._hover_time)

    def _go_to_chapter_start(self, *args):
        if self._curr_chapter_time is not None:
            self.mpv.command_async("seek", self._curr_chapter_time, "absolute")

    def _on_progress_scroll(self, controller, _dx, dy):
        event: Gdk.ScrollEvent = controller.get_current_event()
        state = event.get_modifier_state()

        if state & Gdk.ModifierType.CONTROL_MASK:
            return True

        direction: Gdk.ScrollDirection = event.get_direction()
        rel_dir: Gdk.ScrollRelativeDirection = event.get_relative_direction()  # type: ignore
        is_natural: bool = rel_dir == Gdk.ScrollRelativeDirection.INVERTED  # type: ignore
        step = dy if direction == Gdk.ScrollDirection.SMOOTH else dy * 10

        if is_natural:
            step = -step

        adj = self.video_progress_scale.get_adjustment()
        progress = adj.get_value()
        new_progress = progress - step
        adj.set_value(new_progress)

        return True

    def _update_volume_icon(self):
        volume = cast(float, self.prop("volume", 0))
        is_muted = self.prop("mute")

        if is_muted or volume == 0:
            icon = "cine-volume-mute-symbolic"
        elif volume < 33:
            icon = "cine-volume-low-symbolic"
        elif volume < 66:
            icon = "cine-volume-mid-symbolic"
        elif volume <= 100.5:
            icon = "cine-volume-max-symbolic"
        else:
            icon = "cine-volume-overamp-symbolic"

        self.volume_menu_btn.props.icon_name = icon

    def _set_time_margin(self):
        self.time_elapsed_label.props.margin_end = 3 if self._show_remaining else 0

    @Gtk.Template.Callback()
    def _toggle_elapsed_remaining(self, _btn):
        self._show_remaining = not self._show_remaining
        settings.set_boolean("show-remaining", self._show_remaining)
        pos = float(self.prop("time-pos", 0))
        self._update_progress(pos, update_bar=False)
        self._set_time_margin()

    def _flush_progress(self):
        self._prog_update_queued = False
        if self._dragging_progress:
            return  # the knob drives the clock until it is let go
        self._update_progress(float(self.prop("time-pos", 0)))

    def _update_progress(self, curr_time, update_bar=True):
        curr_time = round(curr_time, 1)
        # The slider is under the pointer while it is dragged; the position a
        # keyframe seek lands on would only drag it back.
        update_bar = update_bar and not self._dragging_progress

        if update_bar and curr_time == self._prev_prog_time:
            return

        if update_bar:
            self.video_progress_adj.handler_block_by_func(self._on_progress_adjusted)
            self.video_progress_adj.props.value = curr_time
            self.video_progress_adj.handler_unblock_by_func(self._on_progress_adjusted)

        try:
            if self._show_remaining:
                duration = float(self.prop("duration", 0))
                remaining = (duration - curr_time) if duration > curr_time else 0
                self.time_elapsed_label.props.label = f"-{format_time(remaining)}"
            else:
                self.time_elapsed_label.props.label = format_time(curr_time)
        except mpv.ShutdownError:
            pass

        self._prev_prog_time = curr_time

    def _update_chapter_marks_and_menu(self, chapters):
        if not chapters:
            self.video_progress_scale.clear_marks()
            self.chapters_menu_btn.set_visible(False)
            return

        self._chapters = sorted(chapters, key=lambda c: c.get("time", 0))
        self.chapter_times, self.chapter_titles = [], []

        self.chapters_menu_btn.set_visible(True)
        self.chapters_menu.remove_all()

        for i, chapter in enumerate(self._chapters):
            title = chapter.get("title") or _("Chapter") + f" {i + 1}"
            item = Gio.MenuItem.new(title, None)
            item.set_action_and_target_value("win.select-chapter", GLib.Variant("i", i))
            self.chapters_menu.append_item(item)

            time_pos = chapter.get("time")
            if time_pos is not None:
                self.video_progress_scale.add_mark(time_pos, Gtk.PositionType.TOP, None)

            self.chapter_times.append(chapter.get("time"))
            self.chapter_titles.append(GLib.markup_escape_text(title))

    def _navigate_playlist(self, direction: int):
        pos = max(0, int(self.prop("playlist-pos", 0)))
        count = int(self.prop("playlist-count", 0))

        if count > 0:
            target = (pos + direction) % count
            self.mpv.command_async("set", "playlist-pos", target)
            # So a second press before mpv reports back moves on from here.
            self._state["playlist-pos"] = target

    @Gtk.Template.Callback()
    def on_previous_clicked(self, *args):
        self._navigate_playlist(-1)

    @Gtk.Template.Callback()
    def on_next_clicked(self, *args):
        self._navigate_playlist(+1)

    @Gtk.Template.Callback()
    def on_skip_90_clicked(self, *args):
        self.mpv.command_async("seek", 90, "relative+keyframes")

    def _on_subtitle_selected(self, action, parameter):
        self.mpv.command_async("set", "sub-visibility", "yes")
        track_id = parameter.get_int32()
        self.mpv.command_async("set", "sid", track_id if track_id > 0 else "no")
        action.set_state(parameter)

    def _on_audio_selected(self, action, parameter):
        track_id = parameter.get_int32()
        self.mpv.command_async("set", "aid", track_id)
        action.set_state(parameter)

    def _on_video_selected(self, action, parameter):
        track_id = parameter.get_int32()
        self.mpv.command_async("set", "vid", track_id)
        action.set_state(parameter)

    def _on_chapter_selected(self, action, parameter):
        chapter_index = parameter.get_int32()
        self.mpv.command_async("set", "chapter", chapter_index)
        action.set_state(parameter)

    @Gtk.Template.Callback()
    def _sync_chapter_menu_selected(self, *args):
        if action := self.lookup_action("select-chapter"):
            action.set_state(  # pyright: ignore[reportAttributeAccessIssue]
                GLib.Variant("i", self.prop("chapter", 0))
            )

    def _update_play_pause_icon(self, paused):
        play = "cine-playback-start-symbolic"
        pause = "cine-playback-pause-symbolic"

        btn_icon = play if paused else pause
        self.play_pause_btn.set_icon_name(btn_icon)

        text = _("Play") if paused else _("Pause")
        self.play_pause_btn.update_property([Gtk.AccessibleProperty.LABEL], [text])

        self.icon_indicator.props.icon_name = pause if paused else play
        self._show_icon_indicator()
        self._mpris.update_playback_status(paused)

    # -- loop seam probe (NEKOPLAY_TRACE only) ---------------------------------
    #
    # How much longer than the clip does one loop of it take? An earlier
    # attempt read mpv properties synchronously from the main loop every
    # 2 ms and measured its own interference (+187 ms). This one only
    # listens: time-pos arrives on mpv's event thread, a wrap is the position
    # falling by more than half the clip, and the seam is the wall time
    # between wraps minus the clip length, printed as each one completes.

    _loop_prev_pos: float = 0.0
    _loop_prev_wrap: float = 0.0
    _loop_duration: float = 0.0

    def _loop_probe(self, pos: float) -> None:
        import time

        duration = self._loop_duration
        if duration <= 0:
            return
        if self._loop_prev_pos - pos > duration * 0.5:
            now = time.monotonic()
            if self._loop_prev_wrap:
                seam = (now - self._loop_prev_wrap - duration) * 1000
                self._loop_seams.append(seam)
                seams = sorted(self._loop_seams)
                trace(
                    "loop wrap %d: seam %+.1f ms (median %+.1f ms over %d loops, clip %.3f s)"
                    % (len(seams), seam, seams[len(seams) // 2], len(seams), duration)
                )
            self._loop_prev_wrap = now
        self._loop_prev_pos = pos

    def _update_duration(self, duration):
        self.time_total_label.set_text(format_time(duration))

        if duration == 0:
            self.video_progress_scale.set_can_target(False)
            self.video_progress_scale.set_can_focus(False)
            return

        self.video_progress_scale.set_can_target(True)
        self.video_progress_scale.set_can_focus(True)

        self.video_progress_adj.set_upper(duration)

        if duration >= 86400:
            chars = 10
        elif duration >= 3600:
            chars = 7
        elif duration >= 600:
            chars = 6
        else:
            chars = 5

        self.time_elapsed_label.set_width_chars(chars)

    @Gtk.Template.Callback()
    def _cycle_pause(self, *args):
        # Asked for by the user, like every other input that resets this:
        # its change must show.
        self.skip_pause_obs_count = 0
        self.mpv.command_async("cycle", "pause")

    def _on_progress_pressed(self, gesture=None, *args):
        # GtkRange's drag gesture answers every button, but only the primary
        # one moves the knob; a middle click is the jump to a chapter start.
        # (Touch reports no button at all.)
        if gesture is not None and gesture.get_current_button() not in (
            0,
            Gdk.BUTTON_PRIMARY,
        ):
            return
        self._dragging_progress = True
        if self._skip_reset_id:
            GLib.source_remove(self._skip_reset_id)
            self._skip_reset_id = 0
        try:
            self._playing_on_press = not self.prop("pause")
            if self._playing_on_press:
                self.skip_pause_obs_count += 1
                self.mpv.command_async("set", "pause", "yes")
        except Exception:
            logger.exception("_on_progress_pressed failed")
            self.skip_pause_obs_count = 0

    def _on_progress_released(self, *args):
        if not self._dragging_progress:
            return  # a press that was not a drag of the knob
        self._dragging_progress = False
        target, self._drag_seek_target = self._drag_seek_target, None
        if target is not None:
            self.mpv.command_async("seek", target, "absolute")
        try:
            if self._playing_on_press:
                self.skip_pause_obs_count += 1
                self._playing_on_press = False
                self.mpv.command_async("set", "pause", "no")

                # mpv can report a pause and a resume this close together
                # as one change, or none, and a count left over would hide
                # the next real one - mpv pausing at the end of the file.
                def reset():
                    self._skip_reset_id = 0
                    self.skip_pause_obs_count = 0

                self._skip_reset_id = timeout_add_once(1000, reset)
        except Exception:
            logger.exception("_on_progress_released failed")
            self.skip_pause_obs_count = 0

    def _on_progress_adjusted(self, adjustment):
        value = adjustment.props.value
        # GtkRange's fine-tune mode (a long press, or Shift+click, on the
        # knob) is for picking an exact moment, so it always seeks exactly.
        fine_tune = self._prog_fine_tune or self.video_progress_scale.has_css_class(
            "fine-tune"
        )
        if self._dragging_progress and not fine_tune:
            # Keyframes only while the slider moves: each lands at once,
            # where an exact seek would decode its way to a frame the next
            # motion has already made stale. Release seeks exactly.
            self._drag_seek_target = value
            self.mpv.command_async("seek", value, "absolute+keyframes")
        else:
            # Exact already, so there is nothing left for a release to fix.
            self._drag_seek_target = None
            self.mpv.command_async("seek", value, "absolute")
        # The clock shows where the knob is: while dragging, not the keyframe
        # the video stopped on, and for a click, the time it seeked to, which
        # a paused video would otherwise never report back.
        self._update_progress(value, update_bar=False)

    @Gtk.Template.Callback()
    def _on_shuffle_toggled(self, button):
        active = button.props.active

        cmd = "playlist-shuffle" if active else "playlist-unshuffle"
        self.mpv.command(cmd)

        self._mpris.update_shuffle(active)
        self.prev_shuffle = not active

        if isinstance(self._visible_dialog, Playlist):
            idle_add_once(self.splice_playlist)

    def _on_ab_loop_btn_toggled(self, button):
        if not button or not button.get_active():
            self.mpv.command_async("set", "ab-loop-a", "no")
            self.mpv.command_async("set", "ab-loop-b", "no")
        else:
            self.mpv.command_async("ab-loop")

    def _update_loop_state(self):
        icon_loop = "cine-playlist-repeat-symbolic"
        icon_loop_file = "cine-repeat-file-symbolic"
        is_file = self.prop("loop-file")
        is_playlist = self.prop("loop-playlist")

        if is_playlist:
            self._loop_mode = "playlist"
            icon, tooltip, active = icon_loop, _("Loop Playlist"), True
        elif is_file:
            self._loop_mode = "file"
            icon, tooltip, active = icon_loop_file, _("Loop File"), True
        else:
            self._loop_mode = "off"
            icon, tooltip, active = icon_loop, _("Loop"), False

        settings.set_string("loop-state", self._loop_mode)

        self.loop_btn.handler_block_by_func(self._on_loop_toggled)
        self.loop_btn.set_active(active)
        self.loop_btn.handler_unblock_by_func(self._on_loop_toggled)

        self.loop_btn.set_icon_name(icon)
        self.loop_btn.set_tooltip_text(tooltip)

    def _on_loop_toggled(self, _button):
        next_modes = {"off": "playlist", "playlist": "file", "file": "off"}
        curr_mode = self._loop_mode
        target_mode = next_modes[curr_mode]
        loop_file, loop_playlist = {
            "playlist": ("no", "inf"),
            "file": ("inf", "no"),
            "off": ("no", "no"),
        }[target_mode]
        self.mpv.command_async("set", "loop-file", loop_file)
        self.mpv.command_async("set", "loop-playlist", loop_playlist)

    def _sync_can_prev_next(self):
        try:
            count: int = cast(int, self.prop("playlist-count", 0))
            pos: int = cast(int, self.prop("playlist-pos", 0))
            has_multiple: bool = count > 1
            loop_list_on: bool = self.prop("loop-playlist", False) is not False and has_multiple

            self.can_go_prev = loop_list_on or (has_multiple and pos > 0)
            self.can_go_next = loop_list_on or (has_multiple and pos < count - 1)

            self._mpris.update_can_prev_next(self.can_go_prev, self.can_go_next)

            self.previous_btn.props.sensitive = self.can_go_prev
            self.next_btn.props.sensitive = self.can_go_next

            self._actions["previous"].props.enabled = self.can_go_prev
            self._actions["next"].props.enabled = self.can_go_next

            self.shuffle_toggle_btn.props.visible = has_multiple
        except mpv.ShutdownError:
            pass

    def _on_drop_enter(self, _target, _x, _y):
        self.revealer_drop_indicator.set_reveal_child(True)
        return True

    def _on_drop_leave(self, _target):
        self.revealer_drop_indicator.set_reveal_child(False)

    def _on_drop(self, _target, value, _x, _y):
        self.revealer_drop_indicator.set_reveal_child(False)
        items: list[Gio.File] | list[str] = []
        playable_items: list[str] = []

        if is_same_playlist(self.mpv.playlist):
            self.mpv.write_watch_later_config()

        if isinstance(value, Gdk.FileList):
            items = value.get_files()
        elif isinstance(value, str):
            items = [value]

        for item in items:
            if isinstance(item, str):
                playable_items.append(item)
                continue

            path = item.get_path() or item.get_uri()
            if not is_local_path(path):
                playable_items.append(path)
                continue

            try:
                info = item.query_info(
                    "standard::content-type,standard::type",
                    Gio.FileQueryInfoFlags.NONE,
                    None,
                )
            except Exception as e:
                logger.exception("Drop failed")
                idle_add_once(self.show_toast, str(e))
                return

            name = (item.get_basename() or "").lower()
            if name.endswith(SUB_EXTS):
                if not self.prop("idle-active"):
                    self.mpv.command_async("sub-add", path, "select")
                continue

            mime = info.get_content_type() or ""
            if info.get_file_type() == Gio.FileType.DIRECTORY or mime.startswith(
                ("video/", "audio/", "image/")
            ):
                playable_items.append(path)

        for idx, source in enumerate(playable_items):
            mode = "replace" if idx == 0 else "append-play"
            self.mpv.loadfile(source, mode)

        if playable_items:
            self.mpv.command_async("set", "pause", "no")

    def _sync_fullscreen(self, mpv_is_fs: bool):
        self._is_fullscreen = mpv_is_fs
        self.fullscreen() if mpv_is_fs else self.unfullscreen()

    def _set_space_holding(self, hold):
        if hold:
            self._space_hold_id = 0
            if self._click_holding:
                return

            # prevent being able to open menus when clicking buttons while holding spacebar
            # because that causes issues with the internal gtk button handling (space activates it)
            # and it becomes impossible to activate anything again in the window with mouse clicks
            # unless a menu popover is opened again (with keyboard enter)
            self.set_can_target(False)

            self._space_holding = True

            try:
                self._start_fast_forward()
            except mpv.ShutdownError:
                pass
        else:
            self.set_can_target(True)

            if self._space_hold_id:
                GLib.source_remove(self._space_hold_id)
                self._space_hold_id = 0

            if self._space_pressed:
                self._space_pressed = False
                try:
                    self._stop_fast_forward()
                except mpv.ShutdownError:
                    pass

    def _on_key_event(self, _controller, keyval, _keycode, state, event_type):
        self.skip_pause_obs_count = 0
        key_name = Gdk.keyval_name(keyval)

        if self._space_holding and event_type == "keyup":
            self._set_space_holding(False)

        enter = key_name == "Return" and not self._has_enter_binding
        kp_enter = key_name == "KP_Enter" and not self._has_kp_enter_binding

        if key_name in ("Tab", "ISO_Left_Tab") or (enter or kp_enter):
            self.revealer_ui.set_reveal_child(True)
            self.hide_ui_timeout(s=3)
            self._set_space_holding(False)
            return

        clean_state = state & Gtk.accelerator_get_default_mod_mask()
        accel = Gtk.accelerator_name(keyval, clean_state)
        shortcuts_accel = "<Shift><Control>question"
        if self.app.get_actions_for_accel(accel) or accel == shortcuts_accel:
            self._set_space_holding(False)
            return

        mpv_key = chr(Gdk.keyval_to_unicode(keyval))
        mpv_key = KEY_REMAP.get(key_name, mpv_key)

        mods = []
        append_modifiers(state, mods)

        combo = "+".join(mods + [mpv_key])

        if event_type == "keypress":
            if combo in self._nonrepeat_keys and combo in self._pressed_combos:
                return True
            self._pressed_combos.add(combo)
        elif event_type == "keyup":
            self._pressed_combos.discard(combo)

        if combo == "SPACE":
            if event_type == "keypress":
                if self._space_pressed:
                    return True

                self._space_pressed = True

                self._space_hold_id = timeout_add_once(
                    500, self._set_space_holding, True
                )
            elif event_type == "keyup":
                if self._space_hold_id:
                    GLib.source_remove(self._space_hold_id)
                    self._space_hold_id = 0

                if not self._space_holding:
                    self.mpv.command_async("keypress", "SPACE")
                    if self._space_pressed:
                        self._space_pressed = False

            self._space_holding = False
            return True

        try:
            self.mpv.command_async(event_type, combo)
            return True
        except mpv.ShutdownError:
            pass

    def _on_click_pressed(self, gesture, _n_press, x, y, button):
        self.skip_pause_obs_count = 0
        if self._is_hovering_ui() and button != "MBTN_MID":
            return

        if button != "MBTN_LEFT":
            gesture.set_state(Gtk.EventSequenceState.CLAIMED)

        if button == "MBTN_LEFT":
            self._left_clk = settings.get_int("left-click")
        elif button == "MBTN_RIGHT":
            self._right_clk = settings.get_int("right-click")
            if (
                self._right_clk == SecondaryClick.CONTEXT_MENU
                and not self.start_page.props.visible
            ):
                self.clk_rect.x = x
                self.clk_rect.y = y
                self.context_popover_menu.set_pointing_to(self.clk_rect)
                self.context_popover_menu.popup()
                return

        # Back and forward dont trigger _on_click_released when video is playing (??)
        if button in ("MBTN_BACK", "MBTN_FORWARD"):
            self.mpv.command_async("keypress", button)
            return

        self._show_ui()
        self.hide_ui_timeout()

    def _on_click_hold(self, gesture, *args):
        try:
            if self._space_holding or self._is_hovering_ui():
                return

            self._click_holding = True
            self._start_fast_forward()
            gesture.set_state(Gtk.EventSequenceState.CLAIMED)
        except mpv.ShutdownError:
            pass

    def _start_fast_forward(self):
        """Play at twice the speed for as long as a key or button is held."""
        self._prev_speed = float(self.prop("speed", 1.0))
        new_speed = self._prev_speed * 2
        self.mpv.command_async("set", "pause", "no")
        self.mpv.command_async("set", "speed", new_speed)
        self.mpv.command_async("show-text", f"{new_speed:g}× ⯈⯈", "100000000")

    def _stop_fast_forward(self):
        self.mpv.command_async("set", "speed", self._prev_speed)
        self.mpv.command_async("show-text", f"{self._prev_speed:g}×")

    def _run_command(self, cmd):
        try:
            for sub_cmd in cmd.split(";"):
                args = shlex.split(sub_cmd.strip())
                self.mpv.command_async(*args)
        except Exception:
            logger.exception("_run_command failed")

    def _on_click_released(self, gesture, n_press, _x, _y, button):
        gesture.set_state(Gtk.EventSequenceState.CLAIMED)
        ignored_btn = button in ("MBTN_BACK", "MBTN_FORWARD")
        ignore_left = (
            self._is_inactive
            and button == "MBTN_LEFT"
            and self._left_clk == PrimaryClick.FOCUS_PLAY_PAUSE
        )

        if ignored_btn or ignore_left or self._is_hovering_ui():
            return

        is_secondary_pause = (
            button == "MBTN_RIGHT" and self._right_clk == SecondaryClick.PLAY_PAUSE
        )

        if self._click_delay_id:
            GLib.source_remove(self._click_delay_id)
            self._click_delay_id = 0

        if n_press == 1 and not self._click_holding:
            if button == "MBTN_LEFT" and self._left_clk != PrimaryClick.BYPASS:

                def click():
                    self._cycle_pause()
                    self._click_delay_id = 0

                self._click_delay_id = timeout_add_once(self._click_time, click)
                return
        elif n_press == 2 and (cmd_str_dbl := self._mouse_binds.get(f"{button}_DBL")):
            self._run_command(cmd_str_dbl)
            return

        if is_secondary_pause:
            self._cycle_pause()
        elif cmd_str := self._mouse_binds.get(button):
            self._run_command(cmd_str)

    def _cancel_click_hold(self, *args):
        if not self._click_holding:
            return
        try:
            self._stop_fast_forward()
            self._click_holding = False
        except mpv.ShutdownError:
            pass

    def _on_mouse_scroll(self, controller, dx, dy):
        self.skip_pause_obs_count = 0
        event: Gdk.ScrollEvent = controller.get_current_event()
        state = event.get_modifier_state()

        if event.get_unit() == Gdk.ScrollUnit.SURFACE:  # Touchpad
            # Scale it down so it doesn't fire rapidly
            dx *= 0.1
            dy *= 0.1

        self._wheel_accum_x += dx
        self._wheel_accum_y += dy

        rel_dir: Gdk.ScrollRelativeDirection = event.get_relative_direction()  # type: ignore
        is_natural: bool = rel_dir == Gdk.ScrollRelativeDirection.INVERTED  # type: ignore
        UP: str = "WHEEL_DOWN" if is_natural else "WHEEL_UP"
        DOWN: str = "WHEEL_UP" if is_natural else "WHEEL_DOWN"
        LEFT: str = "WHEEL_RIGHT" if is_natural else "WHEEL_LEFT"
        RIGHT: str = "WHEEL_LEFT" if is_natural else "WHEEL_RIGHT"
        wheel: str | None = None

        mods = []
        append_modifiers(state, mods)

        # Only trigger if scrolled a full 'unit'
        if abs(self._wheel_accum_y) >= 1:
            wheel = UP if self._wheel_accum_y < 0 else DOWN
            self._wheel_accum_y = 0.0
        elif abs(self._wheel_accum_x) >= 1:
            wheel = RIGHT if self._wheel_accum_x > 0 else LEFT
            self._wheel_accum_x = 0.0

        if wheel:
            combo = "+".join(mods + [wheel])
            self.mpv.command_async("keypress", combo)

        return True

    def _on_mouse_scroll_volume(self, controller, _dx, dy):
        event: Gdk.ScrollEvent = controller.get_current_event()
        direction: Gdk.ScrollDirection = event.get_direction()
        rel_dir: Gdk.ScrollRelativeDirection = event.get_relative_direction()  # type: ignore
        is_natural: bool = rel_dir == Gdk.ScrollRelativeDirection.INVERTED  # type: ignore
        max_vol = self._volume_max
        step = dy if direction == Gdk.ScrollDirection.SMOOTH else dy * 5

        if is_natural:
            step = -step

        adj = self.volume_scale.get_adjustment()
        volume = adj.get_value()
        new_vol = int(volume - step)
        new_vol = max(adj.get_lower(), min(new_vol, max_vol))
        adj.set_value(new_vol)

        return True

    def _is_hovering_ui(self):
        controls_hover = self.motion_controls.props.contains_pointer
        header_hover = self.motion_header.props.contains_pointer
        separator_hover = self.motion_controls_separator.props.contains_pointer
        hovering = (controls_hover or header_hover) and not separator_hover
        return hovering

    def set_window_size(self, width, height):
        if width <= 0 or height <= 0:
            return

        aspect_ratio = width / height
        base_size = DEFAULT_HEIGHT

        if aspect_ratio < 1:
            new_h = int(base_size / aspect_ratio)
            new_w = base_size
        else:
            new_w = int(base_size * aspect_ratio)
            new_h = base_size

        MAX_W, MAX_H = 1280, 720
        if new_w > MAX_W or new_h > MAX_H:
            scale = min(MAX_W / new_w, MAX_H / new_h)
            new_w = int(new_w * scale)
            new_h = int(new_h * scale)

        self.set_default_size(new_w, new_h)

    def _sync_inhibit(self):
        try:
            should_inhibit = not self.prop("pause") and not self.prop("idle-active")
        except mpv.ShutdownError:
            should_inhibit = False

        if should_inhibit and self._inhibit_cookie == 0:
            self._inhibit_cookie = inhibit_idle(self.app, self, "Playing Media")
        elif not should_inhibit and self._inhibit_cookie != 0:
            uninhibit_idle(self.app, self._inhibit_cookie)
            self._inhibit_cookie = 0

    def _show_icon_indicator(self):
        try:
            if self.prop("idle-active") or self._click_delay_id:
                return
        except mpv.ShutdownError:
            pass

        if not self._hide_icon_indicator:
            self.revealer_icon_indicator.set_reveal_child(True)
            timeout_add_once(350, self.revealer_icon_indicator.set_reveal_child, False)

    def do_close_request(self) -> bool:
        # Nothing queued for this window may start anything new: the
        # thumbnail player in particular would be a fresh mpv on a window
        # that is gone, with nothing left to shut it down.
        self._closing = True
        self._thumb_pending = None
        if self._thumb_timeout_id:
            GLib.source_remove(self._thumb_timeout_id)
            self._thumb_timeout_id = 0
        # A render still going is for a playback that is about to end.
        self.video2x.cancel_all()
        # A load held for a render context that will now never exist would
        # keep mpv from quitting.
        self._video_area.release_loads()
        try:
            same_playlist = is_same_playlist(self.mpv.playlist)
            save_pos = settings.get_boolean("save-video-position")
            if same_playlist or save_pos:
                self.mpv.quit_watch_later()
            else:
                self.mpv.quit()
            self.mpv.wait_for_shutdown(timeout=3)
        except mpv.ShutdownError:
            pass

        if self._inhibit_cookie:
            uninhibit_idle(self.app, self._inhibit_cookie)

        return False

    def splice_playlist(self):
        self._playlist_debounce_id = 0
        self.has_some_doc_path = False
        new_items = []
        for idx, item in enumerate(cast(list, self.mpv.playlist)):
            new_items.append(PlaylistItemObj(item, idx))

            if (
                self.has_some_doc_path
                or not is_document_portal_path(item.get("filename"))
                or has_host_permission
            ):
                continue
            self.has_some_doc_path = True

        if isinstance(self._visible_dialog, Playlist):
            self._visible_dialog.set_save_btn_playlist()
            self._visible_dialog.set_item_count()

        self.playlist_ls.splice(0, self.playlist_ls.get_n_items(), new_items)
        self.prev_shuffle = self.shuffle_toggle_btn.props.active
        self.playlist_changed = False

    def show_toast(self, label, force_dismiss=False):
        toast = Adw.Toast(title=label, timeout=2)
        self.toast_overlay.dismiss_all()
        self.toast_overlay.add_toast(toast)
        if force_dismiss:
            timeout_add_seconds_once(2, toast.dismiss)

    def _setup_observers(self):
        @self.mpv.event_callback("start-file")
        def on_start_file(_event):
            idle_add_once(self.spinner.set_visible, True)

        def on_f_loaded(path):
            try:
                self.spinner.set_visible(False)
                self.is_local_path = is_local_path(path)
                self.start_page.set_sensitive(True)
                self.hide_ui_timeout()
                self._on_ab_loop_btn_toggled(None)

                # The thumbnail player is given this file once its first
                # frame is up (see on_playback_restart); until then it still
                # holds the last one, which must not stay on screen.
                if self.thumb_area and not self._wants_thumb_preview():
                    self.thumb_area.unrealize()
                    self.thumb_area.unmap()
                    self.thumb_area = None
                elif self.thumb_area and not self._thumb_current():
                    self.tooltip_thumb_revealer.set_reveal_child(False)

                self._set_time_tooltip()

                self._mpris.update_metadata()
            except mpv.ShutdownError:
                pass

        # Event thread only: whether the next playback-restart is the first
        # one of a newly loaded file.
        first_restart = False

        @self.mpv.event_callback("file-loaded")
        def on_file_loaded(_event):
            nonlocal first_restart
            trace("file loaded")
            first_restart = True
            self._thumb_pending = None
            self._load_gen += 1
            idle_add_once(on_f_loaded, self.mpv.path)
            timeout_add_seconds_once(5, setattr, self, "_error_count", 0)

        @self.mpv.event_callback("playback-restart")
        def on_playback_restart(_event):
            nonlocal first_restart
            if TRACE:
                # A load or a seek is done once its first frame is on screen.
                trace("playback restarted")
                self._video_area.trace_next_frame = True
            if first_restart:
                first_restart = False
                # Read here rather than on the main thread, where each of
                # these reads would wait for mpv.
                source = self._thumb_source()
                self._thumb_pending = (self._load_gen, source) if source else None
                idle_add_once(self._schedule_thumb_preview)

        @self.mpv.event_callback("end-file")
        def on_end_file(event):
            idle_add_once(self.spinner.set_visible, False)
            idle_add_once(self.start_page.set_sensitive, True)

            try:
                curr_pos = self.mpv.playlist_pos
                info = event.as_dict()
                reason = info["reason"]

                if reason == b"error":
                    # Avoid stopping playback on last file/folder error
                    playlist_count = cast(int, self.mpv.playlist_count)
                    if curr_pos == playlist_count - 1:
                        self.mpv.playlist_pos = 0

                    self._error_count += 1
                    logger.warning(f"File error path: {self._video_path}")
                    error = info["file_error"].decode("utf-8")
                    idle_add_once(self.show_toast, _("File Error") + f": {error}")

                    if self._error_count == 20:
                        self.mpv.stop()
                        self.shuffle_toggle_btn.set_active(False)
                        self._error_count = 0
                elif (
                    not self.mpv.keep_open
                    and self.mpv.idle_active
                    and not self._is_startup
                ):
                    idle_add_once(self.close)
            except mpv.ShutdownError:
                pass

        @self.mpv.property_observer("path")
        def on_path_change(_name, path):
            self._video_path = path

        @self.mpv.property_observer("playlist-count")
        def on_playlist_count_change(_name, _count):
            self.playlist_changed = True
            if isinstance(self._visible_dialog, Playlist):
                if self._playlist_debounce_id > 0:
                    GLib.source_remove(self._playlist_debounce_id)
                    self._playlist_debounce_id = 0
                self._playlist_debounce_id = timeout_add_once(75, self.splice_playlist)
            idle_add_once(self._sync_can_prev_next)

        def update_playing_item(pos):
            try:
                prev_p = self._playlist_prev_pos
                prev_obj = cast(PlaylistItemObj, self.playlist_ls.get_item(prev_p))
                curr_obj = cast(PlaylistItemObj, self.playlist_ls.get_item(pos))
                prev_obj.playing = False
                curr_obj.playing = True
            except (AttributeError, OverflowError):
                pass
            finally:
                self._playlist_prev_pos = pos

        @self.mpv.property_observer("playlist-pos")
        def on_playlist_pos_changed(_name, pos):
            idle_add_once(update_playing_item, pos)

        self._ab_loop_a = None
        self._ab_loop_b = None

        def sync_ab_loop(name):
            scale = self.video_progress_scale
            btn = self.ab_loop_btn

            a_time = self._ab_loop_a
            b_time = self._ab_loop_b
            a_on = a_time is not None
            b_on = b_time is not None
            is_looping = a_on and b_on
            ab_off = not a_on and not b_on

            if name == "ab-loop-a" and a_on:
                scale.add_mark(a_time, Gtk.PositionType.BOTTOM, None)
                btn.add_css_class("a-loop")

            if name == "ab-loop-b" and b_on:
                scale.add_mark(b_time, Gtk.PositionType.BOTTOM, None)
                btn.remove_css_class("a-loop")

            if ab_off and name == "ab-loop-a":
                btn.remove_css_class("a-loop")
                scale.clear_marks()
                for chapter in self._chapters:
                    time_pos = chapter.get("time")
                    if time_pos is not None:
                        scale.add_mark(time_pos, Gtk.PositionType.TOP, None)

            if btn.get_active() != is_looping:
                btn.handler_block_by_func(self._on_ab_loop_btn_toggled)
                btn.set_active(is_looping)
                btn.handler_unblock_by_func(self._on_ab_loop_btn_toggled)

        @self.mpv.property_observer("ab-loop-a")
        @self.mpv.property_observer("ab-loop-b")
        def on_ab_loop_change(name, value):
            val = value if isinstance(value, float) else None
            if name == "ab-loop-a":
                self._ab_loop_a = val
            elif name == "ab-loop-b":
                self._ab_loop_b = val
            idle_add_once(sync_ab_loop, name)

        def sync_loop(name, value):
            new_mode = "no"
            if name == "loop-playlist" and value == "inf":
                self.mpv.command_async("set", "loop-file", "no")
                self._state["loop-file"] = False
                new_mode = "playlist"
                self._sync_can_prev_next()
            elif name == "loop-file" and value == "inf":
                self.mpv.command_async("set", "loop-playlist", "no")
                self._state["loop-playlist"] = False
                new_mode = "file"
            elif name == "loop-playlist":
                self._sync_can_prev_next()

            if self._loop_mode != new_mode:
                self._loop_mode = new_mode
                self._update_loop_state()
                self._mpris.update_loop()

        @self.mpv.property_observer("loop-playlist")
        @self.mpv.property_observer("loop-file")
        def on_loop_change(name, value):
            idle_add_once(sync_loop, name, value)

        def sync_fs(value):
            icon = (
                "cine-view-restore-symbolic"
                if value
                else "cine-view-fullscreen-symbolic"
            )
            text = _("Exit Fullscreen") if value else _("Fullscreen")
            self.fullscreen_btn.set_tooltip_text(text)
            self.fullscreen_btn.set_icon_name(icon)
            self._sync_fullscreen(value)

        @self.mpv.property_observer("fullscreen")
        def on_fs_change(_name, value):
            idle_add_once(sync_fs, value)
            self.hide_ui_timeout()

        @self.mpv.property_observer("time-pos")
        def on_time_change(_name, value):
            pos = float(value or 0)
            if TRACE and value is not None:
                self._loop_probe(pos)
            # mpv reports every frame, and the bar and the clock show tenths
            # at most. Wake the main loop only when what they show changes,
            # and not again until that update has run.
            tenth = round(pos, 1)
            if tenth == self._sent_prog_tenth:
                return
            self._sent_prog_tenth = tenth
            if not self._prog_update_queued:
                self._prog_update_queued = True
                idle_add_once(self._flush_progress)

        def emit_seeked(pos):
            # MPRIS describes the active window, whichever one seeked.
            if self.app.props.active_window is self:
                self._mpris.emit_seeked(pos)

        @self.mpv.property_observer("seeking")
        def on_seeking_change(_name, seeking):
            if not seeking and self._mpris.connected:
                idle_add_once(emit_seeked, self.mpv.time_pos)

        @self.mpv.property_observer("duration")
        def on_duration_change(_name, value):
            self._loop_duration = float(value or 0)
            idle_add_once(self._update_duration, float(value or 0))

        def show_icon_requested() -> bool:
            """Whether the key that made this change asked for the on-screen
            icon. Event thread only: it waits on mpv."""
            try:
                return (self.mpv.user_data or {}).get("show-icon") == "yes"
            except mpv.ShutdownError:
                return False

        def sync_mute(muted, show_icon):
            self.mute_toggle_btn.handler_block(self.mute_handler_id)
            self.mute_toggle_btn.set_active(muted)
            self.mute_toggle_btn.handler_unblock(self.mute_handler_id)
            self._update_volume_icon()

            if show_icon:
                self.icon_indicator.props.icon_name = (
                    self.volume_menu_btn.props.icon_name
                )
                self._show_icon_indicator()

        @self.mpv.property_observer("mute")
        def on_mute_change(_name, muted):
            idle_add_once(sync_mute, muted, show_icon_requested())

        def update_icon_and_vol_adj(value):
            vol = int(value)
            # block the signal to not trigger value-changed
            self.volume_scale.handler_block(self.volume_handler_id)
            self.volume_scale_adj.set_value(vol)
            self.volume_scale.handler_unblock(self.volume_handler_id)

            if vol > 0 and self.prop("mute"):
                self.mpv.command_async("set", "mute", "no")

            if self.volume_menu_btn.props.active:
                self.mpv.command_async("show-text", _("Volume") + f": {vol}%")

            self._update_volume_icon()
            settings.set_int("volume", vol)
            self._mpris.update_volume(vol)

        @self.mpv.property_observer("volume")
        def on_volume_change(_name, value):
            idle_add_once(update_icon_and_vol_adj, value)

        track_map = {
            "sid": "select-subtitle",
            "aid": "select-audio",
            "vid": "select-video",
        }

        def set_track(name, value):
            action_name = track_map.get(name) or ""
            val = value if isinstance(value, int) else 0
            if action := self.lookup_action(action_name):
                action.set_state(  # pyright: ignore[reportAttributeAccessIssue]
                    GLib.Variant("i", val)
                )

        def on_track_change(name, value):
            idle_add_once(set_track, name, value)

        for prop in track_map:
            self.mpv.property_observer(prop)(on_track_change)

        @self.mpv.property_observer("track-list")
        def on_track_list_change(_name, track_list):
            idle_add_once(self._update_track_menus, track_list)

        @self.mpv.property_observer("playlist-pos")
        def on_pl_pos_change(_name, _value):
            idle_add_once(self._sync_can_prev_next)

        @self.mpv.property_observer("chapter-list")
        def on_chapter_list_change(_name, chapters):
            self._chapters = []
            idle_add_once(self._update_chapter_marks_and_menu, chapters)

        @self.mpv.property_observer("chapter")
        def on_chapter_change(_name, chapter_idx):
            if chapter_idx is not None and self.chapters_menu_btn.get_active():
                idle_add_once(self._sync_chapter_menu_selected)

        @self.mpv.property_observer("pause")
        def on_pause_change(_name, paused):
            if self.mpv.eof_reached:  # allow to replay at eof, requires keep-open
                self.mpv.seek(0, reference="absolute")
                self.skip_pause_obs_count = 0

            if self.skip_pause_obs_count > 0:
                self.skip_pause_obs_count -= 1
                return

            idle_add_once(self._sync_inhibit)
            idle_add_once(self._update_play_pause_icon, paused)

        def sync_idle_active(is_idle):
            self._actions["open-sub-menu"].set_enabled(not is_idle)
            self._actions["open-audio-menu"].set_enabled(not is_idle)

            self.title_widget.set_visible(not is_idle)
            self.start_page.set_visible(is_idle)
            self.controls_box.set_visible(not is_idle)
            self._video_area.set_visible(not is_idle)

            self.drop_label.props.label = (
                _("Play") if is_idle else _("Play or Add Subtitles")
            )
            self.drop_icon.props.icon_name = (
                "cine-playback-start-symbolic"
                if is_idle
                else "cine-play-or-sub-symbolic"
            )

            if is_idle:
                self._error_count = 0
                self.revealer_ui.set_reveal_child(True)
                self.set_title(_("NekoPlay"))
                self._hide_icon_indicator = True
                if isinstance(self._visible_dialog, Playlist):
                    self._visible_dialog.close()

            self._sync_inhibit()

        @self.mpv.property_observer("idle-active")
        def on_idle_change(_name, is_idle):
            self._is_startup = False
            idle_add_once(sync_idle_active, is_idle)

        def sync_title(title, filename):
            # python-mpv hands over a name that is not valid UTF-8 as bytes,
            # which GTK will not take.
            if isinstance(title, bytes):
                title = title.decode("utf-8", "replace")
            if isinstance(filename, bytes):
                filename = filename.decode("utf-8", "replace")
            try:
                if title == filename:
                    title_no_ext = os.path.splitext(title)[0]
                    self.set_title(title_no_ext)
                    self.title_widget.set_title(title_no_ext)
                else:
                    self.set_title(title)
                    self.title_widget.set_title(title)
                    pos = abs(cast(int, self.prop("playlist-pos", 0)))
                    if obj := cast(PlaylistItemObj, self.playlist_ls.get_item(pos)):
                        obj.notify("playing")

                self._hide_icon_indicator = False
                self._mpris.update_props()
            except mpv.ShutdownError:
                pass

        @self.mpv.property_observer("media-title")
        def on_title_change(_name, title):
            if title:
                idle_add_once(sync_title, title, self.mpv.filename)

        @self.mpv.property_observer("sub-scale")
        def on_sub_scale_change(_name, value):
            if self._visible_dialog is None:
                idle_add_once(settings.set_double, "subtitle-scale", value)

        def set_sub_icon(name, value, show_icon):
            sub_on_icon = "cine-subtitles-symbolic"
            sub_off_icon = "cine-subtitles-off-symbolic"

            sub_on = (value == "auto" or value) and self.prop("sid")
            self.subtitles_menu_btn.props.icon_name = (
                sub_on_icon if sub_on else sub_off_icon
            )

            if show_icon:
                icon = sub_on_icon if sub_on else sub_off_icon
                self.icon_indicator.props.icon_name = icon
                self._show_icon_indicator()

        @self.mpv.property_observer("sub-visibility")
        @self.mpv.property_observer("sid")
        def on_sub_vis_change(name, value):
            show_icon = name == "sub-visibility" and show_icon_requested()
            idle_add_once(set_sub_icon, name, value, show_icon)

        def set_aid_icon(value):
            audio_on = value == "auto" or value
            self.audio_tracks_menu_btn.props.icon_name = (
                "cine-audio-symbolic" if audio_on else "cine-audio-off-symbolic"
            )

        @self.mpv.property_observer("aid")
        def on_aid_change(_name, value):
            idle_add_once(set_aid_icon, value)

        @self.mpv.property_observer("vid")
        def on_vid_change(_name, value):
            idle_add_once(self.audio_only_icon.set_visible, not bool(value))
            if not value:
                # clear the last frame, which sometimes can still be present
                idle_add_once(self._video_area.queue_render)

        @self.mpv.property_observer("video-zoom")
        def on_zoom_change(_name, value):
            if round(value, 2) == 0.00:
                self.mpv["video-align-x"] = 0
                self.mpv["video-align-y"] = 0

        @self.mpv.property_observer("vo")
        def on_vo_change(_name, vo_list):
            try:
                if vo_list[0].get("name") != "libmpv":
                    self.mpv["vo"] = "libmpv"
            except mpv.ShutdownError:
                pass

        def close_after_quit():
            # A dialog open on the window takes its close request for
            # itself, which would leave the window up with no player.
            for _i in range(8):
                dialog = self.get_visible_dialog()
                if dialog is None:
                    break
                dialog.force_close()
            self.close()

        @self.mpv.event_callback("shutdown")
        def on_quit(_event):
            idle_add_once(close_after_quit)

    @property
    def can_load_before_present(self) -> bool:
        """Whether files may be handed to mpv before the window is shown;
        see VideoGLArea._hold_loads."""
        return self._video_area.holds_loads

    def prop(self, name: str, default=None):
        """The last value mpv reported for one of STATE_PROPS, or *default*
        while it has none. Never waits on mpv, so safe on the main thread."""
        value = self._state.get(name)
        return default if value is None else value

    def _connect(self, signal_name):
        return lambda func: self.connect(signal_name, func)

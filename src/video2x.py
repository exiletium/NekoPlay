# video2x.py
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

"""AI upscaling and frame interpolation through video2x_optimized.

video2x_optimized runs RIFE (interpolation) and Real-ESRGAN (upscaling) on
the GPU through ONNX Runtime and writes a new file. It is a separate program
with its own Python, so it is driven as a subprocess and never imported.

Where it meets the player is an mpv ``on_load`` hook (video2x.lua). When a
file is about to open, the hook asks this module what to play instead, and
the answer is the rendered file: mpv keeps reporting the original path, title
and playlist entry, and only the bytes come from the render. That is the same
trick ytdl_hook uses.

Two ways to wait for the render:

- **Pre-render** keeps the hook deferred until the whole file is done, then
  opens the result. Full quality, fully seekable, and playback does not
  start until the render has finished.
- **Live** opens the output as soon as a few seconds of it exist and follows
  the file as it grows. When the render is slower than playback the player
  waits for it, the way it would buffer a stream - and once a pass has been
  timed on this machine, the source is decoded small enough that the next
  live render is not slower (LIVE_HEADROOM). Seeking far past what has been
  rendered starts a new render from there rather than waiting for the old
  one to arrive. What mpv opens in live mode is an EDL: the original up to
  where the render begins, then the growing file, with the film's full
  length declared, so the seek bar spans the whole film from the start.

Two settings, upscaling and frame interpolation, combine into a recipe of
one or two passes (interpolation first; see Recipe). Rendered files are
cached under the config directory, keyed by the source file and the recipe,
so a file is rendered once. Engines are per-resolution.
A missing one is built on the spot: recent video2x_optimized re-targets an
engine it already has, inside the render command, in a fraction of a second
and without PyTorch. Older installs fall back to a real export from the
weights, which needs torch and takes seconds for an upscaler or a minute or
two for RIFE, and is shown as its own "engine" phase.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from gettext import gettext as _

import gi

gi.require_version("Adw", "1")
gi.require_version("Gdk", "4.0")
gi.require_version("GLib", "2.0")
from gi.repository import Adw, Gdk, GLib

from .platform_compat import IS_WINDOWS, SUBPROCESS_FLAGS, TRACE, ProcessTree
from .utils import CONFIG_DIR, idle_add_once

logger = logging.getLogger(__name__)
if TRACE:
    # The decisions here are otherwise invisible from outside; the startup
    # trace is the natural place to ask for them.
    logger.setLevel(logging.INFO)

# --- Settings and recipes ---------------------------------------------------
#
# Two independent settings, upscaling and frame interpolation, each with its
# own row in the options panel and in Preferences. Their orders match the
# dropdowns in options.blp and preferences.blp.

UPSCALE_OFF = "off"
UPSCALE_X4 = "x4"
UPSCALE_INDEX_MAP: list[str] = [UPSCALE_OFF, UPSCALE_X4]
UPSCALE_TO_INDEX: dict[str, int] = {v: i for i, v in enumerate(UPSCALE_INDEX_MAP)}
UPSCALE_FACTOR = {UPSCALE_OFF: 1, UPSCALE_X4: 4}

INTERP_OFF = "off"
INTERP_2X = "2x"
INTERP_4X = "4x"
INTERP_INDEX_MAP: list[str] = [INTERP_OFF, INTERP_2X, INTERP_4X]
INTERP_TO_INDEX: dict[str, int] = {v: i for i, v in enumerate(INTERP_INDEX_MAP)}
INTERP_FACTOR = {INTERP_OFF: 1, INTERP_2X: 2, INTERP_4X: 4}

RENDER_PRE = "pre"
RENDER_LIVE = "live"
RENDER_INDEX_MAP: list[str] = [RENDER_PRE, RENDER_LIVE]
RENDER_TO_INDEX: dict[str, int] = {r: i for i, r in enumerate(RENDER_INDEX_MAP)}

QUALITY_SPEED = "speed"
QUALITY_BALANCED = "balanced"
QUALITY_QUALITY = "quality"
QUALITY_INDEX_MAP: list[str] = [QUALITY_SPEED, QUALITY_BALANCED, QUALITY_QUALITY]
QUALITY_TO_INDEX: dict[str, int] = {q: i for i, q in enumerate(QUALITY_INDEX_MAP)}

# The heights the "Maximum Render Height" row offers, by row index. 0 is
# "No limit"; the rest cap the upscaler's output (interpolation is never
# capped - see RenderJob.frame_size).
MAX_HEIGHT_INDEX_MAP: list[int] = [0, 1080, 1440, 2160, 2880]
MAX_HEIGHT_TO_INDEX: dict[int, int] = {h: i for i, h in enumerate(MAX_HEIGHT_INDEX_MAP)}

STAGE_INTERP = "interp"
STAGE_UPSCALE = "upscale"


@dataclass(frozen=True)
class Stage:
    """One pass of video2x: interpolate by a factor, or upscale by one."""

    kind: str
    factor: int

    @property
    def label(self) -> str:
        if self.kind == STAGE_INTERP:
            return _("Interpolate %d×") % self.factor
        return _("Upscale ×%d") % self.factor

    def out_fps(self, fps: float) -> float:
        return fps * self.factor if self.kind == STAGE_INTERP else fps

    def out_frames(self, frames: int) -> int:
        if self.kind == STAGE_INTERP:
            return max(0, (frames - 1) * self.factor + 1)
        return frames


@dataclass(frozen=True)
class Recipe:
    """What to do to a file: the two settings combined.

    video2x does one thing per run, so both on means two passes. They go
    interpolation first: RIFE's cost is in full-resolution warps, so
    interpolating the upscaled frames would cost sixteen times as much,
    while upscaling twice the frames only costs twice.
    """

    interp: int = 1
    upscale: int = 1

    @property
    def active(self) -> bool:
        return self.interp > 1 or self.upscale > 1

    @property
    def key(self) -> str:
        parts = []
        if self.interp > 1:
            parts.append(f"interp{self.interp}")
        if self.upscale > 1:
            parts.append(f"up{self.upscale}")
        return "+".join(parts) or "off"

    @property
    def stages(self) -> list[Stage]:
        out = []
        if self.interp > 1:
            out.append(Stage(STAGE_INTERP, self.interp))
        if self.upscale > 1:
            out.append(Stage(STAGE_UPSCALE, self.upscale))
        return out

    @property
    def label(self) -> str:
        return " → ".join(stage.label for stage in self.stages) or _("Off")

# Live playback opens the file once this much of it exists, and pauses to
# let the render get this far ahead again whenever it catches up.
LIVE_LEAD_SECONDS = 4.0
# mpv reads about a second ahead of the playhead, and the encoder holds a
# few frames back; staying this far from the end of the file keeps the
# demuxer from ever seeing EOF while the render is still going.
LIVE_MARGIN_SECONDS = 1.5
# A live render is meant to keep up with playback. Once a kind of pass has
# been timed on this machine (see record_speed), the source is decoded small
# enough for the pass to run this much faster than realtime.
LIVE_HEADROOM = 1.25
# Seeking past what has been rendered: if the render would take longer than
# this to get there, start another one from there instead of waiting.
LIVE_RESTART_WAIT_SECONDS = 8.0
# A render started for a seek begins this far before the target, so that a
# small step back does not fall out of the rendered part.
LIVE_BACKOFF_SECONDS = 2.0
# Decoding smaller stops here: the models make little sense below it.
LIVE_MIN_DECODE_HEIGHT = 240
# Decode heights are rounded down to this step, so that the small drift in
# the speed record from one render to the next does not make every live
# session a slightly different size - and a different cache entry.
DECODE_STEP = 24

# Where the timings live: seconds per output frame per input megapixel, by
# kind of pass. Outside the cache directory so clearing the cache keeps them.
SPEED_FILE = os.path.join(CONFIG_DIR, "video2x-speed.json")

CACHE_DIR = os.path.join(CONFIG_DIR, "video2x")

SR_WEIGHTS = "realesr-animevideov3.pth"
RIFE_WEIGHTS = "flownet_v4.26.pkl"

_PROGRESS = re.compile(
    r"([\d.]+)%\s+(\d+)/(\d+)\s+frames\s+([\d.]+)\s+fps out\s+infer\s+([\d.]+)\s+ms"
)


# --- The install -----------------------------------------------------------


@dataclass
class Install:
    """A video2x_optimized checkout or portable bundle, and what it can do."""

    root: str
    python: str | None = None
    ffmpeg_dir: str | None = None
    problem: str | None = None
    _can_export: bool | None = field(default=None, repr=False)
    _can_autobuild: bool | None = field(default=None, repr=False)

    @property
    def usable(self) -> bool:
        return self.problem is None

    @property
    def models_dir(self) -> str:
        return os.path.join(self.root, "models")

    @property
    def engine_dirs(self) -> list[str]:
        """Where engines are: the shipped set, then the ones video2x built."""
        return [self.models_dir, os.path.join(self.models_dir, "auto")]

    @property
    def can_export(self) -> bool:
        """Whether this install can build an engine for a new resolution.

        Needs PyTorch. Importing it takes seconds, so this is only asked
        when an engine is actually missing, and only once.
        """
        if self._can_export is None:
            self._can_export = bool(self.python) and _python_has(
                self.python, "torch, onnx"
            )
        return self._can_export

    @property
    def can_autobuild(self) -> bool:
        """Whether video2x builds a missing engine by itself.

        Recent video2x_optimized re-targets an engine it already has to the
        new resolution -- regenerating the few tensors that encode the
        resolution with numpy -- rather than re-exporting from the weights.
        That needs onnx but not torch, takes a fraction of a second, and
        happens inside the render command, so there is nothing to pre-build
        and no separate "engine" phase to show.

        Probed in the install's own root because video2x_opt is imported from
        there rather than installed.
        """
        if self._can_autobuild is None:
            self._can_autobuild = bool(self.python) and _python_has(
                self.python, "onnx, video2x_opt.onnx_respec", cwd=self.root
            )
        return self._can_autobuild

    def engine_path(self, stage: Stage, width: int, height: int) -> str:
        if stage.kind == STAGE_UPSCALE:
            stem = os.path.splitext(SR_WEIGHTS)[0]
            return os.path.join(self.models_dir, f"{stem}_{width}x{height}_u8.onnx")
        return os.path.join(self.models_dir, f"rife_v4.26_{width}x{height}_u8_fast.onnx")

    def has_engine(self, stage: Stage, width: int, height: int) -> bool:
        names = [os.path.basename(self.engine_path(stage, width, height))]
        if stage.kind != STAGE_UPSCALE:
            # The CLI falls back to the reference engine when there is no
            # fast one for a resolution.
            names.append(f"rife_v4.26_{width}x{height}_u8.onnx")
        return any(
            os.path.isfile(os.path.join(d, name)) for d in self.engine_dirs for name in names
        )

    def export_command(self, stage: Stage, width: int, height: int) -> list[str]:
        assert self.python
        if stage.kind == STAGE_UPSCALE:
            return [
                self.python,
                "-m",
                "video2x_opt.sr.export_onnx",
                "--weights",
                os.path.join("models", SR_WEIGHTS),
                "--height",
                str(height),
                "--width",
                str(width),
            ]
        # The exporter's default name is not the one the CLI looks for, so
        # say where to put it. --warp-mode all is the "fast" engine the CLI
        # prefers: ~1.35x quicker than the reference graph for a difference
        # on moving edges that the README measures at 40 dB.
        return [
            self.python,
            "-m",
            "video2x_opt.rife.export_onnx",
            "--weights",
            os.path.join("models", RIFE_WEIGHTS),
            "--height",
            str(height),
            "--width",
            str(width),
            "--fp16",
            "--warp-mode",
            "all",
            "--out",
            os.path.relpath(self.engine_path(stage, width, height), self.root),
        ]

    def render_command(
        self,
        stage: Stage,
        src: str,
        dst: str,
        stream: bool,
        offset: float = 0.0,
        max_height: int = 0,
        quality: str = QUALITY_BALANCED,
        follow: bool = False,
        expect_frames: int = 0,
        no_audio: bool = False,
        audio_from: str | None = None,
        audio_start: float = 0.0,
    ) -> list[str]:
        """The command for one pass.

        *offset* is where in the source to begin, in seconds; the output's
        timestamps start at zero regardless. *max_height* has video2x decode
        the source smaller, so that this pass's output is at most that tall:
        for an upscale that is four times the decode height, for
        interpolation the decode height itself. *quality* is the encoder
        effort. For a live chain, the last pass reads the first pass's output
        while it is still being written: *follow* makes the reader wait for
        more instead of stopping at that file's current end, *expect_frames*
        tells it when the input is really complete, and *audio_from* /
        *audio_start* take the sound from the original file rather than the
        partial intermediate.
        """
        assert self.python
        cmd = [self.python, "-m", "video2x_opt", "-i", src, "-o", dst]
        if stage.kind == STAGE_UPSCALE:
            cmd.append("--upscale")
        else:
            cmd += ["-m", str(stage.factor)]
        if offset > 0:
            cmd += ["--start", "%.3f" % offset]
        if max_height > 0:
            cmd += ["--max-output-height", str(max_height)]
        if quality and quality != QUALITY_BALANCED:
            cmd += ["--quality", quality]
        if no_audio:
            cmd.append("--no-audio")
        if audio_from:
            cmd += ["--audio-from", audio_from]
            if audio_start > 0:
                cmd += ["--audio-from-start", "%.3f" % audio_start]
        if follow:
            cmd.append("--follow")
            if expect_frames > 0:
                cmd += ["--expect-frames", str(expect_frames)]
        if stream:
            cmd.append("--stream")
        return cmd

    def child_env(self) -> dict[str, str]:
        env = _foreign_python_env()
        if self.ffmpeg_dir:
            env["PATH"] = self.ffmpeg_dir + os.pathsep + env.get("PATH", "")
        # The pipeline prints progress with carriage returns, and Python
        # would otherwise buffer it until exit.
        env["PYTHONUNBUFFERED"] = "1"
        return env


def _foreign_python_env() -> dict[str, str]:
    """An environment another Python can start in.

    The launcher points PYTHONHOME at the bundle so the app's interpreter
    finds its library. Any other interpreter that inherits it looks for its
    standard library in the wrong place and dies before running a line, so
    it has to go - along with anything else that steers a Python.
    """
    env = dict(os.environ)
    for name in ("PYTHONHOME", "PYTHONPATH", "PYTHONSTARTUP", "PYTHONDONTWRITEBYTECODE"):
        env.pop(name, None)
    return env


def _python_has(python: str, module: str, cwd: str | None = None) -> bool:
    try:
        return (
            subprocess.run(
                [python, "-c", f"import {module}"],
                cwd=cwd,
                env=_foreign_python_env(),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=60,
                creationflags=SUBPROCESS_FLAGS,
            ).returncode
            == 0
        )
    except Exception:
        return False


def _python_candidates(root: str) -> list[str]:
    """Every interpreter worth asking, most likely first.

    shutil.which alone is not enough on Windows: the first python.exe on a
    default PATH is the Microsoft Store alias, a stub that exits at once
    saying Python is not installed, and the real one sits behind it. So
    every PATH entry is looked at, the aliases are skipped, and the
    interpreters Windows knows about through the registry (PEP 514) are
    added, since those need not be on PATH at all.
    """
    names = ("python.exe", "python3.exe") if IS_WINDOWS else ("python3", "python")
    seen: set[str] = set()
    out: list[str] = []

    def add(path: str) -> None:
        key = os.path.normcase(os.path.abspath(path))
        if key in seen or not os.path.isfile(path):
            return
        if key == os.path.normcase(sys.executable):
            return  # the app's own interpreter has no onnxruntime
        if IS_WINDOWS and "windowsapps" in key:
            return  # the Store alias stub
        seen.add(key)
        out.append(path)

    add(os.path.join(root, "python", "python.exe"))  # the portable bundle

    if IS_WINDOWS:
        try:
            import winreg

            for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
                try:
                    core = winreg.OpenKey(hive, r"Software\Python\PythonCore")
                except OSError:
                    continue
                with core:
                    for i in range(64):
                        try:
                            tag = winreg.EnumKey(core, i)
                        except OSError:
                            break
                        try:
                            with winreg.OpenKey(core, tag + r"\InstallPath") as k:
                                exe = winreg.QueryValueEx(k, "ExecutablePath")[0]
                        except OSError:
                            try:
                                with winreg.OpenKey(core, tag + r"\InstallPath") as k:
                                    exe = os.path.join(winreg.QueryValue(k, None), "python.exe")
                            except OSError:
                                continue
                        add(exe)
        except Exception:
            logger.debug("Registry scan for Python failed", exc_info=True)

    for directory in os.environ.get("PATH", "").split(os.pathsep):
        if not directory:
            continue
        for name in names:
            add(os.path.join(directory, name))
    return out


def _find_ffmpeg_dir(root: str) -> str | None:
    """Where ffmpeg and ffprobe are, if anywhere; video2x calls both by name."""
    exe = "ffmpeg.exe" if IS_WINDOWS else "ffmpeg"
    candidates = [os.path.join(root, "bin")]
    found = shutil.which("ffmpeg")
    if found:
        candidates.append(os.path.dirname(found))
    for directory in candidates:
        if os.path.isfile(os.path.join(directory, exe)):
            return directory
    return None


def _candidate_roots(configured: str) -> list[str]:
    pkgdatadir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return [
        p
        for p in (
            configured,
            os.path.join(pkgdatadir, "video2x"),  # bundled with the app
            os.path.join(CONFIG_DIR, "video2x-install"),  # dropped in by hand
        )
        if p
    ]


_install_lock = threading.Lock()
_install_cache: dict[str, Install] = {}


def locate(configured: str) -> Install:
    """Find video2x_optimized and work out how to run it.

    Slow the first time - it launches interpreters to see what they can
    import - and cached per configured path after that.
    """
    with _install_lock:
        if configured in _install_cache:
            return _install_cache[configured]

    install = _locate(configured)

    with _install_lock:
        _install_cache[configured] = install
    return install


def forget_install() -> None:
    """Drop the cached discovery, after the configured path changes."""
    with _install_lock:
        _install_cache.clear()


def _locate(configured: str) -> Install:
    roots = _candidate_roots(configured)
    root = next(
        (r for r in roots if os.path.isfile(os.path.join(r, "video2x_opt", "cli.py"))),
        None,
    )
    if root is None:
        return Install(
            root=configured or roots[-1],
            problem=_("video2x_optimized was not found. Set its folder in Preferences."),
        )

    install = Install(root=root)

    # The portable bundle carries its own interpreter; a checkout relies on
    # a system Python that has onnxruntime.
    for python in _python_candidates(root):
        if _python_has(python, "onnxruntime"):
            install.python = python
            break
    if install.python is None:
        install.problem = _(
            "No Python with onnxruntime was found for video2x_optimized."
        )
        return install

    install.ffmpeg_dir = _find_ffmpeg_dir(root)
    if install.ffmpeg_dir is None:
        install.problem = _("ffmpeg was not found. video2x_optimized needs it on PATH.")
        return install

    return install


# --- The source file -------------------------------------------------------


@dataclass
class Source:
    """A file to render. Width and height are as displayed: a phone video
    stored on its side with a 90° display matrix is tall here, because that
    is the frame video2x decodes and the engine has to fit."""

    path: str
    width: int
    height: int
    fps: float
    nb_frames: int
    duration: float
    rotation: int


def decode_size(source: Source, max_height: int, upscale: int) -> tuple[int, int] | None:
    """The size video2x decodes *source* at for ``--max-output-height``.

    The same arithmetic as video2x_opt's scaled_source_size, so that the
    engine looked for here is the one the render will ask for. None when
    the source already fits.
    """
    if max_height <= 0:
        return None
    target_h = max_height // max(1, upscale)
    if target_h <= 0 or source.height <= target_h:
        return None
    f = target_h / float(source.height)
    w = max(2, int(round(source.width * f)) & ~1)
    h = max(2, int(target_h) & ~1)
    return w, h


def parse_start(value: str) -> float:
    """Seconds from mpv's ``start`` option, or 0 for anything else.

    Handles the forms mpv writes itself - plain seconds, ``+seconds`` and
    ``hh:mm:ss.ms`` - and gives up on chapters, percentages and negative
    (from-the-end) offsets, which nothing here produces.
    """
    value = (value or "").strip()
    if not value or value == "none" or value.startswith(("-", "#")) or value.endswith("%"):
        return 0.0
    value = value.lstrip("+")
    try:
        parts = [float(x) for x in value.split(":")]
    except ValueError:
        return 0.0
    seconds = 0.0
    for part in parts:
        seconds = seconds * 60 + part
    return max(0.0, seconds)


def probe_source(path: str, ffmpeg_dir: str | None) -> Source | None:
    ffprobe = shutil.which(
        "ffprobe", path=ffmpeg_dir or os.environ.get("PATH")
    ) or shutil.which("ffprobe")
    if not ffprobe:
        return None
    try:
        out = subprocess.check_output(
            [
                ffprobe,
                "-v",
                "error",
                "-print_format",
                "json",
                "-show_streams",
                "-show_format",
                path,
            ],
            text=True,
            timeout=15,
            stderr=subprocess.DEVNULL,
            creationflags=SUBPROCESS_FLAGS,
        )
        data = json.loads(out)
    except Exception:
        logger.exception("ffprobe failed for %r", path)
        return None

    video = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), None)
    if not video:
        return None

    rate = video.get("avg_frame_rate") or video.get("r_frame_rate") or "0/1"
    try:
        num, den = (int(x) for x in rate.split("/"))
        fps = num / den if den else 0.0
    except ValueError:
        fps = 0.0

    duration = float(video.get("duration") or data.get("format", {}).get("duration") or 0)
    nb_frames = int(video.get("nb_frames") or 0)
    if not nb_frames and duration and fps:
        nb_frames = int(round(duration * fps))

    rotation = 0
    for side in video.get("side_data_list") or []:
        if "rotation" in side:
            try:
                rotation = int(side["rotation"])
            except (TypeError, ValueError):
                pass
    try:
        rotation = rotation or int((video.get("tags") or {}).get("rotate", 0))
    except (TypeError, ValueError):
        pass

    width, height = int(video.get("width", 0)), int(video.get("height", 0))
    if rotation % 180 == 90:
        # ffmpeg applies the display matrix on decode, so video2x sees the
        # frame upright: the coded 1080x608 arrives as 608x1080.
        width, height = height, width

    return Source(
        path=path,
        width=width,
        height=height,
        fps=fps,
        nb_frames=nb_frames,
        duration=duration,
        rotation=rotation,
    )


# --- The cache -------------------------------------------------------------


def _smaller_decode(
    a: tuple[int, int] | None, b: tuple[int, int] | None
) -> tuple[int, int] | None:
    """Whichever of two decode sizes is shorter; None counts as no limit."""
    if a is None:
        return b
    if b is None:
        return a
    return a if a[1] <= b[1] else b


def cache_key(
    source: Source, recipe: Recipe, decode_height: int = 0, quality: str = QUALITY_BALANCED
) -> str:
    """One key per file, recipe, encoder quality and - when the source was
    decoded smaller to keep a live render realtime, or capped by the height
    limit - decode height, because each is a different output."""
    stat = os.stat(source.path)
    raw = f"{os.path.abspath(source.path)}|{stat.st_size}|{int(stat.st_mtime)}|{recipe.key}"
    if decode_height:
        raw += f"|h{decode_height}"
    if quality and quality != QUALITY_BALANCED:
        raw += f"|q{quality}"
    return hashlib.sha1(raw.encode("utf-8", "surrogateescape")).hexdigest()[:20]


def partial_stem(key: str, offset: float) -> str:
    """The name stem of a render that begins *offset* seconds in.

    Such a render is for one sitting - it is never complete - so it gets
    no marker and is swept as soon as playback lets go of it.
    """
    return f"{key}.from{int(round(offset * 1000))}"


def stage_path(key: str, index: int) -> str:
    """Where a chain parks the output of a pass that is not the last."""
    return os.path.join(CACHE_DIR, f"{key}.stage{index}.mkv")


def cache_paths(key: str) -> tuple[str, str]:
    """The rendered file, and the marker that says it is complete.

    A marker rather than a rename, because the live player holds the file
    open while the render finishes, and Windows will not rename a file that
    is open without FILE_SHARE_DELETE - which mpv does not ask for.
    """
    base = os.path.join(CACHE_DIR, key)
    return base + ".mkv", base + ".done"


def cache_size() -> int:
    total = 0
    try:
        for name in os.listdir(CACHE_DIR):
            try:
                total += os.path.getsize(os.path.join(CACHE_DIR, name))
            except OSError:
                pass
    except OSError:
        pass
    return total


def clear_cache() -> int:
    """Delete every cached render. Returns how many bytes were freed."""
    freed = 0
    try:
        for name in os.listdir(CACHE_DIR):
            path = os.path.join(CACHE_DIR, name)
            try:
                size = os.path.getsize(path)
                os.remove(path)
                freed += size
            except OSError:
                # In use by a render or by playback; it will go next time.
                pass
    except OSError:
        pass
    return freed


def touch_cache_entry(key: str) -> None:
    """Note that a render was just used, so it is the last to be evicted."""
    try:
        os.utime(cache_paths(key)[1], None)
    except OSError:
        pass


def trim_cache(limit_bytes: int, keep: set[str] = frozenset()) -> int:
    """Bring the cache under *limit_bytes*, oldest renders first.

    A 4K render is about 7 GB an hour, so the cache would fill a drive on
    its own. Age is the completion marker's mtime, which cache hits refresh,
    so what goes is what has not been watched for longest. Renders still
    being written (*keep*: the keys of running jobs) are left alone, as is
    anything the OS will not let go of because the player has it open; that
    one goes next time. Returns bytes freed.
    """
    try:
        names = os.listdir(CACHE_DIR)
    except OSError:
        return 0

    entries: dict[str, dict] = {}
    for name in names:
        key, ext = os.path.splitext(name)
        if ext not in (".mkv", ".done"):
            continue
        # <key>.stage1.mkv and <key>.from12000.mkv belong to <key>.
        key, _, rest = key.partition(".")
        try:
            st = os.stat(os.path.join(CACHE_DIR, name))
        except OSError:
            continue
        entry = entries.setdefault(
            key, {"size": 0, "age": 0.0, "complete": False, "partials": []}
        )
        entry["size"] += st.st_size
        if ext == ".done":
            entry["complete"] = True
            entry["age"] = st.st_mtime
        elif rest.startswith("from"):
            entry["partials"].append(name)

    total = sum(e["size"] for e in entries.values())
    freed = 0

    def remove_files(paths) -> int:
        got = 0
        for path in paths:
            try:
                size = os.path.getsize(path)
                os.remove(path)
                got += size
            except OSError:
                pass
        return got

    def remove(key: str) -> int:
        return remove_files(
            os.path.join(CACHE_DIR, n) for n in names if n.partition(".")[0] == key
        )

    # Leftovers from renders that never finished have no use at any size,
    # and a render made for one seek has none once that sitting is over.
    for key, entry in list(entries.items()):
        if key in keep:
            continue
        if not entry["complete"]:
            got = remove(key)
            freed += got
            total -= got
            del entries[key]
        elif entry["partials"]:
            got = remove_files(os.path.join(CACHE_DIR, n) for n in entry["partials"])
            freed += got
            total -= got
            entry["size"] -= got

    victims = sorted(
        (k for k, e in entries.items() if e["complete"] and k not in keep),
        key=lambda k: entries[k]["age"],
    )
    for key in victims:
        if total <= limit_bytes:
            break
        got = remove(key)
        freed += got
        total -= got
    if freed:
        logger.info(
            "video2x: cache trimmed by %.1f MB to %.1f MB", freed / 2**20, total / 2**20
        )
    return freed


# --- How fast this machine renders ---------------------------------------
#
# The GPU cost of a pass is linear in the input's megapixels, so one number
# per kind of pass - seconds per output frame per input megapixel - says
# what any resolution will do. It is measured from the steady part of every
# render and kept across runs, and a live render uses it to pick a decode
# size that keeps up with playback. The first live render on a machine has
# nothing to go on and runs at full size, pausing if it must.

_speed_lock = threading.Lock()
_speed: dict[str, float] | None = None


def _load_speed() -> dict[str, float]:
    global _speed
    if _speed is None:
        try:
            with open(SPEED_FILE, encoding="utf-8") as f:
                data = json.load(f)
            _speed = {k: float(v) for k, v in data.items() if float(v) > 0}
        except (OSError, ValueError, AttributeError):
            _speed = {}
    return _speed


def seconds_per_frame_megapixel(kind: str) -> float | None:
    with _speed_lock:
        return _load_speed().get(kind)


def record_speed(kind: str, seconds_per_frame_mp: float) -> None:
    """Fold one render's measurement into the record for *kind*."""
    if not seconds_per_frame_mp > 0:
        return
    with _speed_lock:
        speed = _load_speed()
        old = speed.get(kind)
        speed[kind] = seconds_per_frame_mp if old is None else (old + seconds_per_frame_mp) / 2
        try:
            os.makedirs(CONFIG_DIR, exist_ok=True)
            with open(SPEED_FILE, "w", encoding="utf-8") as f:
                json.dump(speed, f)
        except OSError:
            logger.exception("Could not save the video2x speed record")
    logger.info(
        "video2x: %s runs at %.4f s per frame per megapixel on this machine", kind, speed[kind]
    )


def estimated_out_fps(kind: str, width: int, height: int) -> float | None:
    """What a pass of *kind* should manage on input of this size, or None
    before that kind has ever been timed here."""
    k = seconds_per_frame_megapixel(kind)
    if k is None:
        return None
    return 1.0 / (k * width * height / 1e6)


# --- The render ------------------------------------------------------------


@dataclass
class Progress:
    phase: str = "starting"  # starting | engine | render | done | failed | cancelled
    stage: int = 0  # index into the recipe's stages
    stages: int = 1
    frames_done: int = 0
    frames_total: int = 0
    out_fps: float = 0.0
    infer_ms: float = 0.0
    error: str = ""

    @property
    def stage_fraction(self) -> float:
        if not self.frames_total:
            return 0.0
        return min(1.0, self.frames_done / self.frames_total)

    @property
    def fraction(self) -> float:
        """Across the whole recipe, each pass weighted equally."""
        return (self.stage + self.stage_fraction) / max(1, self.stages)


class RenderJob(threading.Thread):
    """One render of one file, in the background.

    Progress is published through :attr:`progress` under :attr:`cond`, and
    ``cond`` is notified on every change, so a waiter can sleep on it.
    """

    def __init__(
        self,
        install: Install,
        source: Source,
        recipe: Recipe,
        stream: bool,
        offset: float = 0.0,
        decode: tuple[int, int] | None = None,
        quality: str = QUALITY_BALANCED,
        live_chain: bool = False,
    ):
        """*offset*: begin this many seconds into the source; such a render
        is for one sitting and is never cached. *decode*: the size to decode
        the source at, when a live render has to be smaller to keep up.
        *quality*: the encoder effort. *live_chain*: run the two passes of a
        chain at once, the upscaler following the interpolator's output, so
        a live chain can start playing without waiting out the first pass."""
        super().__init__(name="video2x-render", daemon=True)
        self.install = install
        self.source = source
        self.recipe = recipe
        self.stages = recipe.stages
        self.stream = stream
        self.offset = max(0.0, offset)
        self.decode = decode
        self.quality = quality
        self.live_chain = bool(live_chain and stream and len(self.stages) == 2)
        self.key = cache_key(source, recipe, decode[1] if decode else 0, quality)
        if self.offset:
            self.stem = partial_stem(self.key, self.offset)
            self.output = os.path.join(CACHE_DIR, self.stem + ".mkv")
            self.marker = None
        else:
            self.stem = self.key
            self.output, self.marker = cache_paths(self.key)
        self.progress = Progress(stages=len(self.stages))
        self.cond = threading.Condition()
        self._tree: ProcessTree | None = None
        self._aux_tree: ProcessTree | None = None  # the interpolator, in a live chain
        self._cancelled = False
        self._rate_anchor: tuple[float, int] | None = None
        self._rate_last: tuple[float, int] | None = None
        self.on_done = None  # called on this thread once the job is over

        # The final output's frame rate, to turn a frame count into seconds
        # for the live follower.
        self.out_fps = source.fps * recipe.interp

    def frame_size(self, stage: Stage) -> tuple[int, int]:
        """The size of the frames *stage* works on.

        Only the upscaler decodes smaller: video2x's interpolation stalls
        with --max-output-height as of this writing, so in a chain the
        interpolation runs at the source's size and the upscaler scales
        its output down as it reads it.
        """
        if self.decode and stage.kind == STAGE_UPSCALE:
            return self.decode
        return self.source.width, self.source.height

    def stage_input_fps(self, index: int) -> float:
        """The frame rate going into pass *index*: the source's, or the
        interpolated one if a pass before it multiplied that."""
        return self.source.fps * (self.recipe.interp if index > 0 else 1)

    def speed(self) -> float:
        """How fast the running pass is going relative to realtime, from the
        progress so far; 0 before anything is known."""
        with self.cond:
            p = self.progress
            if p.phase != "render" or not p.out_fps:
                return 0.0
            stage = self.stages[min(p.stage, len(self.stages) - 1)]
            want = stage.out_fps(self.stage_input_fps(p.stage))
            return p.out_fps / want if want else 0.0

    # -- state --------------------------------------------------------------

    @property
    def finished(self) -> bool:
        return self.progress.phase in ("done", "failed", "cancelled")

    @property
    def ok(self) -> bool:
        return self.progress.phase == "done"

    def rendered_seconds(self) -> float:
        """How much of the final output exists, in seconds from its start.

        Zero while an earlier pass of a chain is still running: nothing of
        the file that will be played exists yet.
        """
        if not self.out_fps:
            return 0.0
        with self.cond:
            if self.progress.stage != len(self.stages) - 1:
                return 0.0
            return self.progress.frames_done / self.out_fps

    def frontier(self) -> float:
        """Where in the source the final output reaches, in source time."""
        return self.offset + self.rendered_seconds()

    @property
    def streaming(self) -> bool:
        """Whether the pass now running is the one the player will read."""
        with self.cond:
            return self.progress.phase == "render" and self.progress.stage == len(self.stages) - 1

    def _set(self, **changes) -> None:
        with self.cond:
            for k, v in changes.items():
                setattr(self.progress, k, v)
            self.cond.notify_all()

    def wait(self, predicate, timeout: float | None = None) -> bool:
        """Block until ``predicate(progress)`` holds or the job ends."""
        with self.cond:
            return self.cond.wait_for(
                lambda: self.finished or predicate(self.progress), timeout
            )

    def cancel(self) -> None:
        with self.cond:
            self._cancelled = True
            trees = [t for t in (self._tree, self._aux_tree) if t is not None]
            # Waiters should not have to wait for the tree to die and the
            # pipe to drain before they learn this is over.
            self.progress.phase = "cancelled"
            self.cond.notify_all()
        for tree in trees:
            tree.kill()

    # -- work ---------------------------------------------------------------

    def run(self) -> None:
        try:
            self._run()
        except Exception as error:
            logger.exception("video2x render failed")
            self._set(phase="failed", error=str(error))
        finally:
            if not self.finished:
                self._set(phase="failed", error=_("the render ended without a result"))
            if self.on_done is not None:
                try:
                    self.on_done()
                except Exception:
                    logger.exception("video2x on_done failed")

    def _run(self) -> None:
        os.makedirs(CACHE_DIR, exist_ok=True)
        src = self.source

        for stage in self.stages:
            if not self._ensure_engine(stage, *self.frame_size(stage)):
                return

        if self.live_chain:
            self._run_live_chain()
            return

        # Anything left over from a run that did not finish.
        for path in (self.output, self.marker):
            if path:
                try:
                    os.remove(path)
                except OSError:
                    pass

        last = len(self.stages) - 1
        inputs = src.path
        frames = src.nb_frames
        if self.offset and src.duration:
            frames = max(1, int(round((src.duration - self.offset) * src.fps)))
        intermediates = []
        for index, stage in enumerate(self.stages):
            is_last = index == last
            output = self.output if is_last else stage_path(self.stem, index)
            if not is_last:
                intermediates.append(output)
            self._set(
                phase="render",
                stage=index,
                frames_done=0,
                frames_total=stage.out_frames(frames),
                out_fps=0.0,
            )
            self._rate_anchor = self._rate_last = None
            width, height = self.frame_size(stage)
            max_height = 0
            if self.decode and stage.kind == STAGE_UPSCALE:
                max_height = height * stage.factor
            ok = self._run_child(
                self.install.render_command(
                    stage,
                    inputs,
                    output,
                    self.stream and is_last,
                    offset=self.offset if index == 0 else 0.0,
                    max_height=max_height,
                    quality=self.quality,
                ),
                self._on_progress_line,
            )
            self._record_speed(stage, width, height)
            if not ok or not os.path.isfile(output) or os.path.getsize(output) == 0:
                # Half a file is no use to anyone. The final output may
                # still be open in the player after a live session, in
                # which case the next render overwrites it instead.
                for path in intermediates + [self.output]:
                    try:
                        os.remove(path)
                    except OSError:
                        pass
                if ok:
                    self._set(phase="failed", error=_("The render produced no output."))
                return
            inputs = output
            frames = stage.out_frames(frames)

        for path in intermediates:
            try:
                os.remove(path)
            except OSError:
                pass

        if self.marker:
            with open(self.marker, "w", encoding="utf-8") as marker:
                json.dump(
                    {"source": src.path, "recipe": self.recipe.key, "decode": self.decode},
                    marker,
                )
        self._set(phase="done")

    def _run_live_chain(self) -> None:
        """Interpolate and upscale at the same time.

        The interpolator writes a video-only file that the upscaler follows
        as it grows (--follow), so playback of the upscaled output can begin
        without the whole interpolation pass finishing first. Sound comes
        from the original, not the partial intermediate. The player watches
        the upscaler, which is the last stage and the slower one.
        """
        src = self.source
        interp, upscale = self.stages  # a chain is interpolate then upscale
        intermediate = stage_path(self.stem, 0)
        for path in (self.output, self.marker, intermediate):
            if path:
                try:
                    os.remove(path)
                except OSError:
                    pass

        src_frames = src.nb_frames
        if self.offset and src.duration:
            src_frames = max(1, int(round((src.duration - self.offset) * src.fps)))
        interp_frames = interp.out_frames(src_frames)
        final_frames = upscale.out_frames(interp_frames)

        up_w, up_h = self.frame_size(upscale)
        max_height = up_h * upscale.factor if self.decode else 0

        cmd_interp = self.install.render_command(
            interp, src.path, intermediate, stream=True,
            offset=self.offset, quality=self.quality, no_audio=True,
        )
        cmd_upscale = self.install.render_command(
            upscale, intermediate, self.output, stream=True,
            max_height=max_height, quality=self.quality,
            follow=True, expect_frames=interp_frames,
            audio_from=src.path, audio_start=self.offset,
        )

        # The player follows the last stage; report it as that from the off.
        self._set(phase="render", stage=1, frames_done=0,
                  frames_total=final_frames, out_fps=0.0)
        self._rate_anchor = self._rate_last = None

        with self.cond:
            if self._cancelled:
                self._set(phase="cancelled")
                return
            try:
                self._aux_tree = ProcessTree(
                    cmd_interp, cwd=self.install.root, env=self.install.child_env(),
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    creationflags=SUBPROCESS_FLAGS,
                )
            except OSError as error:
                self._set(phase="failed", error=str(error))
                return
        logger.info("video2x: live chain, interpolating into %s", os.path.basename(intermediate))

        # The upscaler cannot open the intermediate until a cluster of it
        # exists; wait for the file to appear with something in it.
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if self._cancelled or self._aux_tree.proc.poll() is not None:
                break
            try:
                if os.path.getsize(intermediate) > 65536:
                    break
            except OSError:
                pass
            time.sleep(0.1)

        ok = self._run_child(cmd_upscale, self._on_progress_line)

        with self.cond:
            aux, self._aux_tree = self._aux_tree, None
        if aux is not None:
            aux.kill()
            aux.close()
        # Deliberately no _record_speed here: in a chain the upscaler spends
        # most of its time waiting on the interpolator, so what it managed
        # says nothing about what the GPU can do, and recording it would
        # make every later live render decode far smaller than it needs to.

        if self._cancelled:
            self._set(phase="cancelled")
            return
        if not ok or not os.path.isfile(self.output) or os.path.getsize(self.output) == 0:
            for path in (intermediate, self.output):
                try:
                    os.remove(path)
                except OSError:
                    pass
            if ok:
                self._set(phase="failed", error=_("The render produced no output."))
            return
        try:
            os.remove(intermediate)
        except OSError:
            pass
        if self.marker:
            with open(self.marker, "w", encoding="utf-8") as marker:
                json.dump(
                    {"source": src.path, "recipe": self.recipe.key, "decode": self.decode},
                    marker,
                )
        self._set(phase="done")

    def _record_speed(self, stage: Stage, width: int, height: int) -> None:
        """Time the steady part of the pass that just ran, if there was
        enough of it to mean anything."""
        if not self._rate_anchor or not self._rate_last:
            return
        (t0, f0), (t1, f1) = self._rate_anchor, self._rate_last
        if t1 - t0 < 2.0 or f1 - f0 < 30:
            return
        fps = (f1 - f0) / (t1 - t0)
        record_speed(stage.kind, (1.0 / fps) / (width * height / 1e6))

    def _ensure_engine(self, stage: Stage, width: int, height: int) -> bool:
        if self.install.has_engine(stage, width, height):
            return True
        if self.install.can_autobuild:
            # The render command builds it as it starts, in well under a
            # second. Nothing to do here, and no phase worth showing.
            logger.info("no %s engine for %dx%d; video2x will build one", stage.kind, width, height)
            return True
        if self.install.can_export:
            self._set(phase="engine")
            if not self._run_child(self.install.export_command(stage, width, height), None):
                return False
            if not self.install.has_engine(stage, width, height):
                self._set(phase="failed", error=_("Building the engine produced no file."))
                return False
            return True
        self._set(
            phase="failed",
            error=_("No engine for %dx%d, and this video2x install cannot build one.")
            % (width, height),
        )
        return False

    def _run_child(self, cmd: list[str], on_line) -> bool:
        logger.info("video2x: running %s", " ".join(cmd))
        with self.cond:
            if self._cancelled:
                self._set(phase="cancelled")
                return False
            try:
                self._tree = ProcessTree(
                    cmd,
                    cwd=self.install.root,
                    env=self.install.child_env(),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    creationflags=SUBPROCESS_FLAGS,
                )
            except OSError as error:
                self._set(phase="failed", error=str(error))
                return False
        proc = self._tree.proc
        assert proc.stdout is not None

        tail: list[str] = []
        buf = b""
        while True:
            chunk = proc.stdout.read(1)
            if not chunk:
                break
            if chunk in (b"\r", b"\n"):
                line = buf.decode("utf-8", "replace").strip()
                buf = b""
                if not line:
                    continue
                tail.append(line)
                del tail[:-8]
                if on_line:
                    on_line(line)
            else:
                buf += chunk
        code = proc.wait()

        with self.cond:
            cancelled = self._cancelled
            tree, self._tree = self._tree, None
        if tree is not None:
            tree.close()
        if cancelled:
            self._set(phase="cancelled")
            return False
        if code != 0:
            detail = next(
                (l for l in reversed(tail) if not _PROGRESS.search(l)), ""
            )
            logger.error("video2x exited %d: %s", code, " | ".join(tail))
            self._set(phase="failed", error=detail or _("video2x exited with code %d") % code)
            return False
        return True

    def _on_progress_line(self, line: str) -> None:
        m = _PROGRESS.search(line)
        if not m:
            return
        done, total = int(m.group(2)), int(m.group(3)) or self.progress.frames_total
        self._set(
            frames_done=done,
            frames_total=total,
            out_fps=float(m.group(4)),
            infer_ms=float(m.group(5)),
        )
        # For the speed record: the pass is timed from its first sixth on,
        # past DirectML's shader compilation and the pipeline filling up.
        now = time.monotonic()
        if self._rate_anchor is None:
            if total and done >= max(20, total // 6):
                self._rate_anchor = (now, done)
        else:
            self._rate_last = (now, done)


# --- The controller --------------------------------------------------------


class Video2X:
    """Everything the player needs to know about video2x, per window.

    Owns the current settings, answers the on_load hook, runs renders, shows
    progress, and follows a live render so playback never runs past it.
    """

    def __init__(self, window, settings):
        self._win = window
        self._mpv = window.mpv
        self._settings = settings
        self._toast: Adw.Toast | None = None
        self._toast_job: RenderJob | None = None

        # One job per cache key, so a file re-opened during its own render
        # attaches to the render already going instead of starting another.
        self._jobs: dict[str, RenderJob] = {}
        self._jobs_lock = threading.Lock()
        # Renders a deferred hook is still waiting on. mpv sends end-file
        # for a load it gives up on, and that is the moment to stop them.
        self._awaited: set[RenderJob] = set()

        # The live follower: what is playing, how far it has been rendered.
        self._live: RenderJob | None = None
        self._live_pos = 0.0
        self._live_paused_by_us = False
        self._live_poll_id = 0
        self._live_last_osd = 0.0
        self._live_restarted_at = 0.0

        # The tallest screen, in pixels: a live render decoded smaller to
        # keep up is never made smaller than what the screen can show.
        self._screen_height = 0
        try:
            display = Gdk.Display.get_default()
            monitors = display.get_monitors() if display else None
            if monitors is not None:
                monitors.connect("items-changed", lambda *a: self._measure_screens())
            self._measure_screens()
        except Exception:
            logger.exception("Could not measure the screens")

        self._load_script()
        self._publish_mode()

        settings.connect("changed::video2x-upscale", lambda *a: self._publish_mode())
        settings.connect("changed::video2x-interp", lambda *a: self._publish_mode())
        settings.connect("changed::video2x-path", lambda *a: forget_install())
        settings.connect("changed::video2x-cache-limit", lambda *a: self._trim_later())
        # The limit may have been lowered, or a render left half-written,
        # since the app last ran.
        self._trim_later()

        @self._mpv.event_callback("client-message")
        def on_client_message(event):
            args = [
                a.decode("utf-8", "replace") if isinstance(a, bytes) else str(a)
                for a in (event.as_dict().get("args") or [])
            ]
            if len(args) >= 3 and args[0] == "video2x-want":
                start = parse_start(args[3]) if len(args) > 3 else 0.0
                threading.Thread(
                    target=self._decide, args=(args[1], args[2], start),
                    name="video2x-decide", daemon=True,
                ).start()

        @self._mpv.event_callback("end-file")
        def on_end_file(event):
            with self._jobs_lock:
                abandoned = list(self._awaited)
            for job in abandoned:
                job.cancel()
            idle_add_once(self._stop_following)

        @self._mpv.property_observer("time-pos")
        def on_time_pos(_name, value):
            if self._live is not None and value is not None:
                idle_add_once(self._follow, float(value))

        @self._mpv.property_observer("eof-reached")
        def on_eof(_name, value):
            if self._live is not None and value:
                idle_add_once(self._recover_from_eof)

    # -- settings -----------------------------------------------------------

    @property
    def upscale(self) -> str:
        value = self._settings.get_string("video2x-upscale")
        return value if value in UPSCALE_INDEX_MAP else UPSCALE_OFF

    @property
    def interp(self) -> str:
        value = self._settings.get_string("video2x-interp")
        return value if value in INTERP_INDEX_MAP else INTERP_OFF

    @property
    def recipe(self) -> Recipe:
        return Recipe(interp=INTERP_FACTOR[self.interp], upscale=UPSCALE_FACTOR[self.upscale])

    @property
    def render(self) -> str:
        render = self._settings.get_string("video2x-render")
        return render if render in RENDER_INDEX_MAP else RENDER_PRE

    @property
    def quality(self) -> str:
        quality = self._settings.get_string("video2x-quality")
        return quality if quality in QUALITY_INDEX_MAP else QUALITY_BALANCED

    @property
    def max_height(self) -> int:
        """The user's output-height cap in lines, or 0 for none."""
        return max(0, self._settings.get_int("video2x-max-height"))

    @property
    def save_beside(self) -> bool:
        return self._settings.get_boolean("video2x-save-beside")

    @property
    def cache_limit(self) -> int:
        """In bytes; the setting is in whole GB."""
        return max(1, self._settings.get_int("video2x-cache-limit")) * 1024**3

    def _measure_screens(self) -> None:
        display = Gdk.Display.get_default()
        if display is None:
            return
        tallest = 0
        monitors = display.get_monitors()
        for i in range(monitors.get_n_items()):
            monitor = monitors.get_item(i)
            geometry = monitor.get_geometry()
            scale = monitor.get_scale() if hasattr(monitor, "get_scale") else monitor.get_scale_factor()
            tallest = max(tallest, int(round(geometry.height * scale)))
        self._screen_height = tallest

    def _keys_in_use(self) -> set[str]:
        """Cache keys the trimmer must not touch: renders still being
        written, and the one being played."""
        with self._jobs_lock:
            keys = {j.key for j in self._jobs.values() if not j.finished}
        live = self._live
        if live is not None:
            keys.add(live.key)
        return keys

    def trim_cache(self) -> None:
        """Enforce the size limit; safe to call from any thread."""
        try:
            trim_cache(self.cache_limit, self._keys_in_use())
        except Exception:
            logger.exception("Trimming the video2x cache failed")

    def _trim_later(self) -> None:
        threading.Thread(target=self.trim_cache, name="video2x-trim", daemon=True).start()

    def set_upscale(self, value: str) -> None:
        if value not in UPSCALE_INDEX_MAP or value == self.upscale:
            return
        self._settings.set_string("video2x-upscale", value)
        self._reload_current()

    def set_interp(self, value: str) -> None:
        if value not in INTERP_INDEX_MAP or value == self.interp:
            return
        self._settings.set_string("video2x-interp", value)
        self._reload_current()

    def set_render(self, render: str) -> None:
        if render not in RENDER_INDEX_MAP:
            return
        self._settings.set_string("video2x-render", render)

    def _publish_mode(self) -> None:
        # Read by the hook before it does anything, so that "off" is free.
        # python-mpv's item access prefixes "options/", which user-data is
        # not under, so this goes through the set command instead.
        try:
            self._mpv.command("set", "user-data/video2x/mode", self.recipe.key)
        except Exception:
            logger.exception("Could not publish the video2x mode to mpv")

    def _reload_current(self, pos: float | None = None) -> None:
        """Play the current file again through the hook, from *pos* - by
        default from where it is now."""
        try:
            if self._mpv.idle_active:
                return
            if pos is None:
                pos = self._mpv.time_pos
            index = self._mpv.playlist_pos
            if index is None or index < 0:
                return
            options = f"start={pos:.3f}" if pos else ""
            # Re-running the current entry makes mpv open it afresh, hook
            # included, and the entry itself stays what it was.
            self._mpv.command(
                "loadfile", self._mpv.playlist[index]["filename"], "replace", -1, options
            )
        except Exception:
            logger.exception("Could not reopen the current file")

    def _load_script(self) -> None:
        pkgdatadir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        script = os.path.join(pkgdatadir, "video2x.lua")
        if not os.path.isfile(script):
            logger.warning("video2x.lua is missing from %s", pkgdatadir)
            return
        try:
            self._mpv.command("load-script", script)
        except Exception:
            logger.exception("Could not load video2x.lua")

    # -- the hook's question -------------------------------------------------

    def _answer(self, request_id: str, target: str) -> None:
        try:
            self._mpv.command("script-message-to", "video2x", "video2x-open", request_id, target)
        except Exception:
            # mpv is shutting down, or the load was abandoned; nothing to do.
            logger.debug("Could not answer video2x hook %s", request_id, exc_info=True)

    def _decide(self, request_id: str, path: str, start: float = 0.0) -> None:
        """Worker thread: what should mpv open for *path*?"""
        target = ""
        started = time.monotonic()
        try:
            target = self._decide_target(path, start) or ""
        except Exception:
            logger.exception("video2x decision failed for %r", path)
        finally:
            logger.info(
                "video2x: %s -> %s after %.1f s",
                os.path.basename(path), target or "(original)", time.monotonic() - started,
            )
            self._answer(request_id, target)

    def _decide_target(self, path: str, start: float = 0.0) -> str | None:
        recipe = self.recipe
        if not recipe.active or not path or not os.path.isfile(path):
            return None

        install = locate(self._settings.get_string("video2x-path"))
        if not install.usable:
            logger.warning("video2x: %s (root %s)", install.problem, install.root)
            idle_add_once(self._win.show_toast, install.problem)
            return None

        source = probe_source(path, install.ffmpeg_dir)
        if source is None or source.nb_frames <= 1 or not source.fps:
            # Audio, an image, or something ffprobe cannot read.
            logger.info("video2x: not a video, leaving alone: %r", source)
            return None

        live = self.render == RENDER_LIVE
        quality = self.quality
        # The height cap applies to both timings; the live realtime fit only
        # to live. The render is decoded at whichever is smaller.
        decode = _smaller_decode(
            self._fit_decode(source, recipe) if live else None,
            self._cap_decode(source, recipe),
        )

        # A complete render serves any session that would have settled for
        # its size: the full-size one serves everyone, one decoded smaller a
        # live session that planned on that size or a little more.
        for decode_height in self._acceptable_heights(source, decode):
            key = cache_key(source, recipe, decode_height, quality)
            output, marker = cache_paths(key)
            if os.path.isfile(marker) and os.path.isfile(output):
                logger.info(
                    "video2x: cache hit for %s (%s%s)",
                    os.path.basename(path), recipe.key, f" at {decode_height}p" if decode_height else "",
                )
                touch_cache_entry(key)
                return output

        # Where to render from. From the start, so that the result can be
        # kept - unless this is a live session beginning far enough in that
        # rendering everything before it would take too long.
        offset = 0.0
        if live and start > 0:
            wait = start / (self._estimated_speed(source, recipe, decode) or 1.0)
            if wait > LIVE_RESTART_WAIT_SECONDS:
                offset = max(0.0, start - LIVE_BACKOFF_SECONDS)

        # Make room before, and hold the line after: the new render counts
        # against the limit as soon as it is complete.
        self.trim_cache()
        job = self._start_or_join(install, source, recipe, offset, decode, quality, live)
        job.on_done = lambda: self._on_job_done(job)
        idle_add_once(self._show_progress, job)

        with self._jobs_lock:
            self._awaited.add(job)
        try:
            if live:
                # In a chain this includes the whole of every pass before
                # the last: only the last one writes the file to be played.
                # The lead is measured from where playback will begin, which
                # may be past where the render did - a resume, or the file
                # re-opened after a setting changed.
                lead = (start - job.offset) + min(
                    LIVE_LEAD_SECONDS, max(1.0, source.duration * 0.25)
                )
                job.wait(lambda p: job.streaming and job.rendered_seconds() >= lead)
            else:
                job.wait(lambda p: False)  # until it ends, one way or another
        finally:
            with self._jobs_lock:
                self._awaited.discard(job)

        if job.ok and not job.offset:
            return job.output
        if job.finished and not job.ok:
            self._report_failure(job)
            return None
        # Rendering still, with enough of a lead to play behind it - or a
        # render from an offset, complete, that still needs the original in
        # front of it.
        idle_add_once(self._start_following, job)
        return self._live_target(job)

    @staticmethod
    def _live_target(job: RenderJob) -> str:
        """What mpv opens to play a live render: an EDL.

        mpv's own idea of a growing file's length is however much has been
        written, so the seek bar would end at the render's frontier. The
        EDL declares the film's real length, and when the render begins
        part-way in it puts the original before it, so seeking anywhere
        works: back into the original, or ahead into the render - where a
        seek past the frontier simply waits for the frames to arrive.
        Everything mpv reports about the file still refers to the original.
        """

        def segment(path: str, start: float, length: float) -> str:
            # %N% quotes a path of N bytes, however many commas it holds.
            return "%%%d%%%s,%.3f,%.3f" % (len(path.encode("utf-8")), path, start, length)

        if not job.source.duration > job.offset:
            return job.output  # no length to declare; the plain file will do
        parts = ["!no_chapters"]
        if job.offset:
            parts.append(segment(job.source.path, 0.0, job.offset))
        parts.append(segment(job.output, 0.0, job.source.duration - job.offset))
        return "edl://" + ";".join(parts)

    def _cap_decode(self, source: Source, recipe: Recipe) -> tuple[int, int] | None:
        """The decode size that keeps the upscaler's output within the user's
        height cap, or None when there is no cap, the last pass is not an
        upscale (only it can be capped), or the output already fits."""
        cap = self.max_height
        stage = recipe.stages[-1]
        if not cap or stage.kind != STAGE_UPSCALE:
            return None
        return decode_size(source, cap, stage.factor)

    def _on_job_done(self, job: RenderJob) -> None:
        """On the render thread, once a job is over: keep the cache in check
        and, if asked, drop a copy of a finished full render beside its
        source so it can be kept and used elsewhere."""
        self.trim_cache()
        if job.ok and not job.offset and self.save_beside:
            try:
                dest = self._save_beside(job)
                if dest:
                    idle_add_once(
                        self._win.show_toast,
                        _("Saved next to the original: %s") % os.path.basename(dest),
                    )
            except Exception as error:
                logger.exception("video2x: could not save the render beside the original")
                idle_add_once(
                    self._win.show_toast,
                    _("video2x: could not save beside the original (%s)") % error,
                )

    @staticmethod
    def _save_beside(job: RenderJob) -> str | None:
        src = job.source.path
        folder = os.path.dirname(os.path.abspath(src))
        stem = os.path.splitext(os.path.basename(src))[0]
        suffix = job.recipe.key  # e.g. interp2+up4
        dest = os.path.join(folder, f"{stem} [video2x {suffix}].mkv")
        if os.path.isfile(dest) and os.path.getsize(dest) == os.path.getsize(job.output):
            return None  # already there and the same size; nothing to do
        tmp = dest + ".part"
        shutil.copyfile(job.output, tmp)
        os.replace(tmp, dest)
        logger.info("video2x: saved a copy beside the original at %s", dest)
        return dest

    def _fit_decode(self, source: Source, recipe: Recipe) -> tuple[int, int] | None:
        """The decode size at which a live render of *source* keeps up.

        From the speed record for the last pass - the one the player reads -
        and LIVE_HEADROOM. None when the source fits as it is, or when this
        kind of pass has never been timed here.
        """
        stage = recipe.stages[-1]
        if stage.kind != STAGE_UPSCALE:
            return None  # see RenderJob.frame_size
        k = seconds_per_frame_megapixel(stage.kind)
        if k is None or not source.fps or not source.height:
            return None
        in_fps = source.fps * (recipe.interp if len(recipe.stages) > 1 else 1)
        need_fps = stage.out_fps(in_fps) * LIVE_HEADROOM
        megapixels = 1.0 / (need_fps * k)
        aspect = source.width / source.height
        fits = math.sqrt(megapixels * 1e6 / aspect)
        if fits >= source.height:
            return None
        factor = stage.factor if stage.kind == STAGE_UPSCALE else 1
        # No smaller than the screen needs, than the models make sense at,
        # or than half of what the screen can show of the source: below
        # that the lost detail is plain to see, and pausing is the lesser
        # evil.
        screen = self._screen_height or 1080
        floor = max(
            LIVE_MIN_DECODE_HEIGHT,
            min(screen, source.height * factor) // factor,
            min(screen, source.height) // 2,
        )
        height = int(fits) // DECODE_STEP * DECODE_STEP
        while height < floor:
            height += DECODE_STEP
        if height >= source.height:
            return None
        return decode_size(source, height * factor, factor)

    @staticmethod
    def _acceptable_heights(source: Source, decode: tuple[int, int] | None) -> list[int]:
        """Decode heights whose cached render would do, best first: full
        size (0), then - when the plan is to decode smaller - every step
        from just under the source down to a little below the plan."""
        heights = [0]
        if decode:
            heights.append(decode[1])
            top = (source.height - 1) // DECODE_STEP * DECODE_STEP
            lowest = int(decode[1] * 0.85)
            heights += [h for h in range(top, lowest - 1, -DECODE_STEP) if h != decode[1]]
        return heights

    def _estimated_speed(
        self, source: Source, recipe: Recipe, decode: tuple[int, int] | None
    ) -> float:
        """The last pass's speed relative to realtime, as the record
        predicts; 0 when there is no record."""
        stage = recipe.stages[-1]
        width, height = decode or (source.width, source.height)
        fps = estimated_out_fps(stage.kind, width, height)
        if not fps:
            return 0.0
        in_fps = source.fps * (recipe.interp if len(recipe.stages) > 1 else 1)
        want = stage.out_fps(in_fps)
        return fps / want if want else 0.0

    def _start_or_join(
        self,
        install: Install,
        source: Source,
        recipe: Recipe,
        offset: float,
        decode: tuple[int, int] | None,
        quality: str,
        live: bool,
    ) -> RenderJob:
        key = cache_key(source, recipe, decode[1] if decode else 0, quality)
        stem = partial_stem(key, offset) if offset else key
        with self._jobs_lock:
            job = self._jobs.get(stem)
            if job is None or job.finished:
                job = RenderJob(
                    install, source, recipe,
                    stream=live, offset=offset, decode=decode,
                    quality=quality, live_chain=live and len(recipe.stages) == 2,
                )
                self._jobs[stem] = job
                job.start()
            return job

    def _report_failure(self, job: RenderJob) -> None:
        if job.progress.phase == "cancelled":
            return
        idle_add_once(
            self._win.show_toast,
            _("video2x: %s") % (job.progress.error or _("render failed")),
        )

    def cancel_all(self) -> None:
        with self._jobs_lock:
            jobs = list(self._jobs.values())
        for job in jobs:
            job.cancel()

    # -- progress (main loop) -------------------------------------------------

    def _show_progress(self, job: RenderJob) -> None:
        if self._toast is not None and self._toast_job is job:
            return
        if self._toast is not None:
            self._toast.dismiss()

        toast = Adw.Toast(title=self._progress_text(job), timeout=0)
        toast.set_button_label(_("Cancel"))
        toast.connect("button-clicked", lambda *a: job.cancel())
        toast.connect("dismissed", self._on_toast_dismissed)
        self._toast = toast
        self._toast_job = job
        self._win.toast_overlay.add_toast(toast)
        GLib.timeout_add(500, self._tick_progress, job)

    def _on_toast_dismissed(self, toast) -> None:
        if self._toast is toast:
            self._toast = None
            self._toast_job = None

    def _tick_progress(self, job: RenderJob) -> bool:
        if self._toast is None or self._toast_job is not job:
            return False
        if job.finished:
            logger.info("video2x: render %s (%s)", job.progress.phase, job.progress.error or "ok")
            if job.ok:
                self._toast.dismiss()
            else:
                self._toast.set_title(
                    _("video2x: %s") % (job.progress.error or job.progress.phase)
                )
                self._toast.set_button_label(None)
                self._toast.set_timeout(4)
            return False
        self._toast.set_title(self._progress_text(job))
        return True

    @staticmethod
    def _progress_text(job: RenderJob) -> str:
        p = job.progress
        stage = job.stages[min(p.stage, len(job.stages) - 1)]
        label = stage.label
        if job.decode:
            # Decoded smaller to keep up; say what will come out.
            label = _("%s at %dp") % (
                label, job.decode[1] * (stage.factor if stage.kind == STAGE_UPSCALE else 1),
            )
        if len(job.stages) > 1:
            label = _("%s (%d/%d)") % (label, p.stage + 1, len(job.stages))
        if p.phase == "engine":
            return _("%s: building the engine for %d×%d…") % (
                label, job.source.width, job.source.height,
            )
        if p.phase == "render" and p.frames_total:
            remaining = (p.frames_total - p.frames_done) / p.out_fps if p.out_fps else 0
            # Speed relative to this pass's own output rate. Its input runs
            # at the source rate, or at the interpolated rate if a pass
            # before it multiplied that.
            stage_fps = stage.out_fps(job.stage_input_fps(p.stage))
            speed = p.out_fps / stage_fps if stage_fps else 0
            if remaining >= 3600:
                eta = _("%d h %02d min") % divmod(int(remaining // 60), 60)
            else:
                eta = _("%d:%02d") % divmod(int(remaining), 60)
            return _("%s: %d%% · %.2f× realtime · %s left") % (
                label, int(p.stage_fraction * 100), speed, eta,
            )
        return _("%s: starting…") % label

    # -- live playback (main loop) ---------------------------------------------

    def _start_following(self, job: RenderJob) -> None:
        logger.info(
            "video2x: live playback starts at %.1f, rendered to %.1f", job.offset, job.frontier()
        )
        self._live = job
        self._live_paused_by_us = False
        if self._toast is not None and self._toast_job is job:
            self._toast.dismiss()

    def _stop_following(self) -> None:
        job, self._live = self._live, None
        if job is not None and not job.finished and job.stream:
            # Playback has moved on; the render was only for this session.
            # A pre-render is left to finish because it is cached whole.
            job.cancel()
        if job is not None and job.offset:
            # A render from an offset is for one sitting, and this was it.
            self._trim_later()
        self._live_paused_by_us = False
        if self._live_poll_id:
            GLib.source_remove(self._live_poll_id)
            self._live_poll_id = 0

    def _follow(self, pos: float) -> None:
        """Where playback is, in the film's time - or where a seek has asked
        to go, while mpv waits for those frames to exist."""
        job = self._live
        if job is None:
            return
        self._live_pos = pos
        if job.finished:
            # The file is complete; mpv will read it to the end. The job
            # stays here so that what it wrote is kept until playback ends.
            if job.ok:
                self._resume()
            return

        if pos < job.offset:
            # Before where the render begins: this is the original playing,
            # from the EDL's first segment, and nothing to wait for.
            self._resume()
            return

        ahead = job.frontier() - pos
        if ahead < 0:
            # Past the frontier: a seek. mpv holds it until the frames
            # arrive. If that is a long way off, render from here instead.
            wait = -ahead / (job.speed() or self._estimated_speed(job.source, job.recipe, job.decode) or 1.0)
            if wait > LIVE_RESTART_WAIT_SECONDS and time.monotonic() - self._live_restarted_at > 3.0:
                logger.info(
                    "video2x: seek to %.1f is %.1f s past the frontier, about %.0f s of "
                    "rendering away; rendering from there instead", pos, -ahead, wait,
                )
                self._live_restarted_at = time.monotonic()
                self._reload_current(pos)
                return

        if self._live_paused_by_us:
            if ahead >= LIVE_LEAD_SECONDS:
                logger.info("video2x: render is %.1f s ahead again, resuming at %.1f", ahead, pos)
                self._resume()
            else:
                self._osd(_("Rendering ahead… %d%%") % int(job.progress.fraction * 100))
        elif ahead < LIVE_MARGIN_SECONDS:
            try:
                if not self._mpv.pause:
                    logger.info("video2x: render only %.1f s ahead at %.1f, pausing", ahead, pos)
                    self._mpv.pause = True
                    self._live_paused_by_us = True
                    self._osd(_("Rendering ahead… %d%%") % int(job.progress.fraction * 100))
                    # A paused mpv reports no time-pos changes, so nothing
                    # else would call back here to notice the render has
                    # pulled ahead again.
                    if not self._live_poll_id:
                        self._live_poll_id = GLib.timeout_add(300, self._poll_while_paused)
            except Exception:
                logger.exception("Could not pause for the render")

    def _poll_while_paused(self) -> bool:
        if self._live is None or not self._live_paused_by_us:
            self._live_poll_id = 0
            return False
        self._follow(self._live_pos)
        return True

    def _resume(self) -> None:
        if not self._live_paused_by_us:
            return
        self._live_paused_by_us = False
        try:
            self._mpv.pause = False
            self._mpv.show_text("", 1)
        except Exception:
            logger.exception("Could not resume after the render caught up")

    def _recover_from_eof(self) -> None:
        """mpv saw the end of the file while it was still being written.

        The demuxer does not look again on its own; a seek to the same
        position makes it re-read the file and notice the new data. Then
        wait for the lead to build back up before playing on.
        """
        job = self._live
        if job is None or job.finished:
            return
        try:
            pos = self._mpv.time_pos or 0.0
            logger.info("video2x: hit the end of the growing file at %.1f, re-reading", pos)
            self._mpv.command("seek", pos, "absolute+exact")
            self._mpv.pause = True
            self._live_pos = pos
            self._live_paused_by_us = True
            self._osd(_("Rendering ahead… %d%%") % int(job.progress.fraction * 100))
            if not self._live_poll_id:
                self._live_poll_id = GLib.timeout_add(300, self._poll_while_paused)
        except Exception:
            logger.exception("Could not recover from EOF during a live render")

    def _osd(self, text: str) -> None:
        now = time.monotonic()
        if now - self._live_last_osd < 0.9:
            return
        self._live_last_osd = now
        try:
            self._mpv.show_text(text, 1500)
        except Exception:
            pass

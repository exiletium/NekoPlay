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
  waits for it, the way it would buffer a stream. Seeking ahead of what has
  been rendered waits too, because the render is sequential.

Rendered files are cached under the config directory, keyed by the source
file and the mode, so a file is rendered once. Engines are per-resolution;
one that is missing is built on the spot when the video2x install has
PyTorch, which takes seconds for an upscaler and a minute or two for RIFE.
"""

from __future__ import annotations

import hashlib
import json
import logging
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
gi.require_version("GLib", "2.0")
from gi.repository import Adw, GLib

from .platform_compat import IS_WINDOWS, SUBPROCESS_FLAGS, TRACE, ProcessTree
from .utils import CONFIG_DIR, idle_add_once

logger = logging.getLogger(__name__)
if TRACE:
    # The decisions here are otherwise invisible from outside; the startup
    # trace is the natural place to ask for them.
    logger.setLevel(logging.INFO)

# --- Modes -----------------------------------------------------------------

MODE_OFF = "off"
MODE_UPSCALE = "upscale"
MODE_INTERP2 = "interp2"
MODE_INTERP4 = "interp4"

# Order matches the dropdowns in options.blp and preferences.blp.
MODE_INDEX_MAP: list[str] = [MODE_OFF, MODE_UPSCALE, MODE_INTERP2, MODE_INTERP4]
MODE_TO_INDEX: dict[str, int] = {m: i for i, m in enumerate(MODE_INDEX_MAP)}

RENDER_PRE = "pre"
RENDER_LIVE = "live"
RENDER_INDEX_MAP: list[str] = [RENDER_PRE, RENDER_LIVE]
RENDER_TO_INDEX: dict[str, int] = {r: i for i, r in enumerate(RENDER_INDEX_MAP)}

MODE_LABELS = {
    MODE_OFF: _("Off"),
    MODE_UPSCALE: _("Upscale ×4"),
    MODE_INTERP2: _("Interpolate 2×"),
    MODE_INTERP4: _("Interpolate 4×"),
}

# Live playback opens the file once this much of it exists, and pauses to
# let the render get this far ahead again whenever it catches up.
LIVE_LEAD_SECONDS = 4.0
# mpv reads about a second ahead of the playhead, and the encoder holds a
# few frames back; staying this far from the end of the file keeps the
# demuxer from ever seeing EOF while the render is still going.
LIVE_MARGIN_SECONDS = 1.5

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

    @property
    def usable(self) -> bool:
        return self.problem is None

    @property
    def models_dir(self) -> str:
        return os.path.join(self.root, "models")

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

    def engine_path(self, mode: str, width: int, height: int) -> str:
        if mode == MODE_UPSCALE:
            stem = os.path.splitext(SR_WEIGHTS)[0]
            return os.path.join(self.models_dir, f"{stem}_{width}x{height}_u8.onnx")
        return os.path.join(self.models_dir, f"rife_v4.26_{width}x{height}_u8_fast.onnx")

    def has_engine(self, mode: str, width: int, height: int) -> bool:
        if os.path.isfile(self.engine_path(mode, width, height)):
            return True
        if mode != MODE_UPSCALE:
            # The CLI falls back to the reference engine when there is no
            # fast one for a resolution.
            reference = os.path.join(self.models_dir, f"rife_v4.26_{width}x{height}_u8.onnx")
            return os.path.isfile(reference)
        return False

    def export_command(self, mode: str, width: int, height: int) -> list[str]:
        assert self.python
        if mode == MODE_UPSCALE:
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
            os.path.relpath(self.engine_path(mode, width, height), self.root),
        ]

    def render_command(
        self, mode: str, src: str, dst: str, stream: bool
    ) -> list[str]:
        assert self.python
        cmd = [self.python, "-m", "video2x_opt", "-i", src, "-o", dst]
        if mode == MODE_UPSCALE:
            cmd.append("--upscale")
        else:
            cmd += ["-m", "4" if mode == MODE_INTERP4 else "2"]
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


def _python_has(python: str, module: str) -> bool:
    try:
        return (
            subprocess.run(
                [python, "-c", f"import {module}"],
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
    path: str
    width: int
    height: int
    fps: float
    nb_frames: int
    duration: float
    rotation: int


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

    return Source(
        path=path,
        width=int(video.get("width", 0)),
        height=int(video.get("height", 0)),
        fps=fps,
        nb_frames=nb_frames,
        duration=duration,
        rotation=rotation,
    )


# --- The cache -------------------------------------------------------------


def cache_key(source: Source, mode: str) -> str:
    stat = os.stat(source.path)
    raw = f"{os.path.abspath(source.path)}|{stat.st_size}|{int(stat.st_mtime)}|{mode}"
    return hashlib.sha1(raw.encode("utf-8", "surrogateescape")).hexdigest()[:20]


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
        try:
            st = os.stat(os.path.join(CACHE_DIR, name))
        except OSError:
            continue
        entry = entries.setdefault(key, {"size": 0, "age": 0.0, "complete": False})
        entry["size"] += st.st_size
        if ext == ".done":
            entry["complete"] = True
            entry["age"] = st.st_mtime

    total = sum(e["size"] for e in entries.values())
    freed = 0

    def remove(key: str) -> int:
        got = 0
        for path in cache_paths(key):
            try:
                size = os.path.getsize(path)
                os.remove(path)
                got += size
            except OSError:
                pass
        return got

    # Leftovers from renders that never finished have no use at any size.
    for key, entry in list(entries.items()):
        if not entry["complete"] and key not in keep:
            got = remove(key)
            freed += got
            total -= got
            del entries[key]

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


# --- The render ------------------------------------------------------------


@dataclass
class Progress:
    phase: str = "starting"  # starting | engine | render | done | failed | cancelled
    frames_done: int = 0
    frames_total: int = 0
    out_fps: float = 0.0
    infer_ms: float = 0.0
    error: str = ""

    @property
    def fraction(self) -> float:
        if not self.frames_total:
            return 0.0
        return min(1.0, self.frames_done / self.frames_total)


class RenderJob(threading.Thread):
    """One render of one file, in the background.

    Progress is published through :attr:`progress` under :attr:`cond`, and
    ``cond`` is notified on every change, so a waiter can sleep on it.
    """

    def __init__(self, install: Install, source: Source, mode: str, stream: bool):
        super().__init__(name="video2x-render", daemon=True)
        self.install = install
        self.source = source
        self.mode = mode
        self.stream = stream
        self.key = cache_key(source, mode)
        self.output, self.marker = cache_paths(self.key)
        self.progress = Progress()
        self.cond = threading.Condition()
        self._tree: ProcessTree | None = None
        self._cancelled = False
        self.on_done = None  # called on this thread once the job is over

        multiplier = {MODE_INTERP2: 2, MODE_INTERP4: 4}.get(mode, 1)
        # What the output's frame rate will be, to turn a frame count into
        # seconds for the live follower.
        self.out_fps = source.fps * multiplier

    # -- state --------------------------------------------------------------

    @property
    def finished(self) -> bool:
        return self.progress.phase in ("done", "failed", "cancelled")

    @property
    def ok(self) -> bool:
        return self.progress.phase == "done"

    def rendered_seconds(self) -> float:
        """How far into the file the encoder has got."""
        if not self.out_fps:
            return 0.0
        with self.cond:
            return self.progress.frames_done / self.out_fps

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
            tree = self._tree
            # Waiters should not have to wait for the tree to die and the
            # pipe to drain before they learn this is over.
            self.progress.phase = "cancelled"
            self.cond.notify_all()
        if tree is not None:
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

        if not self.install.has_engine(self.mode, src.width, src.height):
            if not self.install.can_export:
                self._set(
                    phase="failed",
                    error=_("No engine for %dx%d, and this video2x install cannot build one (it needs PyTorch).")
                    % (src.width, src.height),
                )
                return
            self._set(phase="engine")
            if not self._run_child(self.install.export_command(self.mode, src.width, src.height), None):
                return
            if not self.install.has_engine(self.mode, src.width, src.height):
                self._set(phase="failed", error=_("Building the engine produced no file."))
                return

        # Anything left over from a run that did not finish.
        for path in (self.output, self.marker):
            try:
                os.remove(path)
            except OSError:
                pass

        self._set(phase="render", frames_total=self._expected_frames())
        if not self._run_child(
            self.install.render_command(self.mode, src.path, self.output, self.stream),
            self._on_progress_line,
        ):
            # Half a file is no use to anyone. It may still be open in the
            # player after a live session, in which case the next render
            # overwrites it instead.
            try:
                os.remove(self.output)
            except OSError:
                pass
            return

        if not os.path.isfile(self.output) or os.path.getsize(self.output) == 0:
            self._set(phase="failed", error=_("The render produced no output."))
            return

        with open(self.marker, "w", encoding="utf-8") as marker:
            json.dump({"source": src.path, "mode": self.mode}, marker)
        self._set(phase="done")

    def _expected_frames(self) -> int:
        n = self.source.nb_frames
        if self.mode == MODE_INTERP2:
            return max(0, (n - 1) * 2 + 1)
        if self.mode == MODE_INTERP4:
            return max(0, (n - 1) * 4 + 1)
        return n

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
        self._set(
            frames_done=int(m.group(2)),
            frames_total=int(m.group(3)) or self.progress.frames_total,
            out_fps=float(m.group(4)),
            infer_ms=float(m.group(5)),
        )


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

        self._load_script()
        self._publish_mode()

        settings.connect("changed::video2x-mode", lambda *a: self._publish_mode())
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
                threading.Thread(
                    target=self._decide, args=(args[1], args[2]),
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
    def mode(self) -> str:
        mode = self._settings.get_string("video2x-mode")
        return mode if mode in MODE_INDEX_MAP else MODE_OFF

    @property
    def render(self) -> str:
        render = self._settings.get_string("video2x-render")
        return render if render in RENDER_INDEX_MAP else RENDER_PRE

    @property
    def cache_limit(self) -> int:
        """In bytes; the setting is in whole GB."""
        return max(1, self._settings.get_int("video2x-cache-limit")) * 1024**3

    def _running_keys(self) -> set[str]:
        with self._jobs_lock:
            return {k for k, j in self._jobs.items() if not j.finished}

    def trim_cache(self) -> None:
        """Enforce the size limit; safe to call from any thread."""
        try:
            trim_cache(self.cache_limit, self._running_keys())
        except Exception:
            logger.exception("Trimming the video2x cache failed")

    def _trim_later(self) -> None:
        threading.Thread(target=self.trim_cache, name="video2x-trim", daemon=True).start()

    def set_mode(self, mode: str) -> None:
        if mode not in MODE_INDEX_MAP or mode == self.mode:
            return
        self._settings.set_string("video2x-mode", mode)
        self._reopen_current()

    def set_render(self, render: str) -> None:
        if render not in RENDER_INDEX_MAP:
            return
        self._settings.set_string("video2x-render", render)

    def _publish_mode(self) -> None:
        # Read by the hook before it does anything, so that "off" is free.
        # python-mpv's item access prefixes "options/", which user-data is
        # not under, so this goes through the set command instead.
        try:
            self._mpv.command("set", "user-data/video2x/mode", self.mode)
        except Exception:
            logger.exception("Could not publish the video2x mode to mpv")

    def _reopen_current(self) -> None:
        """Play the current file again through the hook, from where it was."""
        try:
            if self._mpv.idle_active:
                return
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

    def _decide(self, request_id: str, path: str) -> None:
        """Worker thread: what should mpv open for *path*?"""
        target = ""
        started = time.monotonic()
        try:
            target = self._decide_target(path) or ""
        except Exception:
            logger.exception("video2x decision failed for %r", path)
        finally:
            logger.info(
                "video2x: %s -> %s after %.1f s",
                os.path.basename(path), target or "(original)", time.monotonic() - started,
            )
            self._answer(request_id, target)

    def _decide_target(self, path: str) -> str | None:
        mode = self.mode
        if mode == MODE_OFF or not path or not os.path.isfile(path):
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
        if source.rotation % 360:
            idle_add_once(
                self._win.show_toast,
                _("video2x: rotated videos are not supported, playing the original"),
            )
            return None

        key = cache_key(source, mode)
        output, marker = cache_paths(key)
        if os.path.isfile(marker) and os.path.isfile(output):
            logger.info("video2x: cache hit for %s (%s)", os.path.basename(path), mode)
            touch_cache_entry(key)
            return output

        # Make room before, and hold the line after: the new render counts
        # against the limit as soon as it is complete.
        self.trim_cache()
        job = self._start_or_join(install, source, mode, key)
        job.on_done = self.trim_cache
        idle_add_once(self._show_progress, job)

        with self._jobs_lock:
            self._awaited.add(job)
        try:
            if self.render == RENDER_LIVE:
                lead = min(LIVE_LEAD_SECONDS, max(1.0, source.duration * 0.25))
                job.wait(
                    lambda p: p.phase == "render"
                    and p.frames_done / max(job.out_fps, 1e-9) >= lead
                )
            else:
                job.wait(lambda p: False)  # until it ends, one way or another
        finally:
            with self._jobs_lock:
                self._awaited.discard(job)

        if job.ok:
            return output
        if job.finished:
            self._report_failure(job)
            return None
        # Still rendering, with enough of a lead to start playing behind it.
        idle_add_once(self._start_following, job)
        return output

    def _start_or_join(self, install: Install, source: Source, mode: str, key: str) -> RenderJob:
        with self._jobs_lock:
            job = self._jobs.get(key)
            if job is None or job.finished:
                job = RenderJob(install, source, mode, stream=self.render == RENDER_LIVE)
                self._jobs[key] = job
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
        label = MODE_LABELS.get(job.mode, job.mode)
        if p.phase == "engine":
            return _("%s: building the engine for %d×%d…") % (
                label, job.source.width, job.source.height,
            )
        if p.phase == "render" and p.frames_total:
            remaining = (p.frames_total - p.frames_done) / p.out_fps if p.out_fps else 0
            speed = p.out_fps / job.out_fps if job.out_fps else 0
            if remaining >= 3600:
                eta = _("%d h %02d min") % divmod(int(remaining // 60), 60)
            else:
                eta = _("%d:%02d") % divmod(int(remaining), 60)
            return _("%s: %d%% · %.2f× realtime · %s left") % (
                label, int(p.fraction * 100), speed, eta,
            )
        return _("%s: starting…") % label

    # -- live playback (main loop) ---------------------------------------------

    def _start_following(self, job: RenderJob) -> None:
        logger.info("video2x: live playback starts, %.1f s rendered", job.rendered_seconds())
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
        self._live_paused_by_us = False
        if self._live_poll_id:
            GLib.source_remove(self._live_poll_id)
            self._live_poll_id = 0

    def _follow(self, pos: float) -> None:
        job = self._live
        if job is None:
            return
        self._live_pos = pos
        if job.finished:
            # The file is complete now and mpv will read it to the end.
            if job.ok:
                self._resume()
            self._live = None
            return

        ahead = job.rendered_seconds() - pos
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

# probe.py
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

"""Video dimensions, fetched before anything is waiting for them.

The window is sized from the first file's dimensions before it is shown, so
the first frame does not arrive into a window of the wrong shape. Asking
ffprobe costs about 100 ms, nearly all of it process startup, and it used to
be spent immediately before the window was presented - the one point in
startup where nothing else can make progress.

Nothing about the answer depends on the rest of startup, so :func:`prefetch`
starts it on a worker thread as soon as the process knows its arguments.
Loading GTK then takes the better part of a second, and by the time anything
asks the answer is already sitting here. :func:`video_size` still probes
synchronously on a miss, so behaviour is unchanged either way.
"""

import logging
import os
import threading

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_cache: dict[str, tuple[int, int] | None] = {}


def _run_ffprobe(path: str) -> tuple[int, int] | None:
    """The display size of a file's first video stream, rotation applied."""
    import shutil
    import subprocess

    from .platform_compat import SUBPROCESS_FLAGS

    # It ships with the Flatpak, but on Windows it is merely optional.
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return None

    try:
        output = subprocess.check_output(
            [
                ffprobe,
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=width,height:stream_side_data=rotation",
                "-of",
                "csv=s=x:p=0",
                path,
            ],
            text=True,
            timeout=2,
            stderr=subprocess.DEVNULL,
            creationflags=SUBPROCESS_FLAGS,
        ).strip()
    except Exception:
        logger.exception("Metadata probe failed")
        return None

    if not output:
        return None

    try:
        # "1920x1080x-90", or just "1920x1080" with no rotation side data.
        parts = output.splitlines()[0].split("x")
        width = int(parts[0])
        height = int(parts[1])
    except (IndexError, ValueError):
        logger.exception("Could not read dimensions from %r", output)
        return None

    try:
        rotation = int(parts[2]) if len(parts) > 2 else 0
    except ValueError:
        logger.exception("Failed to get rotation")
        rotation = 0

    if abs(rotation) in (90, 270):
        return height, width
    return width, height


def video_size(path: str) -> tuple[int, int] | None:
    """Ask for a file's display size, using the prefetched answer if there is one.

    Blocks for as long as an ffprobe run takes on a miss, which is what the
    caller used to do unconditionally.
    """
    with _lock:
        if path in _cache:
            return _cache[path]

    size = _run_ffprobe(path)
    with _lock:
        _cache.setdefault(path, size)
        return _cache[path]


def prefetch(paths) -> None:
    """Start probing *paths* in the background and return immediately.

    Called before the GTK stack is loaded, so it must not import anything
    expensive on the calling thread - the worker does its own importing.
    """
    wanted = [p for p in paths if p and not p.startswith("-")]
    if not wanted:
        return

    def work():
        for path in wanted:
            try:
                if os.path.isfile(path):
                    video_size(path)
            except Exception:
                logger.exception("Prefetch failed for %r", path)

    # Daemonised because a second launch that forwards its files to an
    # already running instance exits almost immediately, and should not be
    # held open waiting for an answer nobody will read.
    threading.Thread(target=work, name="probe-prefetch", daemon=True).start()

#!/usr/bin/env python3
# make-ico.py
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Render the app SVG into the multi-resolution .ico that nekoplay.rc embeds.

Run this only when the source icon changes; the result is committed so that
building the launcher does not need a working SVG loader.

    python3 build-aux/windows/make-ico.py

Uses GdkPixbuf, which is already a hard dependency of the app, rather than
pulling in an image library just for this.
"""

import os
import struct
import sys

import gi

gi.require_version("GdkPixbuf", "2.0")
from gi.repository import GdkPixbuf

# Windows picks the closest match itself; 256 is what Explorer's extra-large
# view wants and 16 is what the title bar takes.
SIZES = (16, 24, 32, 48, 64, 128, 256)

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SVG = os.path.join(
    ROOT, "data", "icons", "hicolor", "scalable", "apps", "moe.nyarchlinux.nekoplay.svg"
)
ICO = os.path.join(ROOT, "src", "nekoplay.ico")


def render(size: int) -> bytes:
    pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_scale(SVG, size, size, True)
    ok, data = pixbuf.save_to_bufferv("png", [], [])
    if not ok:
        raise RuntimeError(f"failed to render {size}x{size}")
    return data


def main() -> int:
    if not os.path.isfile(SVG):
        print(f"missing source icon: {SVG}", file=sys.stderr)
        return 1

    images = [render(size) for size in SIZES]

    # ICONDIR, then one 16-byte ICONDIRENTRY per image, then the PNG blobs.
    # Storing PNGs rather than BMPs is understood by Vista and later.
    offset = 6 + 16 * len(images)
    header = struct.pack("<HHH", 0, 1, len(images))
    entries = b""

    for size, data in zip(SIZES, images):
        # 0 is how the format spells 256.
        dim = 0 if size >= 256 else size
        entries += struct.pack(
            "<BBBBHHII", dim, dim, 0, 0, 1, 32, len(data), offset
        )
        offset += len(data)

    with open(ICO, "wb") as f:
        f.write(header + entries + b"".join(images))

    print(f"wrote {ICO} ({os.path.getsize(ICO)} bytes, {len(images)} sizes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env bash
# build.sh
#
# SPDX-License-Identifier: GPL-3.0-or-later
#
# Configure, compile and install NekoPlay into ./_install.
# Run from an MSYS2 UCRT64 shell at the top of the source tree.

set -euo pipefail

SRC_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
BUILD_DIR="${BUILD_DIR:-$SRC_ROOT/_build}"
INSTALL_DIR="${INSTALL_DIR:-$SRC_ROOT/_install}"

cd "$SRC_ROOT"

if [ ! -d "$BUILD_DIR" ]; then
	meson setup "$BUILD_DIR" --prefix="$INSTALL_DIR" --buildtype=release
fi

meson compile -C "$BUILD_DIR"
rm -rf "$INSTALL_DIR"
meson install -C "$BUILD_DIR"

echo
echo "Installed to $INSTALL_DIR"
echo "Next: ./build-aux/windows/bundle.sh [--with-video2x=DIR]"

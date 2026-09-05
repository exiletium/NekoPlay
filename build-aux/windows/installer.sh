#!/usr/bin/env bash
# installer.sh
#
# SPDX-License-Identifier: GPL-3.0-or-later
#
# Wrap the folder built by bundle.sh into NekoPlay-Setup.exe.
# Run from an MSYS2 UCRT64 shell, after build.sh and bundle.sh:
#
#     ./build-aux/windows/installer.sh
#
# Needs mingw-w64-ucrt-x86_64-nsis.

set -euo pipefail

SRC_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DIST="${DIST:-$SRC_ROOT/dist/NekoPlay}"
OUT_DIR="${OUT_DIR:-$SRC_ROOT/dist}"

if [ ! -f "$DIST/nekoplay.exe" ]; then
	echo "no bundle at $DIST - run bundle.sh first" >&2
	exit 1
fi

if ! command -v makensis >/dev/null; then
	echo "makensis not found - pacman -S mingw-w64-ucrt-x86_64-nsis" >&2
	exit 1
fi

# Read the version meson already knows about rather than repeating it.
VERSION="$(sed -n "s/^ *version: *'\([^']*\)'.*/\1/p" "$SRC_ROOT/meson.build" | head -1)"
VERSION="${VERSION:-0.0.0}"
OUT="$OUT_DIR/NekoPlay-$VERSION-Setup.exe"

# NSIS wants Windows paths, and the tree is only reachable by one here.
win_path() { cygpath -w "$1"; }

echo "==> Building installer for $VERSION"
makensis -V2 \
	"-DAPP_VERSION=$VERSION" \
	"-DDIST_DIR=$(win_path "$DIST")" \
	"-DSRC_DIR=$(win_path "$SRC_ROOT")" \
	"-DOUT_FILE=$(win_path "$OUT")" \
	"$SRC_ROOT/build-aux/windows/nekoplay.nsi"

echo "==> $OUT ($(du -h "$OUT" | cut -f1))"

#!/usr/bin/env bash
# bundle.sh
#
# SPDX-License-Identifier: GPL-3.0-or-later
#
# Turn a meson install tree into a self-contained folder that runs on a
# Windows machine with no MSYS2 on it.
#
# Run from an MSYS2 UCRT64 shell, from the top of the source tree:
#
#     ./build-aux/windows/build.sh          # configure, compile, install
#     ./build-aux/windows/bundle.sh         # collect everything into dist/
#
# The result is dist/NekoPlay/, which can be zipped, moved or renamed; the
# launcher works out where it is at startup.

set -euo pipefail

PREFIX="${MINGW_PREFIX:-/ucrt64}"
SRC_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
INSTALL_DIR="${INSTALL_DIR:-$SRC_ROOT/_install}"
DIST="${DIST:-$SRC_ROOT/dist/NekoPlay}"
PYVER="$(python -c 'import sys; print("%d.%d" % sys.version_info[:2])')"

WITH_ANIME4K=0
WITH_VIDEO2X=""
for arg in "$@"; do
	case "$arg" in
	--with-anime4k) WITH_ANIME4K=1 ;;
	# A video2x_optimized portable bundle (the folder holding video2x.bat)
	# to ship inside the app, so AI upscaling works without any setup.
	--with-video2x=*) WITH_VIDEO2X="${arg#--with-video2x=}" ;;
	*)
		echo "unknown option: $arg" >&2
		exit 2
		;;
	esac
done

if [ ! -d "$INSTALL_DIR" ]; then
	echo "no install tree at $INSTALL_DIR - run build.sh first" >&2
	exit 1
fi

say() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }

rm -rf "$DIST"
mkdir -p "$DIST"/{bin,lib,share,etc}

# --- the app itself --------------------------------------------------------

say "Copying the application"
cp "$INSTALL_DIR/bin/nekoplay.exe" "$DIST/nekoplay.exe"
cp -r "$INSTALL_DIR/share/cine" "$DIST/share/"
cp -r "$INSTALL_DIR/share/locale" "$DIST/share/"
mkdir -p "$DIST/share/icons"
cp -r "$INSTALL_DIR/share/icons/hicolor" "$DIST/share/icons/"

# --- native libraries ------------------------------------------------------
#
# Walk the import tables outwards from everything that gets loaded, and take
# whatever resolves inside the MSYS2 prefix. Anything that does not is a
# Windows system DLL and is already on the target machine.

declare -A seen=()

collect() {
	local queue=("$@") current name

	while [ ${#queue[@]} -gt 0 ]; do
		current="${queue[0]}"
		queue=("${queue[@]:1}")

		[ -f "$current" ] || continue

		while read -r name; do
			[ -n "$name" ] || continue
			[ -n "${seen[$name]:-}" ] && continue

			if [ -f "$PREFIX/bin/$name" ]; then
				seen[$name]=1
				cp -n "$PREFIX/bin/$name" "$DIST/bin/"
				queue+=("$PREFIX/bin/$name")
			fi
		done < <(objdump -p "$current" 2>/dev/null |
			sed -n 's/^\s*DLL Name:\s*//p')
	done
}

say "Copying helper executables"
for exe in ffprobe.exe yt-dlp.exe; do
	if [ -f "$PREFIX/bin/$exe" ]; then
		cp "$PREFIX/bin/$exe" "$DIST/bin/"
	else
		echo "  note: $exe not found, skipping" >&2
	fi
done

# Nothing in the bundle imports these: libmpv is opened by name through
# ctypes, and GTK and libadwaita are only ever reached through their
# typelibs, which GObject introspection loads with GModule at run time.
# Walking import tables alone would miss all three.
LAZY_LOADED=(
	libmpv-2.dll
	libgtk-4-1.dll
	libadwaita-1-0.dll
	libgirepository-2.0-0.dll
	librsvg-2-2.dll
)
for dll in "${LAZY_LOADED[@]}"; do
	cp "$PREFIX/bin/$dll" "$DIST/bin/"
	seen[$dll]=1
done

say "Resolving DLL dependencies"
mapfile -t roots < <(
	printf '%s\n' \
		"$DIST/nekoplay.exe" \
		"$DIST"/bin/*.exe \
		"$DIST"/bin/*.dll \
		"$PREFIX/lib/python$PYVER/site-packages/gi"/*.pyd \
		"$PREFIX/lib/python$PYVER/lib-dynload"/*.pyd \
		"$PREFIX/lib/gdk-pixbuf-2.0/2.10.0/loaders"/*.dll
)
collect "${roots[@]}"
say "  ${#seen[@]} libraries collected"

# --- Python ----------------------------------------------------------------

say "Copying the Python runtime"
mkdir -p "$DIST/lib/python$PYVER"
# Everything the interpreter needs, minus the parts only a developer wants.
# GNU tar only honours --exclude when it comes before the operand.
tar -C "$PREFIX/lib" \
	--exclude='__pycache__' \
	--exclude='*.dll.a' \
	--exclude='test' \
	--exclude='tests' \
	--exclude='idlelib' \
	--exclude='tkinter' \
	--exclude='turtledemo' \
	--exclude='ensurepip' \
	--exclude='pydoc_data' \
	--exclude='config-*' \
	--exclude='pip' \
	--exclude='setuptools' \
	--exclude='pkg_resources' \
	-cf - "python$PYVER" |
	tar -C "$DIST/lib" -xf -

# python-mpv is a single module and pip put it in the MSYS2 site-packages.
if [ ! -f "$DIST/lib/python$PYVER/site-packages/mpv.py" ]; then
	cp "$PREFIX/lib/python$PYVER/site-packages/mpv.py" \
		"$DIST/lib/python$PYVER/site-packages/"
fi

# --- GObject introspection, pixbuf loaders, schemas, icons -----------------

say "Copying typelibs"
mkdir -p "$DIST/lib/girepository-1.0"
cp "$PREFIX/lib/girepository-1.0"/*.typelib "$DIST/lib/girepository-1.0/"

say "Copying gdk-pixbuf loaders"
LOADERS="$DIST/lib/gdk-pixbuf-2.0/2.10.0/loaders"
mkdir -p "$LOADERS"
cp "$PREFIX/lib/gdk-pixbuf-2.0/2.10.0/loaders"/*.dll "$LOADERS/"
# The cache records absolute paths, which gdk-pixbuf rewrites at runtime
# against wherever its own DLL turned out to be.
GDK_PIXBUF_MODULEDIR="$LOADERS" gdk-pixbuf-query-loaders \
	>"$DIST/lib/gdk-pixbuf-2.0/2.10.0/loaders.cache"

say "Compiling GSettings schemas"
SCHEMAS="$DIST/share/glib-2.0/schemas"
mkdir -p "$SCHEMAS"
# GTK reads its own settings from these, so ours cannot be the only one.
cp "$PREFIX/share/glib-2.0/schemas"/*.gschema.xml "$SCHEMAS/"
cp "$INSTALL_DIR/share/glib-2.0/schemas"/*.gschema.xml "$SCHEMAS/"
glib-compile-schemas "$SCHEMAS"
rm -f "$SCHEMAS"/*.gschema.xml

say "Copying the Adwaita icon theme"
cp -r "$PREFIX/share/icons/Adwaita" "$DIST/share/icons/"

say "Copying fontconfig configuration"
cp -r "$PREFIX/etc/fonts" "$DIST/etc/"

say "Fetching Adwaita fonts"
# The UI asks for Adwaita Sans by name. Windows has no such font and MSYS2
# packages none, so without this fontconfig substitutes something arbitrary:
# the interface renders in the wrong face and glyphs the substitute lacks,
# the ellipsis in "Open..." among them, come out as tofu.
ADW_URL="https://gitlab.gnome.org/GNOME/adwaita-fonts/-/archive/51.0/adwaita-fonts-51.0.tar.gz"
ADW_SHA="d9d23a83ed9a6b3a28aad520c681effaf22522fbe4a8768482b8bf6dd664bd19"
FONT_DIR="$DIST/share/fonts/adwaita"
mkdir -p "$FONT_DIR"
tmp="$(mktemp -d)"
curl -fsSL "$ADW_URL" -o "$tmp/adwaita-fonts.tar.gz"
echo "$ADW_SHA  $tmp/adwaita-fonts.tar.gz" | sha256sum -c -
tar -C "$tmp" -xzf "$tmp/adwaita-fonts.tar.gz"
find "$tmp" -name '*.ttf' -exec cp {} "$FONT_DIR/" \;
rm -rf "$tmp"
say "  $(ls "$FONT_DIR" | wc -l) font files"

# fonts.conf already pulls in conf.d relative to itself, so a drop-in is
# enough.
cat > "$DIST/etc/fonts/conf.d/99-nekoplay-fonts.conf" <<'FONTCONF'
<?xml version="1.0"?>
<!DOCTYPE fontconfig SYSTEM "urn:fontconfig:fonts.dtd">
<fontconfig>
  <!-- The font files themselves are registered at startup by nekoplay.in,
       via FcConfigAppFontAddDir: APPSHAREFONTDIR resolves one directory too
       high for this layout, and a <dir> cannot be written relative to a
       folder the user may move. -->

  <!-- GTK asks for "Adwaita Sans Text", which is not a real family: it only
       exists as a generic alias in fontconfig's own latin rules and binds to
       whatever sans-serif wins. Point it at the font actually shipped here. -->
  <match target="pattern">
    <test qual="any" name="family"><string>Adwaita Sans Text</string></test>
    <edit name="family" mode="prepend" binding="strong"><string>Adwaita Sans</string></edit>
  </match>
</fontconfig>
FONTCONF

# --- Anime4K ---------------------------------------------------------------

if [ "$WITH_ANIME4K" = 1 ]; then
	say "Fetching Anime4K shaders"
	# Same release and checksum the Flatpak manifest pins.
	A4K_URL="https://github.com/bloc97/Anime4K/releases/download/v4.0.1/Anime4K_v4.0.zip"
	A4K_SHA="139cd282086457c5adc79caf7b75b8b825091d71c9b54958c18745fea62d7ed7"
	tmp="$(mktemp -d)"
	curl -fsSL "$A4K_URL" -o "$tmp/anime4k.zip"
	echo "$A4K_SHA  $tmp/anime4k.zip" | sha256sum -c -
	mkdir -p "$DIST/share/cine/shaders"
	# python rather than unzip, which is not in a default MSYS2 install.
	python -m zipfile -e "$tmp/anime4k.zip" "$tmp/x"
	find "$tmp/x" -name '*.glsl' -exec cp {} "$DIST/share/cine/shaders/" \;
	rm -rf "$tmp"
	say "  $(find "$DIST/share/cine/shaders" -name '*.glsl' | wc -l) shaders"
else
	echo "  skipping Anime4K shaders (pass --with-anime4k to include them)"
fi

# --- video2x_optimized ------------------------------------------------------
#
# Optional, and large (~170 MB plus ~100 MB of engines): the portable bundle
# of video2x_optimized, copied to where video2x.py looks first. Without it
# the feature still works from a folder the user points to in Preferences.

if [ -n "$WITH_VIDEO2X" ]; then
	if [ ! -f "$WITH_VIDEO2X/video2x_opt/cli.py" ]; then
		echo "no video2x_optimized at $WITH_VIDEO2X (expected video2x_opt/cli.py)" >&2
		exit 1
	fi
	say "Copying video2x_optimized"
	mkdir -p "$DIST/share/cine/video2x"
	# Everything the runtime needs; not the docs, the benchmarks or the
	# demo clip, and never the source checkout's build leftovers.
	for item in python lib video2x_opt models bin video2x.bat README.txt requirements.txt; do
		[ -e "$WITH_VIDEO2X/$item" ] && cp -r "$WITH_VIDEO2X/$item" "$DIST/share/cine/video2x/"
	done
	find "$DIST/share/cine/video2x" -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null || true
	if [ ! -x "$DIST/share/cine/video2x/python/python.exe" ]; then
		echo "  note: no bundled interpreter; a system Python with onnxruntime will be used"
	fi
	say "  $(du -sh "$DIST/share/cine/video2x" | cut -f1), $(ls "$DIST/share/cine/video2x/models"/*.onnx 2>/dev/null | wc -l) engines"
else
	echo "  skipping video2x_optimized (pass --with-video2x=DIR to bundle it)"
fi

# --- finish ----------------------------------------------------------------

say "Byte-compiling"
python -m compileall -q -j0 "$DIST/lib/python$PYVER" "$DIST/share/cine" >/dev/null || true

say "Done: $DIST ($(du -sh "$DIST" | cut -f1))"

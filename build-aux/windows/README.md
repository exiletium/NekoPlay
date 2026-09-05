# NekoPlay on Windows

NekoPlay is a GTK4 / libadwaita application driving libmpv. Both stacks are
packaged for Windows by [MSYS2](https://www.msys2.org/), so the port builds
against the UCRT64 environment and ships as a folder containing everything
it needs.

## Building

Install MSYS2, then from a **UCRT64** shell:

```bash
pacman -S --needed \
  mingw-w64-ucrt-x86_64-gtk4 \
  mingw-w64-ucrt-x86_64-libadwaita \
  mingw-w64-ucrt-x86_64-python-gobject \
  mingw-w64-ucrt-x86_64-python-pip \
  mingw-w64-ucrt-x86_64-mpv \
  mingw-w64-ucrt-x86_64-blueprint-compiler \
  mingw-w64-ucrt-x86_64-meson \
  mingw-w64-ucrt-x86_64-ninja \
  mingw-w64-ucrt-x86_64-pkgconf \
  mingw-w64-ucrt-x86_64-gcc \
  mingw-w64-ucrt-x86_64-adwaita-icon-theme \
  mingw-w64-ucrt-x86_64-gettext-tools \
  mingw-w64-ucrt-x86_64-yt-dlp

python -m pip install --break-system-packages python-mpv
```

Then, from the top of the source tree:

```bash
./build-aux/windows/build.sh                    # meson configure/compile/install
./build-aux/windows/bundle.sh --with-anime4k    # collect into dist/NekoPlay
```

`dist/NekoPlay/` is self-contained: about 330 MB, runs on a machine with no
MSYS2 installed, and can be zipped, moved or renamed freely. `nekoplay.exe`
works out where it lives at startup and everything else follows from that.

Drop `--with-anime4k` to skip fetching the shaders; the build then needs no
network. The archive is pinned to the same release and SHA256 as the Flatpak
manifest.

## How the port fits together

Everything that differs between the two platforms lives in
[`src/platform_compat.py`](../../src/platform_compat.py). The rest of the
application never asks which OS it is on.

| Concern | Linux | Windows |
| --- | --- | --- |
| GL entry points | `eglGetProcAddress`, `libGL.so.1` | `wglGetProcAddress`, falling back to `opengl32.dll` for the core GL 1.1 functions it will not resolve, and to `libEGL.dll` under ANGLE |
| Display handle for mpv | `wl_display` / `x11_display` | none — a WGL context is found through the calling thread |
| Idle inhibition | `GtkApplication.inhibit()` over D-Bus | `SetThreadExecutionState` |
| MPRIS | D-Bus session bus | not applicable; the object stays inert |
| Config directory | `~/.config/cine` | `%LOCALAPPDATA%\NekoPlay` |
| Settings storage | dconf | the registry, under `HKCU\Software\GSettings` |
| mpv path lists | `:` separated | `;` separated |
| Subtitle font default | Adwaita Sans SemiBold | Segoe UI Semibold — Adwaita Sans ships with the GNOME runtime and is not packaged for Windows |

Two things have no Windows equivalent and are simply absent: MPRIS, so no
media-key integration or Now Playing panel, and the Flatpak permission
notices in Preferences, which hide themselves off Flatpak already.

## The launcher

`nekoplay.exe` ([`src/nekoplay-launcher.c`](../../src/nekoplay-launcher.c))
is a small C program that points `PATH`, `PYTHONHOME`, `GI_TYPELIB_PATH`,
`GSETTINGS_SCHEMA_DIR`, `XDG_DATA_DIRS`, `GDK_PIXBUF_MODULE_FILE` and
`FONTCONFIG_PATH` at the bundle, then calls `Py_Main`.

It loads `libpython` with `LoadLibrary` rather than linking against it. A
static import would be resolved by the Windows loader before any of the
launcher's own code runs, and the loader only looks beside the executable —
which would force all 168 bundled DLLs to sit in the top folder, next to
the icon the user actually clicks.

It writes the environment with `_wputenv_s`, not `SetEnvironmentVariableW`.
The latter updates only the Win32 environment block, while the C runtime
hands Python a copy taken at startup; python-mpv looks for `libmpv-2.dll`
by walking `os.environ['PATH']` and would not see it otherwise.

## Known issue: Direct Composition and the software renderer

On some machines GSK logs this at startup and falls back to
`GskCairoRenderer`:

```
Failed to realize renderer 'GskGLRenderer' for surface 'GdkWin32Toplevel':
OpenGL requires Direct Composition
```

GTK 4.22 presents both its GL and Vulkan renderers through a Direct
Composition device, and creates that device from a D3D11 device it makes at
display-open time. When that does not come together, the only renderer left
is the software one.

It is not specific to NekoPlay — a five-line GTK4 program reproduces it —
and it is not fatal. mpv still draws through its own WGL context, so video
plays; measured against bare `mpv.exe` on the same 4K clip the fallback cost
about 28% more CPU (41% of one core versus 32%). It is worth knowing about
because the gap widens with the size of the window being composited.

To check which renderer you ended up with:

```bash
GSK_DEBUG=renderer nekoplay.exe
```

Note that the app is built against the GUI subsystem and has no console, so
that output — and any Python traceback — goes to
`%LOCALAPPDATA%\NekoPlay\nekoplay.log`. An empty log means a clean start.

## Optional runtime pieces

`ffprobe.exe` and `yt-dlp.exe` are bundled if present at build time.
`ffprobe` is only used to size the window before the first frame arrives and
the app runs without it; `yt-dlp` is what mpv shells out to for streaming
URLs.

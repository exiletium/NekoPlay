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
./build-aux/windows/installer.sh                # wrap it in NekoPlay-<ver>-Setup.exe
```

The installer step additionally needs `mingw-w64-ucrt-x86_64-nsis`.

`dist/NekoPlay/` is self-contained: about 330 MB, runs on a machine with no
MSYS2 installed, and can be zipped, moved or renamed freely. `nekoplay.exe`
works out where it lives at startup and everything else follows from that.

Drop `--with-anime4k` to skip fetching the shaders; the build then needs no
network. The archive is pinned to the same release and SHA256 as the Flatpak
manifest.

## The installer

[`nekoplay.nsi`](nekoplay.nsi) packs the same folder into a per-machine
installer, around 80 MB compressed. It installs under Program Files, puts a
shortcut in the Start Menu for all users, registers an Add/Remove Programs
entry, and writes an uninstaller. A desktop shortcut and file associations
are optional components.

The associations register NekoPlay under **Open With** for the common video
containers rather than seizing the default player, which is as much as an
installer has been permitted to do since Windows 8.

Settings live in the registry and in `%LOCALAPPDATA%\NekoPlay`, and the
uninstaller leaves both alone so a reinstall picks up where you left off.

`/S` installs silently, and replaces an existing install without prompting.

Nothing here is code-signed, so SmartScreen will warn on first run until the
binary builds reputation or a certificate is added.

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

## Direct Composition, and why the app forces it on

GSK will only present with the GPU once GDK has built a Direct Composition
device. MSYS2 builds GTK with `003-default-dcomp-off.patch`, which turns
that into an opt-in:

```c
-  if (!gdk_has_feature (GDK_FEATURE_DCOMP))
+  if (!gdk_has_feature (GDK_FEATURE_DCOMP) || g_getenv ("GDK_WIN32_FORCE_DCOMP") == NULL)
     return;
```

It returns before `DCompositionCreateDevice` is ever reached, so the device
stays NULL, GSK refuses **both** its GL and its Vulkan renderer with
"OpenGL requires Direct Composition", and the whole window — video included
— is composited by `GskCairoRenderer` in software. Nothing warns about it,
because nothing failed.

So [`src/nekoplay.in`](../../src/nekoplay.in) sets `GDK_WIN32_FORCE_DCOMP=1`,
and the launcher sets it too. It has to happen **before anything imports
`gi`**: GDK reads it while its library loads, so setting it from `main.py`,
after `from gi.repository import Gtk`, is already too late.

What it is worth, on a Radeon RX 9060 XT at 1920x1080, playing a 4K60
60 Mbit/s clip in a maximized window:

| | software (before) | GPU (after) |
| --- | --- | --- |
| 4K60 | 87.5% of one core | **~50%** |
| 1080p60 | 59.9% of one core | **~31%** |

If the GPU path ever misbehaves, `GDK_WIN32_FORCE_DCOMP=` or
`GDK_DISABLE=dcomp` puts it straight back on the software renderer, and
`GSK_RENDERER=vulkan` is a third option that also works once the device
exists. To see which renderer you got:

```bash
GSK_DEBUG=renderer nekoplay.exe
```

## What still costs more than mpv, and why

Even on the GPU path the player costs roughly 50% of one core on that 4K60
clip, against 7.7% for bare `mpv --hwdec=d3d11va` and 5.1% for VLC. That gap
is architectural, not tuning left on the table:

- **The frame copy (~20 points).** libmpv's render API offers only `opengl`
  and `sw` back ends — there is no D3D11 one. So a decoded D3D11 surface
  cannot be handed to the compositor the way mpv's own `vo=gpu` does, and
  every frame takes a GPU→CPU→GPU trip (~12.4 MB/frame at 4K, ~750 MB/s).
  Every `hwdec` value — `d3d11va`, `dxva2`, `dxinterop`, `auto` — resolves to
  `d3d11va-copy` in this context; forcing bare mpv down the same
  `d3d11va-copy` path costs it 26% instead of 7.7%, which prices the copy
  directly.
- **Toolkit compositing (~23 points).** The video has to reach a GTK scene
  graph so the overlay controls can be drawn on top. `Gtk.GraphicsOffload`
  cannot help here: GTK 4.22 implements `GdkSubsurface` only for Wayland,
  so offload is a no-op on Win32.

Things measured and found not to matter: every `hwdec` mode, mpv's
render-quality options (the whole span from `gpu-dumb-mode` to default is
under 4 points), `GSK_RENDERER=vulkan`, and `Gtk.GraphicsOffload`. Frame
pacing is already correct — GTK repaints at the video rate, not the
display's 240 Hz.

## Why it still costs more than mpv

Even on the GPU path the player costs roughly 50% of one core on that 4K60
clip, against 7.7% for bare `mpv --hwdec=d3d11va`. That gap is architectural
rather than tuning left undone:

- **The frame copy (~20 points).** libmpv's render API offers only `opengl`
  and `sw` back ends - there is no D3D11 one - so a decoded D3D11 surface
  cannot be handed to the compositor the way mpv's own `vo=gpu` does. Forcing
  bare mpv down the same `d3d11va-copy` path costs it 26% instead of 7.7%,
  which prices the copy directly.
- **Toolkit compositing (~23 points).** The video has to reach a GTK scene
  graph so the overlay controls can be drawn on top of it.

Closing either one means taking the video out of GTK entirely - handing mpv
its own native window - which is a different player with a different UI. That
was measured and rejected; the experiments are in git history around
`42baea7` if the question ever comes back.

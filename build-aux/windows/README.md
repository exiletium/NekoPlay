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
./build-aux/windows/bundle.sh                   # collect into dist/NekoPlay
./build-aux/windows/installer.sh                # wrap it in NekoPlay-<ver>-Setup.exe
```

The installer step additionally needs `mingw-w64-ucrt-x86_64-nsis`.

`dist/NekoPlay/` is self-contained: about 330 MB, runs on a machine with no
MSYS2 installed, and can be zipped, moved or renamed freely. `nekoplay.exe`
works out where it lives at startup and everything else follows from that.

`--with-video2x=DIR` copies a video2x_optimized portable bundle (the folder
holding `video2x.bat`) into the app, so AI upscaling works with no setup. It
adds ~330 MB; without it the feature works from a folder the user picks in
Preferences. See "AI upscaling and interpolation" below. Upstream's Anime4K
shader presets are not in this port: the upscale row is video2x's.

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

## One instance, like on Linux

On Linux a second `nekoplay --new-window file.mp4` hands the file to the
running instance over D-Bus and exits; the desktop entry is written that
way, and `do_open` then decides between a new window and the current one
from the "Open New Window for New Files" preference. Windows has no session
bus, so GApplication quietly made every launch a primary: every double-click
in Explorer was a whole new process and window, about a second warm.

`platform_compat.SingleInstance` does both halves with a named pipe,
`\\.\pipe\NekoPlay-<user>`. Whoever creates it first is the primary (the
one allowed instance makes the election atomic - the loser gets
`ERROR_PIPE_BUSY`); anyone else connects, writes `{"cwd", "argv"}` as JSON
and exits, at about 80 ms, before GTK would have started loading. That
happens at the top of `nekoplay.in`, before the log file is even opened, so
a forwarding launch never truncates the running instance's log. The primary
serves the pipe from `do_startup` and turns each message into what
GApplication would have done: `open()` for files, `activate()` for
`--new-window`, upstream's "NekoPlay is running" message otherwise - except
that the bare exe with no arguments counts as `--new-window`, because on
Windows the exe is the thing the user clicks. The installer's shortcuts and
file association pass `--new-window`, as the desktop entry does. Only
writes by this user reach the primary: the default pipe ACL gives other
users read access only.

Measured: the second launch exits in ~150 ms and the file is playing in the
primary ~40 ms after it arrives, in a new window or the current one
according to the preference.

## Black frames, and where the rounded corners come from

GTK draws client-side decorations and popovers on the assumption that the
surface has an alpha channel: it reserves a margin for the drop shadow and
rounds the corners, and expects everything outside to be transparent. On
Win32 nothing outside the painted area is transparent - it is black. So the
toplevel sat in a thick black frame, and every popover, menu and dropdown
list had one of its own (the list under a dropdown even carries a 6 px
`padding-top` on the transparent popover node).

The CSS in `main.py` removes the shadows and that padding, so every surface
is exactly its content. The rounded corners come back from DWM instead:
`DWMWA_WINDOW_CORNER_PREFERENCE = DWMWCP_ROUND` (8 px, the only large
radius it offers; the popover CSS radius is set to match), with
`DWMWA_BORDER_COLOR = DWMWA_COLOR_NONE` so DWM does not add a hairline of
its own. The toplevel gets this on realize. Popovers are windows GTK creates
and never hands to the app, so `platform_compat.round_new_windows` sets an
out-of-process WinEvent hook on `EVENT_OBJECT_SHOW` for this thread and
applies the attributes to every window as it is shown - shown, not created:
DWM rejects the attribute during creation (a CBT hook at `HCBT_CREATEWND`
was tried first and is too early).

Popover arrows are turned off on Windows (`strip_popover_arrows`): the tail
sits in a strip GTK leaves transparent, its height is a constant in
GtkPopover rather than CSS, and the strip would be black.

## AI upscaling and interpolation (video2x)

Preferences and the in-player options panel carry two settings backed by
video2x_optimized: **Upscale (video2x)** - Off or ×4 (Real-ESRGAN) - and
**Frame Interpolation** - Off, 2× or 4× (RIFE v4.26). Both on means two
passes, interpolation first: RIFE's cost is in full-resolution warps, so
interpolating the upscaled frames would cost sixteen times as much while
upscaling twice the frames costs twice. The intermediate lives in the cache
beside the final file and is removed when the chain finishes. It runs
video2x_optimized, the GPU-resident ONNX Runtime pipeline, as a subprocess;
it is never imported into the app, because it needs its own Python with
onnxruntime-directml. The app looks for it in `share/cine/video2x`
(bundled), then in the folder set in Preferences, and uses the bundle's own
interpreter if there is one, otherwise the first system Python that can
`import onnxruntime` (the Microsoft Store alias is skipped). ffmpeg has to be
on PATH or in the install's `bin\`.

The render is joined to playback through an mpv `on_load` hook
(`video2x.lua`), the same mechanism ytdl_hook uses: the hook changes
`stream-open-filename`, so mpv keeps reporting the original path, title and
playlist entry - and the watch history, resume position and external
subtitles all follow the original - while the bytes come from the render.

Two timings, in Preferences under **AI Render Timing**:

- **Pre-render** keeps the load deferred until the whole file is rendered,
  with a progress toast that can cancel; cancelling plays the original.
  Full quality and full seeking.
- **Live** opens the output once ~4 s of it exist past where playback will
  start and plays it as it grows. In a chain that wait includes every pass
  before the last, since only the last writes the file played. When the
  render is slower than playback the player pauses with "Rendering ahead…"
  on the OSD and continues once the render is 4 s ahead again, like
  buffering a stream. mpv follows a growing file as long as it never reads
  to the end (`--stream` makes the muxer flush every second), and if it
  does hit EOF, a seek to the same position makes the demuxer look again.

What mpv opens in live mode is not the growing file but an EDL naming it,
with the film's full length declared - mpv's own idea of a growing file's
length is however much has been written, so the seek bar would otherwise
end at the render's frontier. The EDL also settles seeking. A seek past
the frontier is simply held by mpv until those frames exist (the demuxer
reports the seek target as the position meanwhile, which is how the app
knows where the user wants to be); if the render would take more than
8 s to get there, the app cancels it and starts another from 2 s before
the target (video2x's `--start`), and the EDL then reads: the original
from 0 to that point, then the new render. Seeking back lands in the
original, seeking forward in the render, and the bar spans the whole film
throughout. The same rule applies when a file opens far in - a resume -
rather than rendering everything before the resume point first. A render
started part-way is for that sitting only: it is never marked complete
and is swept when playback of it ends.

Live renders are also fitted to the machine. Every render times its
steady part, and the seconds per output frame per input megapixel it
measures - the GPU cost is linear in input pixels - is kept per kind of
pass in `%LOCALAPPDATA%\NekoPlay\video2x-speed.json`. From then on a live
upscale has video2x decode the source small enough for the pass to run at
1.25× realtime (`--max-output-height`), rounded down to a multiple of 24
lines and never below half of what the screen can show of the source,
nor below what the screen needs; the toast says what will come out
("Upscale ×4 at 3552p"). The first live upscale on a machine has no
record yet and runs at full size, pausing if it must. Pre-renders always
run at full size, and a complete full-size render serves live sessions
too; a live render decoded smaller only serves later live sessions that
would have settled for that size or a little less (down to 85% of the
plan, so the record's drift between renders does not mean a new render
each time). Interpolation is never decoded smaller: video2x's
interpolation stalls under `--max-output-height` at present, so in a
chain RIFE runs at the source's size and the upscaler scales its output
down as it reads it.

Measured on a 608×1080 30 fps clip on an RX 9060 XT (1080p screen):
interpolate 2× runs at 4.0× realtime; upscale ×4 at full size (to
2432×4320) at 0.79×, so the first live session pauses once; fitted, it
decodes at 500×888 and runs at 1.2× with no pauses. A seek 20 s ahead
during a live upscale restarts from there and is playing again 6 s later
(11 s uncapped, most of it the 4 s lead at 0.79×).

Renders are cached under `%LOCALAPPDATA%\NekoPlay\video2x`, keyed by the
source file, its size and mtime, and the mode. Preferences shows the size,
a Clear button and a **Cache Limit** (default 10 GB): the moment the cache
grows past it - at startup, before a render, and as each render completes -
the renders not watched for longest are deleted until it fits. Half-written
leftovers go first, and a render still being written or played is skipped. Engines are per-resolution ONNX files: a missing
one is built on the spot if the install's Python has PyTorch (seconds for
the upscaler, a minute or two for RIFE); otherwise the original plays and a
toast says why. Recent video2x_optimized builds a missing engine itself, in
under a second and without PyTorch, by re-targeting one it has
(`video2x_opt.onnx_respec`); `Install.can_autobuild` detects that and skips
the pre-build, and `has_engine` looks in `models\auto`, where video2x keeps
what it built (capped at 1 GB, oldest out first), as well as `models\`.
Videos with rotation metadata render fine: ffmpeg applies the display
matrix on decode, so video2x sees the frame upright and its output is
plain upright video; the app describes such a source by its displayed
size, which is the frame the engine has to fit. A render left running
when the window closes is killed with everything it spawned - a Windows
Job Object flagged kill-on-close, so this holds even if the app dies.

## Looping

`NEKOPLAY_TRACE=1` also times loops: with loop-file on, each wrap prints the
wall time between wraps minus the clip length. It only listens to the
time-pos observer, because an earlier probe that read properties
synchronously from the main loop every 2 ms measured its own interference
(+187 ms) and nothing else. Measured over five loops of a 15 s clip: median
-45 ms, i.e. the wrap lands inside the last frame (42 ms at 24 fps) with no
stall; bare mpv on the same content gives -16 ms.

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

Closing either one means taking the video out of GTK entirely - handing mpv
its own native window - which is a different player with a different UI. That
was measured and rejected; the experiments are in git history around
`42baea7` if the question ever comes back.

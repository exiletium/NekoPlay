<img style="vertical-align: middle;" src="data/icons/hicolor/scalable/apps/moe.nyarchlinux.nekoplay.svg" width="112" height="112" align="left">

### NekoPlay

Play your 4K animes.

<br>

### Description

NekoPlay is a fork of [Cine](https://github.com/diegopvlk/Cine) but with a few extra features specifically for anime watching.

> ### This branch: the Windows port
>
> `windows-port` builds NekoPlay as a native Windows application — the same
> GTK4/libadwaita interface and the same libmpv playback, with no WSL and no
> X server. Grab the installer or the portable folder from
> [Releases](../../releases), or build it yourself with
> [`build-aux/windows/`](build-aux/windows/) (MSYS2 UCRT64).
>
> What the port adds or changes:
>
> - **One instance, as on Linux.** No D-Bus on Windows, so a second launch
>   hands its file to the running player over a named pipe and exits in
>   ~150 ms, honouring the "Open New Window for New Files" preference.
> - **AI upscaling and frame interpolation** through
>   [video2x_optimized](https://github.com/exiletium/video2x_optimized)
>   (Real-ESRGAN and RIFE on DirectML), replacing Anime4K. Render the whole
>   file first, or play it live while it renders — live renders fit
>   themselves to the machine so they keep up, and seeking anywhere in one
>   works. Quality, an output-height cap, a size-capped cache and
>   "save beside the original" are in Preferences.
> - **GPU compositing on by default** (DirectComposition), which took 4K60
>   playback from 88% of a CPU core to 51%.
> - **Startup work**, ending at roughly 950 ms warm to the first frame.
>
> The details, and the measurements behind each claim, are in
> [`build-aux/windows/README.md`](build-aux/windows/README.md).

### Features

- **Simple Design** — A refined, distraction-free interface
- **MPV-Based** — Leverages the robust power of MPV for great playback and format support
- **Audio and Subtitles** — Control track selection and synchronization for both
- **Video Controls** — Easily adjust brightness, contrast, zoom, aspect ratio, etc.

**NekoPlay-specific features**

- **4K Anime upscaling** — Watch in 4K your anime legally downloaded in 720p
- **90s Skip** — Skip openings directly with one button (*most* anime openings are 90s duration)

### Screenshot

<p align="center"><img src="screenshots/video.png" alt="Video Playing"/></p>

<div>
  <details>
    <summary>More Screenshots (Expand):</summary><br>
      <p align="center"><img height="943" src="screenshots/preferences.png" alt="Preferences"/></p>
      <p align="center"><img src="screenshots/options.png" alt="Video Options"/></p>
      <p align="center"><img src="screenshots/window.png" alt="Main Window"/></p>
  </details>
</div>

### Donate

This is a soft fork, most of the hard work has been done by Cine developers, so support them instead.

- [PayPal](https://www.paypal.com/donate?hosted_button_id=DVL7H35GA66X6)
- [Ko-fi](https://ko-fi.com/diegopvlk)
- Pix: diego.pvlk@gmail.com

In case you want to support Nyarch, here is the Nyarch Linux donation button
- [Nyarch Linux Ko-fi](https://ko-fi.com/nyarchlinux)

### Install

You can install from the .flatpak file from latest release on any distribution:

On Windows, run `NekoPlay-Setup.exe`, or unpack the portable `NekoPlay`
folder anywhere and run `nekoplay.exe` — either way nothing else is needed.

### Build from source

Clone the repo in GNOME Builder and press run.

For Windows, build in an MSYS2 UCRT64 shell:

```bash
./build-aux/windows/build.sh
./build-aux/windows/bundle.sh --with-anime4k
./build-aux/windows/installer.sh
```

See [build-aux/windows/README.md](build-aux/windows/README.md) for the
dependency list and notes on how the port works.

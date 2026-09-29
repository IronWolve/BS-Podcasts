# BS Podcasts

A desktop podcast player for Windows, macOS and Linux, built with Python,
PySide6 and mpv. Manage subscriptions, download episodes and keep your listening
queue in one place.

## Features

- Subscribe to podcast feeds and discover shows through directory search.
- Import and export subscriptions using OPML.
- Browse episodes, stream audio, and download episodes for offline listening.
- Manage Up Next, playlists, folders, favorites and listening history.
- Adjust playback speed, skip through episodes and use a sleep timer.
- Browse chapters, bookmarks and transcripts when available.
- Import local audio files and read their embedded metadata.

## Platform packages

The current build version is **0.9.10**. Packages are prepared for these targets;
check the repository's Releases page for published downloads.

| Platform | Package | Requirements and status |
| --- | --- | --- |
| Windows | `BS-Podcasts-<version>-Windows-x64.zip` | 64-bit Windows; Python and native runtime included. Unsigned. |
| macOS | `BS-Podcasts-<version>-macOS-arm64.zip` | Apple Silicon; macOS 14+ deployment target. Ad-hoc signed, not notarized. No Intel/universal2 package. |
| Linux | `BS-Podcasts-<version>-Linux-x86_64.tar.gz` | x86-64, glibc 2.43+, Python 3.14.7+, and compatible ALSA/PulseAudio and desktop libraries. |

Unsigned or non-notarized builds may trigger operating-system warnings. Verify
the download source and checksums before running them; these builds do not claim
code-signing or notarization approval.

### Windows

Extract the complete archive and run `BS Podcasts.exe` inside the `BS Podcasts`
folder. Keep `_internal` and the other bundled files beside the executable;
the EXE is not a standalone one-file application. No separate Python installation
is needed to run this package.

Close the app before replacing its application files during an update. Preserve
your library and any custom data/download directories.

### macOS

Extract the archive and open `BS Podcasts.app`. Keep the app bundle intact.
Python, Qt and the native player are bundled; running the app does not require
Homebrew mpv or a development checkout.

### Linux

Extract the archive, open a terminal in the extracted directory, then run:

```sh
bash setup.sh
bash start.sh --check --plain
bash start.sh
```

`setup.sh` installs the locked Python dependencies into the package's local
environment. The self-check uses isolated scratch data and does not open the GUI
or play audio. `start.sh` launches the app; `stop.sh` stops only this deployment.

If Python is not available as `python3.14`, set `BS_PODCASTS_PYTHON` to a compatible
interpreter when running setup. After moving the Linux installation, preserve or
rename `dists/linux/.venv` and rerun setup: Python environments contain absolute
interpreter paths. The native library is bundled, but this is not a universal
Linux binary or a package for older glibc versions.

## Library, downloads and network access

The library and settings are stored locally in SQLite. Default data locations are:

- Windows: `%LOCALAPPDATA%\BS Podcasts`.
- macOS: `~/Library/Application Support/BS Podcasts`.
- Packaged Linux launcher: `data/` beside the package's top-level launch scripts.

Downloads and caches are separate from application source. The download location
can be changed in the app. `BS_PODCASTS_DATA_DIR` selects a custom data location;
`BS_PODCASTS_CACHE_DIR` selects a custom cache location. Back up the library and
keep personal data out of source repositories and release archives.

Feed refreshes, directory searches, artwork and streaming contact the relevant
external services. Downloaded episodes can be played without fetching them again.

## Source and builds

This repository contains application source, the public README, tests, assets,
dependency locks and reproducible packaging scripts. Build environments, generated
packages, caches, logs and personal data belong outside the checkout.

The build scripts expect this layout:

```text
project/
  repo/             # this Git repository
    README.md
    src/bs_podcasts/
    packaging/
    tools/
    tests/
  dists/            # built applications and release output
  .cache/           # isolated build environments and native inputs
  tmp/              # build and test scratch
```

Source builds require Python 3.14.7+ and the matching platform's compiler/build
tools. Prepare an isolated environment from the relevant dependency lock and
build the pinned native player before packaging. Review the third-party notices
and actual build options before changing dependencies.

- [Linux runtime build](packaging/project-build.sh) and [dependency lock](packaging/requirements-linux.lock).
- [Windows packaging](packaging/build-windows.ps1) and [dependency lock](packaging/requirements-windows.lock).
- [macOS packaging](packaging/build-macos.sh), [dependency lock](packaging/requirements-macos.lock) and [native build tools](packaging/requirements-macos-native.lock).
- Native player recipes: [Linux](packaging/build-libmpv-linux.sh), [Windows](packaging/build-libmpv-lgpl.sh), [macOS](packaging/build-libmpv-macos.sh).
- [Exact source manifest](source-manifest.json) and [release archive assembly](packaging/release_archives.py).

Builds use manifest-only source snapshots and check native input receipts. Keep
build/test work separate from a user's installed application; Windows packaging
requires a dedicated, explicitly selected local test directory for SQLite checks.
Build scripts do not start the normal application. Running a source checkout
through an editable install is not the deployed-runtime workflow.

## Copyright and third-party software

Original BS Podcasts application code is **all rights reserved**. Third-party
components retain their own copyrights and licenses; that reservation does not
override their terms.

See the [third-party notices](packaging/THIRD-PARTY-NOTICES.txt),
[included license texts](packaging/licenses/) and [Inter font license](src/bs_podcasts/assets/fonts/OFL.txt).

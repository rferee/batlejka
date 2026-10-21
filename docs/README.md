# Batlejka user guide

*Sync, cut, capture.*

## What it does

Batlejka takes the raw footage of a home mocap session, recorded by several cameras at once, and turns it into motion capture:

- it works out which file came from which camera,
- it puts every clip on one shared timeline, using the cameras' clocks and their audio,
- it lets you cut that timeline into takes,
- it exports each take as one video per camera, all with the same length and frame rate,
- and it runs [FreeMoCap](https://freemocap.org) on those takes to calibrate the cameras and build a 3D skeleton.

Batlejka is a wrapper around FreeMoCap, not a replacement for it. It was made by one person for their own animation pipeline. The Calibrate and Mocap steps run FreeMoCap 1.8.2, which the app installs into its own folder from the Setup card on the Home screen. Your source footage is never modified.

## The eight steps

The header lists the steps in order. A step unlocks once the one before it has done its job.

| Step | What you do there | Page |
| --- | --- | --- |
| 1. Home | Pick or create a session, and check that the machine has what the app needs (Setup). | [Home and Setup](01-home-and-setup.md) |
| 2. Process | Check the frame rates, pick a target frame rate, and build the thumbnails, sync audio and scrub proxies. | [Process](02-process.md) |
| 3. Cameras | Name each camera and turn its picture upright. Move files between cameras if the grouping got one wrong. | [Cameras](03-cameras.md) |
| 4. Sync | Place every clip on one timeline, by camera clock first, then by audio. Check the clips it was unsure about. | [Sync](04-sync.md) |
| 5. Cut Takes | Scrub the session, play it back, and mark the takes. Fix a clip's position by hand if needed. | [Cut Takes](05-cut-takes.md) |
| 6. Export | Write every take as one video per camera, same length and frame count, into the output folder. | [Export](06-export.md) |
| 7. Calibrate | Run FreeMoCap's camera calibration on the take where you filmed the ChArUco board. | [Calibrate](07-calibrate.md) |
| 8. Mocap | Run FreeMoCap on the other takes and look at the 3D skeleton. | [Mocap](08-mocap.md) |

There is also a page of [tips and limits](09-tips-and-limits.md): things that trip people up, and what is safe to delete.

## What you need

- Windows.
- Python 3.12 or newer.
- `ffmpeg` and `ffprobe` on your PATH. The Setup card tells you if they are missing.
- A Chromium browser (Chrome or Edge). The UI does not work in other browsers.
- Optional: an NVIDIA GPU. It makes proxies and exports faster. Everything works on the CPU without it.
- Only for FreeMoCap: `git` on your PATH, and about 6 GB of free disk space for the install.

## How to start it

1. Double-click `run.bat` in the Batlejka folder.
2. The first run creates a Python environment and installs the app's packages. Later runs skip that.
3. Your browser opens at <http://127.0.0.1:8765/>. If it does not, open that address yourself.

The app only listens on your own machine. Nothing is reachable from the network.

The address bar follows you around: `#/<session>/<step>` points at a session and a step, so you can bookmark where you are.

## Contents

1. [Home and Setup](01-home-and-setup.md): sessions, the Setup card, FreeMoCap install, the job queue.
2. [Process](02-process.md): the scan, the frame-rate check, processing settings, the file table.
3. [Cameras](03-cameras.md): camera cards, rotation, names, moving files, re-grouping.
4. [Sync](04-sync.md): what sync does, the live view, the camera clocks, the offsets, what to check.
5. [Cut Takes](05-cut-takes.md): the layout, playback, making and editing takes, the inspectors, the frame viewer, every hotkey.
6. [Export](06-export.md): the takes-by-cameras table, output settings, where the files land.
7. [Calibrate](07-calibrate.md): calibration profiles, the board settings, reading the error.
8. [Mocap](08-mocap.md): the FreeMoCap settings, the takes table, the skeleton viewer, where results go.
9. [Tips and limits](09-tips-and-limits.md): known limits, gotchas, what is safe to delete.

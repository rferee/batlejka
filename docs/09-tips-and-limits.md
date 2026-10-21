# Tips and limits

Things that trip people up, what the app will not do, and what you can delete.

## What it was made for

- One person's home setup: a few phones or tablets and one camera, all filming the same room with sound. It runs on other footage, but that is what it is tuned for.
- Clips need a time stamp (*created* in the Process table) or audio. A clip with neither cannot be placed; it shows as *unplaced* after Sync and you set its offset by hand in Cut Takes.
- Cameras without audio are placed by their clock only. Check them in Cut Takes.
- The markerless capture, the calibration and the 3D tracking are FreeMoCap's. Batlejka gets the footage into the shape FreeMoCap wants and runs it. Questions about tracking quality are FreeMoCap questions: [freemocap.org](https://freemocap.org).

## Things that block you

- **ffmpeg and ffprobe are required.** Without them a red banner sits at the top of every screen and nothing runs. Install ffmpeg, put it on your PATH, restart, and press **Re-check** in Setup.
- **One job of a kind at a time, per session.** You cannot start a second export, sync or mocap run while one is going. If a button seems dead, look at the Queue pill.
- **Files that are open elsewhere cannot be replaced.** An export that must overwrite a video which FreeMoCap's own GUI, a player or Blender has open ends with *done with N errors* and *can't replace the existing file* in the log. Close the other program and export again.
- **The Mocap step needs the freemocap layout.** A take exported with the flat layout shows *flat layout — export it again* and cannot be processed or calibrated from.
- **A camera the calibration does not know blocks the take.** A take may use fewer cameras than its profile, never more. Renaming a camera after calibrating makes it unknown. Keep names stable, or calibrate a new profile.
- **FreeMoCap needs at least two cameras** in a take, and both must be in the calibration.
- **Leftover videos** in a take's folder (from a camera you have since left out of the export) must be deleted before FreeMoCap runs; the app asks you.
- **GPU options are off when their test failed.** GPU decode on Process and the h264_nvenc encoder on Export follow the tests in Setup. After a driver update, press **Re-check**.

## Sessions and folders

- Sessions must live under the sessions folder (shown at the bottom of Home). A session is `sessions\<name>\`, with `source\` for the footage, `output\` for exports and FreeMoCap's results, and `.preprocess\` for the app's own files.
- `source\` is never modified. Add and remove files there as you like, then **Re-scan** on the Process step.
- Session names cannot contain `< > : " / \ | ? *`, start with a dot, or end with a dot or a space.
- Take and camera names become folder and file names. Characters Windows does not allow are replaced, and two takes with the same name get a `_2` suffix in `output\`.
- Everything the app knows about a session is in that session's folder. Copy the whole folder to move it to another machine.

## What is safe to delete

- Inside `.preprocess\`, the folders `thumbs\`, `features\`, `audio\`, `wave\`, `proxies\` and `previews\` are rebuilt: run **Process again** (and turn **Preview** on again in Cut Takes). They are the big ones; delete them to free space when a session is finished.
- `.preprocess\` also holds the session's state: the cameras, the sync, the takes, the calibration profiles and every setting. Delete the whole folder only if you want to start the session from scratch.
- `output\<take>\synchronized_videos\` is rebuilt by a re-export.
- `output\<take>\output_data\` and the other FreeMoCap folders are never deleted by a re-export. They are overwritten when you process the take again.
- `output\calibrations\` holds one file per profile; delete profiles from the Calibrate step instead.
- `.tools\` next to the app holds FreeMoCap's environment and the GPU test cache. Use **Remove** in Setup rather than deleting it by hand.

## Takes going stale

A processed take turns **stale** on the Mocap step when its videos were re-exported, when its calibration changed (the profile was replaced, or another profile was selected for it), or when the FreeMoCap settings changed since its run. The results are still there and still open in the viewer; stale only means they no longer match what the take would give now. Process it again when you care.

## A camera that changed resolution mid-session

Phones do this: 4K for the calibration take, 1080p later. FreeMoCap would place every point of that camera wrong, and the skeleton would come out broken. Batlejka notices and scales that camera's lens parameters to the take's resolution before FreeMoCap runs. The take detail on the Mocap step says so under **calibration adjustment** (*none* means nothing had to change), and the run's log in the Jobs drawer carries the same note. Nothing to do on your side.

## Cameras and calibration

- FreeMoCap matches cameras to the calibration by their position in the list, not by name. Batlejka writes a matching calibration per take, in the take's camera order, and a *subset* one when a take has fewer cameras. That is why a take can use fewer cameras than the profile but not an unknown one.
- The first exported take with `calib` in its name is preselected for a new profile. Name your calibration take accordingly.
- The square size is the only thing that sets the scale. Measure the printed board; check the bone lengths in a processed take against the person.
- A camera's rotation from the Cameras step is baked into the exported videos. Change it after calibrating and the calibration take is *re-exported*, so the profile warns you.

## The browser

- The UI is for Chromium browsers (Chrome, Edge). Other browsers are not supported.
- Use one browser tab per server. Two tabs editing the same session would overwrite each other's saves.
- The server listens on `127.0.0.1` only. Keep it that way: it serves your footage.
- What is remembered in the browser, not in the session: which session and step you were on, the folded sidebar, inspector and hotkeys in Cut Takes, the open settings windows on Mocap, and the skeleton viewer's view. Takes, offsets, export options, the Preview toggle, the audio camera and the Mocap ticks are saved with the session, so every browser sees the same.
- The address bar carries `#/<session>/<step>`; bookmark it to come back to the same place.

## Speed

- Proxies build in the background after Process, shortest clips first. You can go on to Cameras and Sync meanwhile; Cut Takes shows a still frame with *proxy building…* where a proxy is not ready.
- Export is bound by the encoder. On an NVIDIA GPU pick **h264_nvenc**; at 4K the **p3 fast** preset is about twice as fast as p5. **Files at once** above 1 only helps when the encoder is not already busy, which with one NVENC engine it is. **Long side** 1920 is plenty for FreeMoCap and far faster than 4K.
- Mocap tracking runs on the CPU: a one-minute take with five cameras takes several minutes. **Model complexity** 1 is quicker for a first look. Turn **Run 2D image tracking** off to redo only the 3D part with new settings.
- Calibration detects the board on every frame of every camera: a 2-minute take can take 10–15 minutes. Keep calibration takes short.

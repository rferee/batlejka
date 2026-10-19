# mocap-preprocess

A small local web tool that turns a pile of raw multi-camera footage from a home
mocap session into per-take, frame-synchronized videos that
[FreeMoCap](https://freemocap.org) can process directly.

It is built for this setup: a Fujifilm body that records **one long continuous
clip** for the whole session, plus iPhones/iPads (Blackmagic Camera) that record
**one short clip per take**, every take starting with a loud clap. All clips are
placed on **one session timeline** (camera clocks from timestamps + audio, then
audio within a small window); you mark the takes,
and each take is exported as one video per camera, all with the **same frame
count and frame rate**, correctly rotated.

`source/` is never modified.

## Requirements

- Windows (works elsewhere too), Python 3.10+
- `ffmpeg` and `ffprobe` on `PATH` — or point `MOCAP_FFMPEG` / `MOCAP_FFPROBE` at them
- Optional: an NVIDIA GPU for `-hwaccel cuda` decoding and `h264_nvenc` encoding
- A Chromium-based browser (Chrome/Edge) for the UI

## Running

Double-click **`run.bat`**. It creates `.venv` on first run, installs
`requirements.txt` (FastAPI, uvicorn, numpy, scipy), starts the server on
`http://127.0.0.1:8765/` and opens the browser.

Manually:

```
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python -m mocap_preprocess [--port 8765] [--sessions-dir ./sessions] [--no-browser]
```

The server only binds to `127.0.0.1` unless you pass `--host`.

## Folder layout

```
sessions/<name>/
  source/                  # drop raw clips here (read-only to this tool)
    DSCF0001.MOV           #   a subfolder per camera is optional: source/<camera>/...
    IMG_0206.MOV ...
  output/                  # exports
    <take>/
      take_info.json       #   take bounds, fps, per-camera source/offset/sync/rotation/padding
      synchronized_videos/
        <camera>_synced.mp4
  .preprocess/             # everything derived (safe to delete; it is rebuilt)
    session.json           #   all state: settings, files, cameras, offsets, takes
    thumbs/ features/ audio/ wave/ proxies/
```

## Workflow

The header has five steps; a step unlocks once its prerequisites exist.
The URL (`#/<session>/<step>`) can be bookmarked.

1. **Session** — pick a folder in `sessions/` (or create one) and drop the
   footage into its `source/`.
2. **Process** — the folder is scanned with `ffprobe` as soon as it opens. The
   target frame rate is prefilled with the most common rate (weighted by
   duration), and the fps check lists files at a different rate (with the
   expected drift), variable-frame-rate files and files without audio. These are
   all conformed on export. *Process source files* builds thumbnails, grouping
   features, sync audio and waveforms, groups files into cameras, places clips
   provisionally by creation time, and then builds low-res scrub proxies in the
   background (short clips first).
3. **Cameras** — one card per camera. Fix orientation with the rotate buttons
   (a CSS transform in the browser; pixels are only rotated during export),
   rename (`cam1`…), exclude, or move a file to another camera / a new one.
   Changes autosave.
4. **Sync** — two stages, shown live on the Sync screen: clips start **orange**
   where their timestamps put them, move when their camera's clock is worked out,
   and turn **green** when the audio clicks them into place (*Replay* re-runs the
   animation afterwards; the log, camera clocks and offsets are further down).
   1. *Camera clocks.* Each camera has its own clock (network-synced phones
      agree to the second; standalone cameras can be minutes or hours off) and
      stamps `creation_time` either at the start or at the end of a recording.
      Clips of different cameras whose timestamps are within 10 min of each other
      are matched by a full audio search; every match implies a difference between
      the two cameras' clocks, and per camera pair the largest cluster within ~1 s
      wins (not the mean). Start vs. end stamping is decided per camera by which
      reading makes those clusters tightest, and cameras are chained to a
      reference camera (the one holding the longest file) through the strongest
      links. A camera with no confident link is attached by a full search against
      the placed cameras, checked against its own other clips.
   2. *Fine alignment.* Each clip's prediction (timestamp + its camera's clock
      offset) is refined by audio only within ±W of it (W = 1.5 s, or 3× the
      camera's clock spread if larger), so looping music or rhythmic noise can't
      pull a clip a bar off. Clips that overlap in time form a group and vote
      together (their correlation curves are summed), so one ambiguous clip can't
      drift on its own. Results are settled at the full sample rate.
   - Fallbacks are labelled with their method: a weak peak in the window keeps
     the predicted placement (*camera clock*, amber); a file without creation
     time, or of a camera that links to nothing, is placed by a full search
     (*full search*, orange); a file without audio gets its camera clock's
     prediction (amber); a file with nothing to go on goes after the end (red).
   - Checks: clips of one camera never overlap, and files further than W from
     their camera's clock are flagged (⚠ in *Offsets*, and in the inspector).
   - Manual offsets are never touched unless you tick *re-sync manual files*.
5. **Review & export** — the main screen: camera grid on top, timeline below,
   takes / cameras / output on the left, an inspector on the right.
   - Scrub by clicking/dragging the ruler or the lanes; wheel zooms, Shift+wheel
     pans, **Fit** shows everything.
   - Clip bars are coloured by placement: grey = audio/reference, orange = full
     search (check it), amber = camera clock only (check it), red = unplaced,
     blue = manual. Click a bar to see its
     sync score and to nudge its offset (±1 frame, ±10 frames, ±1 s, or type it),
     or *Re-sync against…* a file you choose (e.g. when looping music matched a
     bar off).
   - Takes: drag in the takes row to create one, drag a take to move it, drag its
     edges to resize, double-click to rename. Or mark **I** / **O** and press
     **N**, or use **Auto-detect takes** (clusters of short clips; the continuous
     camera is ignored). Rename the calibration take yourself (e.g.
     `calibration`).
   - Keys: Space play/pause · ←/→ one frame · Shift+←/→ one second · I/O set
     in/out (of the selected take, or pending marks) · N new take from marks ·
     Del delete take · Home/End · F fit · Ctrl+Z undo take edits · Esc deselect.
   - The sidebar shows per-take coverage ("4/4 cams", or which camera is missing
     or will be padded) and a preview of the output tree. **Export** writes one
     folder per take; every file is checked to have exactly
     `round(duration × fps)` frames.

## Bringing exports into FreeMoCap

Each `output/<take>/` folder is laid out like a FreeMoCap recording: a
`synchronized_videos/` folder whose videos all have identical frame counts.

1. Copy the take folders (or just the ones you need) into your FreeMoCap
   recordings folder (by default `~/freemocap_data/recording_sessions/`), or
   point FreeMoCap at them directly — a folder with `synchronized_videos/`
   inside is what it loads as a recording.
2. Process the calibration take first (the one with the ChArUco board) to get a
   camera calibration.
3. Process the other takes with that calibration. The camera order/names are the
   `<camera>_synced.mp4` file names, so keep camera names stable across takes.

Pick the **flat** layout in *Output › Advanced* if you prefer the videos directly
in `output/<take>/`.

## Settings worth knowing

- **Target fps** — every export is conformed to it with ffmpeg's `fps` filter
  (drops/duplicates frames by timestamp), so 60 vs 59.94 or slightly-VFR iPhone
  clips line up.
- **Sync sample rate** (8 kHz default), **thresholds** and the **window** (*Process ›
  Advanced*): a full-search match is accepted when its normalized correlation
  `score ≥ 0.10` and `ratio ≥ 1.3` (best peak vs. best peak more than 0.1 s
  away); inside the ±W window a quarter of that score is enough, since the
  window already rules out the far-away look-alikes. W defaults to 1.5 s.
- **GPU decode** uses `-hwaccel cuda` for proxies and export (falls back to CPU
  if it fails); the **h264_nvenc** encoder is in the export options.
- **Long side** (1920 by default) only ever downscales; *keep* exports at source
  resolution.

## How it works (short)

- *Grouping*: subfolders win; otherwise files are partitioned by
  `(make, model, codec, display size, nominal fps)`. Identical devices are split
  because one camera can't record two overlapping clips, and by comparing each
  clip's static background (per-pixel median of five tiny grey frames, mildly
  high-passed so the shared ceiling/floor layout of the room doesn't dominate).
- *Sync*: pre-emphasis to favour transients such as claps, z-score, FFT
  cross-correlation (same core as skelly_synchronize), then the correlation is
  normalized per lag by the overlapping energies so partial overlaps compare
  fairly. `offset_x = offset_ref + a0_ref + lag/sr − a0_x` accounts for audio
  streams that don't start at the container start. Clock links and windows are
  searched on 2 kHz audio (distant microphones agree far better below 1 kHz, and
  it's 4× cheaper); the final placement is refined at the sync sample rate. A
  long continuous camera is only searched against one camera pair once the
  cameras are linked otherwise.
- *Export*: `ffmpeg -ss <seek> -i SRC -vf "tpad(start),fps,rotate,scale,tpad(stop)"
  -frames:v N`, audio delayed/padded to match. Rotation filters are applied only
  here.

## Development

```
pip install -r requirements-dev.txt
python -m pytest
```

The suite builds a synthetic session from the clips in
`F:\freemocap\calibration` (override with `MOCAP_TEST_CLIPS`) — four fake
cameras cut from different views, two identical "iPhones", one camera at 30 fps
instead of 29.97, one clip without audio, a rotated continuous "Fuji", a shared
soundtrack with claps and white flashes burned into the video — and checks
grouping, sync accuracy (±1 frame), auto takes, equal frame counts, rotation,
visual alignment of the flashes across cameras, padding, and HTTP Range support.
Generate it on its own with `python tests/make_synthetic_session.py`; it lands in
`tests/_tmp/sessions/synthetic/`, which you can open in the UI with
`python -m mocap_preprocess --sessions-dir tests/_tmp/sessions`.

Four small `scn_*` sessions (`--scenarios`) test the two-stage sync, each
requiring every audio clip within one frame of the clips it overlaps:
`scn_loop` (one 6 s music loop for the whole session; a whole-file search lands
take 1 on another repeat), `scn_clock` (an action camera 3 min fast that stamps
the end of each clip), `scn_nocont` (no continuous camera; one take linked to
nothing), `scn_nometa` (a clip without creation time and a camera without any
metadata; plus a run with every creation time removed).

Code map: `server.py` (API, media with Range), `pipeline.py` (scan, fps check,
process, proxies), `grouping.py`, `syncing.py`, `takes.py`, `exporter.py`,
`ffmpeg_tools.py`, `session_store.py` (locked, atomic `session.json`), `jobs.py`
(background jobs), `static/` (plain HTML/CSS/JS, no build step).

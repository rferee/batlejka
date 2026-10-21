# Batlejka (mocap-preprocess) — implementation spec

> **Status: historical.** This is the spec the first milestones were built from; it is
> kept for the reasoning behind the requirements (§1, §3, §6, §12). Where it disagrees
> with the code, the code and the README win. Known drift: the UI has eight steps
> (Home · Process · Cameras · Sync · Cut Takes · Export · Calibrate · Mocap), not the
> three tabs of §7; the sync is the two-stage camera-clock + windowed-audio design the
> README describes, not the single-reference passes of §6.5; the API (§8) has grown mocap,
> calibration-profile, preview, frame and skeleton routes, and `mocap.calibration` (§12.5)
> is gone in favour of calibration profiles; the app needs Python 3.12.

A small local web tool that turns a pile of raw multi-camera footage from a home mocap
session into per-take, frame-synchronized videos that FreeMoCap can process directly.

Read this whole file before writing code. `ui.png` in the repo root is the layout
reference for the main screen (see §7).

---

## 1. Context (read this, it explains the weird requirements)

- The owner records mocap at home with **4–5 cameras**: a Fujifilm X-series body on a
  tripod plus iPhones/iPads running the **Blackmagic Camera** app (started/stopped
  together from a Mac).
- **The Fuji often records one long continuous clip** for the whole session (touching it
  between takes would move it and break calibration). **The phones record one short clip
  per take.** So `source/` typically holds e.g. 1 long Fuji file plus 4 × N short phone
  files.
- Every take starts with a **loud clap**, and there may be **background music**. All
  cameras record audio, which is what we sync on.
- Frame rates: ideally everything is 29.97 (`30000/1001`) or 59.94 (`60000/1001`), but
  mixes (60 vs 59.94, iPhone near-VFR) happen and must be detected and conformed.
- Phones may record **portrait**, sometimes with rotation metadata and sometimes without,
  so the user must be able to fix orientation per camera.
- Downstream: **FreeMoCap 1.8** wants one recording folder per take containing a
  `synchronized_videos/` folder whose videos all have **identical frame counts**.
- Machine: **Windows**, Python 3.11 available (FreeMoCap env), **ffmpeg/ffprobe on PATH**,
  NVIDIA RTX 3090 (so `h264_nvenc` / `-hwaccel cuda` are available as options).
- Session footage lives in `sessions/<session-name>/source/`. The first real session is
  `sessions/2026-10-19/`.

## 2. Goals / non-goals

**Goals**
1. Pick a session, scan `source/`, prefill settings from metadata, flag frame-rate problems.
2. Work out which file came from which camera, automatically, with easy manual fixes.
3. Let the user set **orientation and a name per camera** from one preview frame.
4. Sync **every clip onto one session timeline** using audio.
5. Timeline + multi-camera grid where the user scrubs, marks takes, and names them.
6. Export selected takes: one folder per take, one `<camera>_synced.mp4` per camera,
   all with the same frame count and frame rate, correctly rotated.

7. Calibrate the cameras and process takes with a pinned FreeMoCap (§12), headless,
   in its own environment.

**Non-goals**
- No pose tracking or calibration code of our own: FreeMoCap does both (§12). No editing
  of FreeMoCap's results.
- No accounts, no remote access (bind to `127.0.0.1`).
- No editing of source files, ever. `source/` is read-only to this tool.

## 3. Hard requirements (do not violate)

- **R1. Rotation only at export.** The preview must never re-encode or rotate video for
  orientation changes. In the UI, per-camera rotation is a **CSS transform** on the
  `<video>`/`<img>` element only, so changing it is instant and scrubbing never stalls.
  The only place pixels are rotated is the export ffmpeg command (§6.6).
  (ffmpeg's default *autorotate* of the container's display-matrix metadata still applies
  when decoding; that's free and keeps preview and export consistent. User rotation is
  applied on top of it.)
- **R2. Scrubbing must stay smooth** with 5 cameras: preview from low-res proxies with
  short GOPs (§6.4), throttle seeks (§7.4).
- **R3. Equal frame counts per take** in the export (use `-frames:v N`, pad if needed).
- **R4. Never modify `source/`.** All derived data goes to `sessions/<s>/.preprocess/`,
  exports to `sessions/<s>/output/`.
- **R5. Long jobs never block the UI**: run in background threads with progress
  reporting and cancel.
- **R6. Works on Windows** (paths with spaces, `os.replace` retries, no shell=True,
  `CREATE_NO_WINDOW` for subprocesses).

## 4. Stack and layout

- Python ≥ 3.12 (`tomllib`), **FastAPI + uvicorn**, **numpy**, **scipy**. No other heavy deps.
- Frontend: **plain HTML/CSS/JS, no build step**, served by FastAPI as static files.
- ffmpeg/ffprobe via `subprocess` (list args). Discover with `shutil.which`, overridable
  by env vars `MOCAP_FFMPEG` / `MOCAP_FFPROBE`. If missing, the UI shows a clear error.

```
mocap-preprocess/
  SPEC.md  README.md  ui.png
  .gitignore            # replace the stray ",gitignore" file (typo) with a real one
  requirements.txt      # fastapi, uvicorn, numpy, scipy
  run.bat               # create .venv if missing, pip install -r, start server, open browser
  mocap_preprocess/
    __main__.py         # CLI: --host 127.0.0.1 --port 8765 --sessions-dir ./sessions --no-browser
    server.py           # FastAPI app, routes, static + media serving
    config.py           # paths, tool discovery, constants
    ffmpeg_tools.py     # probe, frame grabs, audio extraction, proxy + export commands, progress parsing
    session_store.py    # session.json load/save with per-session lock, atomic writes
    jobs.py             # background job manager (progress, log, cancel)
    grouping.py         # file → camera assignment
    syncing.py          # audio cross-correlation placement on the session timeline
    takes.py            # auto take detection
    exporter.py         # per-take export
    static/  index.html  app.js  timeline.js  grid.js  style.css
  tests/
    make_synthetic_session.py
    test_*.py           # pytest
  sessions/             # gitignored
```

`.gitignore`: `sessions/`, `.venv/`, `__pycache__/`, `*.pyc`, `.pytest_cache/`,
`tests/_tmp/`. Delete the stray `,gitignore` file.

Session folder:
```
sessions/<name>/
  source/                  # user drops raw files here (subfolders allowed, see §6.2)
  output/                  # exports
  .preprocess/
    session.json           # all state
    thumbs/<fid>.jpg       # preview frame per file
    features/<fid>.npy     # grouping feature
    audio/<fid>_<sr>.npy   # mono float32 audio for sync
    wave/<fid>.json        # waveform peaks for the timeline
    proxies/<fid>.mp4      # low-res scrub proxy
```

## 5. Data model (`session.json`)

```jsonc
{
  "version": 1,
  "name": "2026-10-19",
  "settings": {
    "target_fps": "30000/1001",      // string fraction; prefilled (§6.1), user-editable
    "target_fps_user_set": false,
    "sync_sample_rate": 8000,
    "proxy_long_side": 640,
    "hwaccel": false,                // -hwaccel cuda for decoding in proxies/export
    "sync_min_score": 0.10,          // tune with tests, see §6.5
    "sync_min_ratio": 1.3
  },
  "files": [{
    "id": "f_3a9c1e02bd",            // "f_" + sha1(relative path)[:10], stable
    "rel": "IMG_0206.MOV",           // path relative to source/
    "size": 447145355, "mtime": 1792425610.2,   // fingerprint; change → invalidate caches
    "probe": {
      "duration": 61.43, "format_start": 0.0,
      "video": {"codec": "hevc", "width": 1920, "height": 1080,
                "display_width": 1080, "display_height": 1920,   // after metadata rotation
                "rotation_meta": 90, "r_fps": "30/1", "avg_fps": "1798/60",
                "nominal_fps": "30000/1001", "vfr_suspect": true,
                "nb_frames": 1841, "start": 0.0},
      "audio": {"codec": "aac", "sample_rate": 48000, "channels": 1, "start": 0.0} ,  // or null
      "creation_time": 1792425000.0, // epoch seconds or null
      "make": "Apple", "model": "iPhone 13", "encoder": "...", "handler": "..."
    },
    "assets": {"thumb": true, "features": true, "audio": true, "wave": true, "proxy": false},
    "camera": "c2",                  // camera id or null
    "offset": 123.456,               // session-timeline time (s) of this file's t=0, or null
    "sync": {"method": "audio", "ref": "f_...", "score": 0.41, "ratio": 3.2}
                                     // method: reference | audio | metadata | manual | none
  }],
  "cameras": [{"id": "c1", "name": "cam1", "rotation": 0, "include": true}],
                                     // rotation: extra clockwise degrees 0/90/180/270
  "takes": [{"id": "t1", "name": "take_01", "start": 12.30, "end": 45.60, "export": true}],
  "export_options": {"layout": "freemocap", "long_side": 1920, "encoder": "libx264",
                     "crf": 17, "audio": true, "overwrite": true},
  "state": {"scanned_at": null, "processed_at": null, "synced_at": null}
}
```

**File time** is measured from the container's format start (what ffmpeg `-ss` on an
input uses). **Session time** = `file.offset + file_time`. All takes are in session time.

## 6. Pipeline

### 6.1 Scan (automatic when a session is opened, fast)
- Recursively list video files in `source/` (`.mp4 .mov .mkv .avi .m4v .mts .m2ts .mxf`,
  case-insensitive). Drop entries for deleted files; re-probe files whose size/mtime
  changed (and invalidate their cached assets).
- `ffprobe -v error -show_format -show_streams -of json`. Extract the fields in §5:
  - fps from `r_frame_rate` and `avg_frame_rate`; **nominal fps** = `avg` snapped to the
    nearest standard rate within 0.3% (24000/1001, 24, 25, 30000/1001, 30, 48, 50,
    60000/1001, 60, 100, 120000/1001, 120, 240), else the raw value.
  - `vfr_suspect` if `r` and `avg` differ by more than 0.2%.
  - rotation from the display-matrix side data (`side_data_list[].rotation`) or the
    legacy `tags.rotate`; normalize to 0/90/180/270 clockwise; swap display dims for 90/270.
  - `creation_time` from `format.tags.creation_time` or
    `com.apple.quicktime.creationdate` (parse ISO 8601 with timezone).
  - make/model from `com.apple.quicktime.make/model`, `make/model` tags; also keep
    `encoder` and the video stream's `handler_name` (useful for grouping).
- **Prefill `target_fps`** (unless the user already set it) with the most common
  nominal fps weighted by total duration.
- **FPS check** (shown immediately on the Source screen):
  - ✓ all files at the target nominal rate;
  - ⚠ files at a different rate (show which, e.g. "60 vs 59.94 → ~2 frames of drift per
    minute at 30 fps"); these are conformed on export;
  - ⚠ VFR-suspect files ("will be conformed to constant fps on export");
  - ✗ files with no video stream; ⚠ files with no audio ("can't be audio-synced; will be
    placed by metadata, check manually").

### 6.2 Process ("Process source files" button)
Runs as a job over all files (thread pool of ~3 workers, ffmpeg does the heavy lifting):
1. **Thumbnail**: one frame at `0.3 × duration`, scaled to fit 480×480, JPEG.
2. **Grouping feature**: grab 5 frames at 10/30/50/70/90% (fast input `-ss` per grab),
   scale each to 32×32 gray (raw output to stdout), take the **per-pixel median** (that's
   the static background, which removes the moving person), z-score it, save as `.npy`.
3. **Audio**: `ffmpeg -vn -map 0:a:0 -ac 1 -ar <sync_sample_rate> -f f32le -` → numpy,
   save `.npy`. Skip files without audio.
4. **Waveform**: from that audio, peak |amplitude| per 20 ms bin, normalized to 0–255,
   saved as JSON (the timeline draws it; claps should be visible spikes).
5. **Camera grouping** (§6.3), unless the user has already assigned cameras. Then only
   assign new or unassigned files to existing cameras (best match, else a new camera).
6. **Provisional placement** by metadata so the timeline is useful before syncing:
   `offset = creation_time − min(creation_time)` (method `metadata`).
7. Start the **proxies job** (§6.4) automatically in the background.

### 6.3 Camera grouping (`grouping.py`)
1. **Subfolders win**: if a file is inside `source/<sub>/`, then `<sub>` is its camera
   (also used as the default camera name). If all files have subfolders, skip the rest.
2. Otherwise **partition** files by signature `(make, model, codec, display WxH, nominal fps)`.
   Different signature ⇒ different camera.
3. **Within a partition**, split identical devices (e.g. two of the same iPhone):
   - Two files whose creation-time intervals `[ct, ct + duration]` overlap by more than
     1 s **cannot** be the same camera (a camera can't record two clips at once).
   - Sort files by creation time, greedily add each to the cluster whose mean feature has
     the highest Pearson correlation **and** has no overlapping member; join if the
     correlation is ≥ 0.6, otherwise start a new cluster. Then merge clusters whose means
     correlate ≥ 0.85 and don't overlap.
4. Order cameras by their earliest clip and name them `cam1 … camN`
   (or the subfolder name). `rotation = 0`, `include = true`.
5. The UI lets the user move any file to another camera or a new one (§7.2), so
   heuristics only need to be right most of the time.

### 6.4 Proxies (background job)
Per file, H.264 low-res proxy for scrubbing, **no user rotation** (R1):
```
ffmpeg -y [-hwaccel cuda] -i SRC -map 0:v:0 -map 0:a:0? \
  -vf "scale=w='if(gte(iw,ih),min(L,iw),-2)':h='if(gte(iw,ih),-2,min(L,ih))'" \
  -c:v libx264 -preset veryfast -crf 28 -g 15 -keyint_min 15 -sc_threshold 0 \
  -pix_fmt yuv420p -c:a aac -ac 1 -b:a 96k -movflags +faststart \
  -progress pipe:1 -nostats .preprocess/proxies/<fid>.mp4.tmp.mp4   (rename when done)
```
`L = proxy_long_side`. (The scale expression was verified with ffmpeg 6: 1920×1080 →
640×360, 1080×1920 → 360×640.) Do the long files last so short clips become usable first.
Progress comes from `out_time_us` vs. duration. If `-hwaccel cuda` fails, retry without it.

### 6.5 Sync (`syncing.py`, "Sync" button on the timeline screen)

**Why not call skelly_synchronize's folder pipeline:** `skelly_synchronize` takes one clip
per camera and trims everything to the common overlap. With one long Fuji clip plus
many short per-take phone clips, that can't work. Use the **same core math** as
skelly_synchronize (`core_processes/correlation_functions.py::cross_correlate`: z-scored
audio → `scipy.signal.correlate(a, b, mode="full", method="fft")` →
`scipy.signal.correlation_lags` → argmax), but place every clip on one session timeline.

Per pair (reference audio `r`, clip audio `x`, both at `sync_sample_rate`):
1. Pre-emphasis (`y[n] = x[n] − 0.97·x[n−1]`) to favor transients like claps, then z-score.
2. `c = correlate(r, x, "full", "fft")`, `lags = correlation_lags(len(r), len(x), "full")`.
   A lag `k` means x's sample 0 lines up with r's sample k.
3. **Normalize per lag** by the overlap energies (cumulative sums of squares) to get a
   normalized correlation `ncc[k]`, and mask out lags whose overlap is shorter than
   `min(5 s, 50% of the shorter clip)`.
4. `best = argmax(ncc)`. `score = ncc[best]`. `ratio = score / max(ncc outside ±0.1 s of best)`.
   Accept if `score ≥ sync_min_score and ratio ≥ sync_min_ratio`. Tune these defaults on
   the synthetic tests (§9) and keep them editable under "Advanced".
5. Offset math, including audio start offsets
   (`a0 = audio.start − format_start` per file):
   `offset_x = offset_r + a0_r + best/sr − a0_x`.

Placement:
1. Reference = the longest file with audio. `offset = 0`, method `reference`.
2. Pass 1: correlate every other file with audio against the reference.
3. Later passes (up to 4): correlate files that are still unplaced against files placed
   in the previous pass (covers sessions without a long continuous camera). Keep the
   best accepted match.
4. Fallback for files that are still unplaced (no audio, or no confident match):
   use creation time, through the same camera's clock if possible
   (`offset_f = offset_g + (ct_f − ct_g)` for a placed file `g` of the same camera),
   otherwise the median clock offset of all placed files. Method `metadata`, shown in
   amber. No creation time: put it after the end, method `none`, shown in red.
5. Shift all offsets so the earliest file starts at 0.
6. **Never overwrite `manual` offsets** unless the user ticks "re-sync manual files".

Also offer a per-file **"Re-sync against…"** action (pick a reference file) for when the
automatic match is wrong (e.g. loopy music matching one bar off).

### 6.6 Export (`exporter.py`)
For each selected take and each included camera:
- Use every file of the camera that overlaps `[take.start, take.end]`, in order.
  No overlap: skip and report. Partial overlap: pad with black (and silence) so every
  camera has exactly the same length; the same for breaks between files.
- Several files: lay them out in output frames — file k covers
  `[max(prev_end, round(s_k·fps)), min(N, round(e_k·fps)))` with `s_k, e_k` its start/end
  in take time (an overlapping file starts where the previous one ends) — and join one
  chain per file (`fps, scale, rotation, tpad=start=<break frames>, trim`) with
  `concat`. Audio: one input per file, delayed to the file's exact start (samples),
  silence for files without audio. Single file: the command below.
- `in_t = take.start − file.offset`; `pre_pad = max(0, −in_t)`; `seek = max(0, in_t)`;
  `N = round((take.end − take.start) × target_fps)`; `post_pad` = whatever the file is
  missing at the end (+1 frame for safety).
- Command (build as a list):
```
ffmpeg -y [-hwaccel cuda] -ss <seek> -i SRC
  -vf "[tpad=start_duration=<pre_pad>:color=black,]fps=<target_fps>,
       [<rotation filters>,][<scale>,][tpad=stop_duration=<post_pad>:color=black,]format=yuv420p"
  -frames:v N
  -c:v libx264 -preset medium -crf <crf>        (or: -c:v h264_nvenc -preset p5 -cq <crf+2>)
  [-af "adelay=<pre_pad_ms>:all=1,apad" -c:a aac -b:a 160k -t <dur>]  or  -an
  -movflags +faststart -progress pipe:1 -nostats OUT
```
  - Rotation filters by `camera.rotation` (clockwise): 90 → `transpose=1`,
    180 → `transpose=1,transpose=1`, 270 → `transpose=2`. This is the **only** place
    user rotation is applied (R1).
  - Scale (optional, default long side 1920, only ever downscale), same expression as
    the proxy with `L = long_side`.
  - Input `-ss` with re-encoding is frame-accurate enough (within one source frame).
    Never use `-c copy` for cuts.
- Output layout (`export_options.layout`):
  - `freemocap` (default): `output/<take>/synchronized_videos/<camera>_synced.mp4`
  - `flat`: `output/<take>/<camera>_synced.mp4`
- Also write `output/<take>/take_info.json` (take bounds, fps, for each camera the
  source file, offset, sync method/score, rotation, pre/post padding, and `sources`:
  every file used with its seek, break and frame count).
- After each take, verify with ffprobe that all outputs have exactly `N` frames. Report
  any mismatch as an error in the job log.
- Sanitize take and camera names for Windows filenames.

### 6.7 Auto-detect takes (`takes.py`)
- "Continuous" files = duration ≥ 60% of the session span. Ignore them for clustering.
- Cluster the remaining file intervals in session time (merge if they overlap or the gap
  is < 2 s).
- Take for each cluster = intersection of the per-camera coverage in that cluster
  (latest camera start → earliest camera end). If that's empty or < 1 s, use the union.
- Name them `take_01, take_02, …`. If takes already exist, ask before replacing.
- The user renames the calibration take themselves (e.g. `calibration`).

## 7. UI

Single page, dark theme, dense "editor tool" look. A top bar holds the session picker
(dropdown of folders in `sessions/`, plus "New session" and "Open folder", which calls
`os.startfile` on Windows) and three step tabs: **1 Source · 2 Cameras · 3 Timeline**.
Disable a tab until its prerequisites exist. Show active jobs with a progress bar and a
cancel button in the top bar, plus a collapsible job log.

### 7.1 Source
- File table: name, camera (once grouped), duration, display resolution, codec,
  nominal fps (+ raw avg), VFR flag, audio yes/no, rotation metadata, creation time,
  make/model, asset status (thumb/audio/proxy).
- Settings form prefilled from the scan: target fps (dropdown of standard rates + "custom"),
  sync sample rate, proxy size, GPU decode toggle, advanced sync thresholds.
- FPS check panel (§6.1).
- **Process source files** button with progress.

### 7.2 Cameras
- One card per camera: a large preview frame (thumbnail of its first clip), **rotate ⟲ ⟳
  buttons that only change a CSS transform** (R1), a name input (default `camN`), an
  include toggle, a short label (make/model · resolution · fps), and the list of its
  files (mini thumbnail, name, duration, start time) with a dropdown on each to move it
  to another camera or "new camera".
- "Re-run auto grouping" (asks before overwriting manual changes). Save changes
  explicitly or debounce autosave; either way, never lose edits on reload.

### 7.3 Timeline (main screen, follow `ui.png`)
Layout per the reference sketch:
- **Top-left: settings/inspector panel** (session summary; when a file or take is
  selected, show its properties: offset, sync method/score, nudge buttons ±1 frame,
  ±10 frames, ±1 s, a numeric offset field, "Re-sync against…").
- **Top, across: the camera grid**, one cell per included camera (`cam1 … cam5`), each
  a `<video>` of the proxy that covers the playhead, rotated by CSS. "No footage" when
  nothing covers the playhead; the static thumbnail while its proxy is still building.
- **Middle: the timeline.** Time ruler from 0:00:00 to the session end; a **takes row**
  (take ranges drawn as labelled brackets, like the red "take 1" in the sketch); then
  **one row per camera** with its files drawn as bars at their offsets (one long bar for
  the continuous Fuji, clusters of short bars for the phones), with waveforms inside the
  bars. Colour the bars by sync method (reference/audio = normal, metadata = amber,
  none = red, manual = blue).
- **Left: takes list** (checkbox, name, duration, coverage); export is its own step (§7.5).

Interactions:
- Click/drag in the ruler or on empty track space → move the playhead (scrub).
- Wheel = zoom around the cursor, Shift+wheel = pan, "Fit" button.
- Takes row: drag on empty space to create a take; drag a take body to move it; drag
  its edges to resize; click to select; double-click to rename. Del deletes the
  selected take.
- Click a file bar to select it (inspector shows its sync info and nudge controls).
- Keys: Space play/pause · ←/→ one frame (at target fps) · Shift+←/→ 1 s ·
  I / O set the selected take's in/out at the playhead (with nothing selected, I/O set
  pending marks and N creates a take from them) · Home/End.
- "Sync" and "Auto-detect takes" buttons in the timeline toolbar. An "audio from"
  dropdown chooses which camera you hear during playback (others are muted).
- Persist takes and offsets to `session.json` on every change (debounced).

### 7.4 Grid playback / scrubbing (R2)
- Per camera, find the file covering session time `t` and set
  `video.currentTime = t − offset` (switch `src` when the covering file changes).
- **Throttle seeks**: if a video is still seeking, remember only the latest requested
  time and issue it on `seeked`. Never queue every mousemove.
- Playback: a master clock (`requestAnimationFrame`) advances the playhead; videos
  `play()` and are corrected only when drift exceeds ~0.1 s. Videos whose coverage
  starts mid-playback get their `src` and start playing when the playhead reaches them.
- Size each video to fit its cell from the display dims, taking the CSS rotation into
  account (for 90/270, swap the dims when computing the fit, then
  `transform: rotate(Xdeg)` around the centre).
- Media endpoints **must support HTTP Range requests (206)** or Chrome can't seek.
  Check whether the installed Starlette `FileResponse` handles Range. If it doesn't,
  implement a small range-serving endpoint.

### 7.5 Export step
(A sidebar at first; it moved to its own step after Review because the sidebar was too
cramped.)
- Takes × cameras table: a row per take (checkbox, name, in → out, duration) and a
  column per camera (checkbox in the header, default = the camera's `include`); each
  cell shows that camera's coverage of the take (full / padded by how much / joined
  from N files / missing → skipped). Select all / deselect all.
- Options: target fps (shows the session's), long side (keep / 1920 / 1280 / 1080),
  encoder (libx264 / h264_nvenc), quality (CRF/CQ), include audio, layout
  (freemocap / flat), overwrite existing.
- **Export** button → job with per-file progress; when done, link to "Open output folder".

## 8. HTTP API (suggested)

```
GET  /api/health                               → {ffmpeg, ffprobe, version}
GET  /api/sessions                             → [{name, n_source_files, processed, synced}]
POST /api/sessions {name}                      → create source/ + output/
GET  /api/sessions/{s}                         → full session.json + derived fps check
POST /api/sessions/{s}/scan                    → job
POST /api/sessions/{s}/process                 → job (then starts proxies job)
PUT  /api/sessions/{s}/settings
PUT  /api/sessions/{s}/cameras {cameras, assignments}
POST /api/sessions/{s}/regroup                 → job
POST /api/sessions/{s}/sync {resync_manual?}   → job
PUT  /api/sessions/{s}/files/{fid}/offset {offset}       (method → manual)
POST /api/sessions/{s}/files/{fid}/resync {ref_fid}
PUT  /api/sessions/{s}/takes [..]
POST /api/sessions/{s}/takes/auto
POST /api/sessions/{s}/export {take_ids, camera_ids, options} → job
POST /api/sessions/{s}/reveal {what: "session"|"output"}
GET  /api/jobs?session={s}   GET /api/jobs/{id}   POST /api/jobs/{id}/cancel
GET  /media/{s}/thumb/{fid}.jpg   /media/{s}/proxy/{fid}.mp4   /media/{s}/wave/{fid}.json
```
Validate session names and file ids (no path traversal). Jobs: at most one job of each
kind per session at a time. `session.json` updates go through a per-session lock with
read-modify-write and atomic replace (retry `os.replace` a few times on Windows
`PermissionError`).

## 9. Testing

**Test footage on this machine:** `F:\freemocap\calibration\` holds three already-synced
clips from an earlier session: `synced_cam1.mp4`, `synchronized_videos\synced_cam2.mp4`,
`synchronized_videos\synced_cam3.mp4`. `synced_cam3.mp4` is H.264, 1920×3414 portrait,
29.97 fps, 118 s, and has **no audio track** (the others probably don't either, since they
were exported with `-an`). Don't modify that folder.

Write `tests/make_synthetic_session.py` that builds `tests/_tmp/sessions/synthetic/source/`
with known ground truth:
- A synthetic **room soundtrack** for the whole span: background "music" (filtered noise
  plus tones) and sharp **claps** at take starts. Each fake camera gets that track with
  its own EQ, gain, and independent noise (different mics).
- Brief full-frame **brightness flashes** at known, irregular times burned into the base
  video (drawbox), so exports can be checked for visual alignment.
- Fake cameras made from the test clips by **cropping different regions** (different
  "views"):
  - `fuji`: one continuous clip covering everything, with rotation metadata set
    (tests orientation handling), named `DSCF0001.MOV`.
  - two **identical-model "iPhones"** (same make/model tags, same prefix `IMG_`) with
    3 short clips each at different, known offsets and slightly different start/stop
    times per camera, plus realistic `creation_time` tags with a few seconds of clock error.
  - one camera at **30 fps instead of 29.97** and one clip with **no audio**.
- Write `ground_truth.json` (camera of each file, true offsets, true takes).

Automated checks (pytest):
1. Scan: fps prefill = 29.97, mismatch and no-audio warnings raised.
2. Grouping: files grouped into the true cameras (identical iPhones split correctly).
3. Sync: every audio file within **±1 frame (33 ms)** of the true offset; the no-audio
   file placed by metadata and flagged.
4. Auto takes ≈ true takes (±0.5 s).
5. Export: every output of a take has exactly `N` frames and the target fps; rotation
   applied (output dims rotated); the flash frames line up across all cameras of a take
   (±1 frame); pre/post padding works when a camera starts late.
6. Proxies: Range requests return 206.
Also drive the UI once with Playwright (or a quick manual pass): load the session, rotate
a camera (must not trigger any network request or re-encode), scrub, create a take,
and export.

## 10. Milestones (commit after each one, push at the end)

1. Skeleton: FastAPI app, static UI shell, session list/create, scan + probe, Source
   screen with fps check. `run.bat`, `requirements.txt`, `.gitignore`.
2. Process job: thumbnails, features, audio, waveforms, grouping. Cameras screen with
   CSS-only rotation, names, reassignment.
3. Proxies job + Range media serving + timeline rendering + grid scrubbing/playback.
4. Sync job + inspector with sync info, nudging, and re-sync against a chosen file.
5. Takes (manual + auto) + export sidebar + export job with frame-count verification.
6. Synthetic test generator + pytest suite + README (usage, folder layout, how to bring
   exports into FreeMoCap).

Commit messages: short imperative subject; push to `origin master` when milestone 6 passes.

## 11. Edge cases to handle

- iPhone clips at 29.97/30 that are slightly VFR → conformed on export with the `fps` filter.
- 59.94 vs 60 mixes → warning plus conformed; explain the drift in the fps check.
- A Fuji that splits long recordings into several files → just more files on the
  same camera row; sync handles each one.
- A camera that starts after the take start or stops before its end → padding plus a
  coverage warning in the sidebar.
- No continuous camera at all → multi-pass sync still places everything by overlaps
  within each take.
- Music with a repeating beat → possible off-by-one-bar matches. The low `ratio` flags
  it, and "Re-sync against…" / nudging fixes it.
- Files without audio → metadata placement, amber, manual nudge.
- HEVC/MOV sources the browser can't decode → irrelevant, since the preview always uses
  H.264 proxies.
- Huge sources (4K, 10+ minutes) → proxies build in the background, longest last, and
  the UI stays usable throughout.

## 12. FreeMoCap integration

Replaces the old non-goal "no FreeMoCap integration beyond the folder layout". R1–R6 still
apply (no `shell=True`, `CREATE_NO_WINDOW`, never block the UI, never touch `source/`).

### 12.1 Pinned versions (don't upgrade without re-running the self-test)
- FreeMoCap **v1.8.2**, git commit `a1cd374ac7c186d78d0552b8f69bf59be2f4bdcc` (not PyPI, which
  stops at 1.0.28; not `main`, which is the v2 rewrite).
- **Python 3.12** for the FreeMoCap env, installed by uv; never the system Python.
- **opencv-contrib-python==4.8.1.78** and **no opencv-python**: a normal install also pulls
  opencv-python 4.11, which shadows contrib and lacks `cv2.aruco.estimatePoseCharucoBoard`, so
  calibration crashes right after board detection.
- **uv==0.11.32** in the app's own `.venv` (requirements.txt).
- MediaPipe tracking runs on CPU; no CUDA PyTorch. The GPU checks are for ffmpeg only.

### 12.2 Environment (gitignored `.tools/`, per machine)
```
.tools/python/                     uv-managed Python (UV_PYTHON_INSTALL_DIR, set for every uv call)
.tools/uv-cache/                   uv's cache (UV_CACHE_DIR): same drive as the venv, not C:
.tools/gpu_probe.json              GPU decode / NVENC test results (keyed by ffmpeg path+version, driver)
.tools/freemocap-v1.8.2/venv/      FreeMoCap env
.tools/freemocap-v1.8.2/home/      FreeMoCap's home dir (--fmc-home), away from the user's GUI state
.tools/freemocap-v1.8.2/logs/      raw output of every run
.tools/freemocap-v1.8.2/selftest/  self-test data
.tools/freemocap-v1.8.2/state.json installed_at, last check, last self-test
```
The lock `mocap_preprocess/fmc/requirements-freemocap.lock` is committed with its inputs
(`in.txt`, `excludes.txt` = opencv-python, `overrides.txt` = opencv-contrib-python==4.8.1.78) and
regenerated with
`uv pip compile in.txt --python-version 3.12 --python-platform x86_64-pc-windows-msvc
--excludes excludes.txt --overrides overrides.txt -o requirements-freemocap.lock`.

Install job (kind `setup`, no session): preflight (git on PATH, ≥ 6 GB free) → `uv python
install 3.12` → `uv venv --python 3.12` → `uv pip sync` of the lock → `fmc_headless.py check`
must pass. Repair deletes the venv and redoes the last three steps; Remove deletes
`.tools/freemocap-v1.8.2` after a confirm.

### 12.3 Runner: `mocap_preprocess/fmc/fmc_headless.py`
Runs inside the FreeMoCap venv (stdlib + FreeMoCap deps); the app never imports it. Invoked
as `<venv>\Scripts\python.exe -I -u -X utf8 fmc_headless.py --fmc-home <home> <cmd> ...` with
cwd `.tools/freemocap-v1.8.2`; output teed to `logs/<timestamp>_<cmd>.log`, ANSI stripped, cancel
kills the process tree (tracking spawns workers).
- `--fmc-home` sets HOME/USERPROFILE before freemocap is imported (1.8.2 puts freemocap_data/ under
  `Path.home()` and creates its log file on import). Console logging is quieted to WARNING.
- Output: `FMC_EVENT {json}` lines (take_start, take_done, take_failed, calibration_done, warn,
  download, stage — stages from FreeMoCap's log messages: tracking_2d, triangulating, skeleton,
  saving, blender), tqdm bars on stderr (`processing video: <cam>.mp4: NN%|…| n/total`, one per
  camera), and `FMC_RESULT {json}` always last. Exit 0 = all ok.
- `check`: versions; fails unless freemocap is v1.8.2 and `cv2.aruco.estimatePoseCharucoBoard` exists.
- `calibrate <rec>`: `pin_camera_0_to_origin=True` always (1.8.2 raises UnboundLocalError when it and
  `use_charuco_as_groundplane` are both False); the reprojection error is element [0] of
  `CameraGroup.calibrate_videos`' return value; returns `<rec>/<stem>_camera_calibration.toml`.
- `process <takes...> --toml T --processes N`: 1.8.2 pairs videos with calibration cameras by sorted
  position only, so cameras are matched by name (video stem = TOML `name`): unknown camera or fewer
  than 2 → the take fails; a subset → `<take>/<take>_camera_calibration.toml` with only those
  cameras, renumbered in the take's order (+ `metadata.subset_of`); the full set → the TOML is copied
  there unchanged. A take without `output_data/*_body_3d_xyz.npy` afterwards fails.
- `selftest --workdir D`: FreeMoCap's 3-camera sample (7x5 board, 58 mm): calibrate one copy,
  process another; passes when reprojection error < 2 px, NaN fraction < 0.2, median left upper arm
  150–450 mm and left thigh 250–600 mm.

### 12.4 Setup panel (Session screen)
One row per requirement: ffmpeg/ffprobe, NVIDIA GPU (none = neutral), GPU decode and NVENC
(actually encoded/decoded, results cached in `.tools/gpu_probe.json`), git, uv, FreeMoCap
(not installed / installing / ready / broken + Install, Repair, Remove) and its self-test.
`GET /api/setup[?refresh=1]`, `POST /api/setup/freemocap/{install|repair|remove|selftest}`.
A failed GPU-decode or NVENC test disables those options (Process, Export); new sessions
default to h264_nvenc when its test passed.

### 12.5 Mocap step (step 7, after Export)
- Enabled once a take is exported; without a ready FreeMoCap it links to the Setup panel.
- Calibration: an exported take (default: the first whose name contains "calib"), board 7x5 / 5x3,
  square size in mm (required — output units come from it), "board flat on the floor"
  (→ ground plane). Shows the reprojection error (green < 1 px, amber < 2, red), cameras, date.
- Takes: every other exported take with its cameras vs. the calibration (a subset is fine; a camera
  the calibration doesn't know is an error), status (not processed / done / stale / failed), and
  "Process selected" = one job, one subprocess. Leftover videos in `synchronized_videos/` that the
  take's `take_info.json` doesn't list would be fed to FreeMoCap, so processing (and calibrating) is
  blocked until they're deleted: 409 `{message, leftovers}`, then the UI offers to delete them and
  resends with `delete_leftovers: true`.
- After each take `output/<take>/mocap_info.json` records the FreeMoCap version, calibration (TOML,
  error), cameras, subset, processes, time and the size+mtime of every video; a take is **stale**
  when its videos changed (re-exported) or the calibration changed.
- `session.json` gains
  `"mocap": {"calibration": {...}, "options": {"processes", "blender", "blender_exe"}, "takes": {"<id>": {...}}}`.
- FreeMoCap writes `output_data/`, `annotated_videos/`, `<take>_by_frame.csv/json`,
  `<take>_by_trajectory.csv`, `<take>_frame_name_xyz.npy` into the take folder; re-export never
  deletes them.


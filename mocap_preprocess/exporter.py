"""Per-take export (§6.6).

For every selected take and camera: one ``<camera>_synced.mp4`` cut from the
camera's files that overlap the take (joined end to end when it spans several),
conformed to the target fps, padded with black (and silence) where the camera
started late, stopped early or wasn't recording in between, with the camera's
user rotation baked in — the only place pixels are rotated (R1) — and exactly
``N = round(duration × fps)`` frames (R3).
"""
from __future__ import annotations

import json
import math
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path

from . import ffmpeg_tools as ff
from .jobs import Job
from .session_store import SessionStore, now, sanitize_filename, write_json_atomic

ROTATE = {90: "transpose=1", 180: "transpose=1,transpose=1", 270: "transpose=2"}
MAX_PARALLEL = 3
NVENC_PRESETS = ("p1", "p2", "p3", "p4", "p5", "p6", "p7")


@dataclass
class Segment:
    file: dict
    seek: float           # input -ss (file time)
    gap: int              # black frames before it (since the previous segment)
    frames: int           # output frames taken from the file
    start: float = 0.0    # where the file starts, in take time (exact; the frames are rounded)


@dataclass
class CamPlan:
    camera: dict
    file: dict            # the file covering most of the take
    name: str             # sanitized camera name used in the file name
    seek: float           # input -ss (file time) of a single-file plan
    pre: float            # black before the footage starts
    post: float           # black after it ends
    segments: list = field(default_factory=list)   # every file used, in order
    out: Path = None
    frames: int | None = None
    status: str = "pending"
    notes: list = field(default_factory=list)


def scale_filter(long_side, cuda: bool = False) -> str:
    """Downscale-only fit of the long side; dimensions kept even for yuv420p.
    ``cuda``: the same on the GPU, also converting to 8-bit nv12 there."""
    if not long_side:
        dims = "w=trunc(iw/2)*2:h=trunc(ih/2)*2"
    else:
        L = int(long_side)
        dims = f"w='if(gte(iw,ih),min({L},trunc(iw/2)*2),-2)':h='if(gte(iw,ih),-2,min({L},trunc(ih/2)*2))'"
    return f"scale_cuda={dims}:format=nv12" if cuda else f"scale={dims}"


def target_fraction(data: dict) -> Fraction:
    f = ff.parse_fraction(data["settings"]["target_fps"])
    if f is None:
        raise ValueError(f"invalid target fps {data['settings']['target_fps']!r}")
    return f


def frame_count(take: dict, fps: Fraction) -> int:
    return int(round((take["end"] - take["start"]) * float(fps)))


def _file_duration(f: dict) -> float:
    v = f["probe"].get("video") or {}
    return float(v.get("duration") or f["probe"].get("duration") or 0.0)


def plan_segments(take: dict, files: list[dict], fps: Fraction) -> list[Segment]:
    """A camera's files laid end to end over the take, in output frames: each one
    starts at its own offset, black fills the time before / between them. A file
    overlapping the previous one starts where that one ends."""
    F = float(fps)
    n = frame_count(take, fps)
    segs, pos = [], 0
    for f in sorted(files, key=lambda f: f["offset"]):
        s = f["offset"] - take["start"]            # file start, in take time
        a = max(pos, round(s * F))
        b = min(n, round((s + _file_duration(f)) * F))
        if b - a < 1:
            continue
        segs.append(Segment(file=f, seek=max(0.0, a / F - s), gap=a - pos, frames=b - a, start=s))
        pos = b
    return segs


def plan_take(data: dict, take: dict, camera_ids: list[str], fps: Fraction | None = None) -> tuple[list[CamPlan], list[dict]]:
    fps = fps or target_fraction(data)
    cams = {c["id"]: c for c in data["cameras"]}
    files = [f for f in data["files"] if f.get("probe") and f["probe"].get("video") and f.get("offset") is not None]
    plans, skipped, used = [], [], set()
    for cid in camera_ids:
        cam = cams.get(cid)
        if cam is None:
            continue
        segs = plan_segments(take, [f for f in files if f.get("camera") == cid], fps)
        if not segs:
            skipped.append({"camera_id": cid, "name": cam["name"], "reason": "no footage overlaps this take"})
            continue
        name = sanitize_filename(cam["name"], cid)
        base, k = name, 2
        while name.lower() in used:
            name = f"{base}_{k}"
            k += 1
        used.add(name.lower())
        duration = take["end"] - take["start"]
        if len(segs) == 1:
            best = segs[0].file
            in_t = take["start"] - best["offset"]
            pre = max(0.0, -in_t)
            seek = max(0.0, in_t)
            post = max(0.0, duration - pre - (_file_duration(best) - seek))
        else:
            best = max(segs, key=lambda sg: sg.frames).file
            seek = segs[0].seek
            pre = segs[0].gap / float(fps)
            post = (frame_count(take, fps) - sum(sg.gap + sg.frames for sg in segs)) / float(fps)
        plans.append(CamPlan(camera=cam, file=best, name=name, seek=seek, pre=pre, post=post, segments=segs))
    return plans, skipped


def build_command(src: Path, out: Path, plan: CamPlan, fps: Fraction, n_frames: int, options: dict,
                  hwaccel: bool) -> list:
    fps_s = ff.frac_str(fps)
    nvenc = options.get("encoder") == "h264_nvenc"
    margin = max(1.0 / float(fps), 0.25)    # pad past the end; -frames:v caps the count
    args: list = ["ffmpeg", "-y", "-v", "error"]
    if hwaccel:
        # decode, fps and scale/8-bit stay on the GPU; frames only come down to system
        # memory for what this build can't do there (rotation, black padding) or for libx264
        args += ["-noautorotate", "-hwaccel", "cuda", "-hwaccel_output_format", "cuda"]
    seek = ["-ss", f"{plan.seek:.6f}"] if plan.seek > 0 else []
    args += seek + ["-i", src]
    with_audio = bool(options.get("audio", True) and plan.file["probe"].get("audio"))
    if with_audio:
        # the audio comes from a second, independent read of the same file: with one
        # demuxer feeding both, a long black lead-in (adelay + tpad of tens of seconds)
        # can deadlock ffmpeg's scheduler before the first video frame (seen with 8.0)
        args += seek + ["-vn", "-i", src]
    args += ["-map", "0:v:0"]
    if hwaccel:
        meta = int((plan.file["probe"].get("video") or {}).get("rotation_meta") or 0)
        rot = (meta + int(plan.camera.get("rotation") or 0)) % 360
        vf = [f"fps={fps_s}:start_time=0", scale_filter(options.get("long_side"), cuda=True)]
        black_end = plan.post > 0.5 / float(fps)
        # one tpad for both ends: two in a row after fps drop the end padding
        pad = [f"start_duration={plan.pre:.6f}"] if plan.pre > 0 else []
        pad += [f"stop_duration={plan.post + margin:.6f}"] if black_end else ["stop_mode=clone", f"stop_duration={margin:.6f}"]
        if plan.pre > 0 or black_end or rot in ROTATE or not nvenc:
            vf += ["hwdownload", "format=nv12"] + ([ROTATE[rot]] if rot in ROTATE else []) + ["tpad=" + ":".join(pad + ["color=black"])]
        else:   # all on the GPU, straight into NVENC (cloning the last frame works on CUDA frames)
            vf.append(f"tpad=stop_mode=clone:stop_duration={margin:.6f}")
    else:
        vf = []
        if plan.pre > 0:
            vf.append(f"tpad=start_duration={plan.pre:.6f}:color=black")
        vf.append(f"fps={fps_s}:start_time=0")
        rot = int(plan.camera.get("rotation") or 0)
        if rot in ROTATE:
            vf.append(ROTATE[rot])
        vf.append(scale_filter(options.get("long_side")))
        # pad whatever the file is missing at the end, plus a margin
        vf.append(f"tpad=stop_duration={plan.post + margin:.6f}:color=black")
        vf.append("format=yuv420p")
    args += ["-vf", ",".join(vf), "-frames:v", str(n_frames)]
    crf = int(options.get("crf", 17))
    if nvenc:
        args += ["-c:v", "h264_nvenc", "-preset", options.get("nvenc_preset", "p5"), "-rc", "vbr",
                 "-cq", str(crf + 2), "-b:v", "0"]
    else:
        args += ["-c:v", "libx264", "-preset", "medium", "-crf", str(crf)]
    if with_audio:
        delay = int(round(plan.pre * 1000))
        args += ["-map", "1:a:0", "-af", f"adelay={delay}:all=1,apad", "-c:a", "aac", "-b:a", "160k",
                 "-t", f"{n_frames / float(fps):.6f}"]
    else:
        args += ["-an"]
    args += ["-movflags", "+faststart", "-progress", "pipe:1", "-nostats", out]
    return args


def build_joined_command(srcs: list[Path], out: Path, plan: CamPlan, fps: Fraction, n_frames: int, options: dict,
                         hwaccel: bool) -> list:
    """A camera that recorded the take in several files: each file gets its own
    chain (fps, scale, rotation, then black for the break before it, counted in
    frames) and the chains are joined with concat. Audio likewise, with silence
    for the breaks and for files without sound."""
    fps_s = ff.frac_str(fps)
    F = float(fps)
    nvenc = options.get("encoder") == "h264_nvenc"
    margin = max(1, math.ceil(0.25 * F))     # extra frames past each file's end
    segs = plan.segments
    tail = n_frames - sum(sg.gap + sg.frames for sg in segs)
    audio = [sg.file["probe"].get("audio") for sg in segs]
    with_audio = bool(options.get("audio", True) and any(audio))
    args: list = ["ffmpeg", "-y", "-v", "error"]
    for sg, src in zip(segs, srcs):
        if hwaccel:
            args += ["-noautorotate", "-hwaccel", "cuda", "-hwaccel_output_format", "cuda"]
        args += (["-ss", f"{sg.seek:.6f}"] if sg.seek > 0 else []) + ["-i", src]
    # audio through inputs of their own, as in build_command, placed at the file's
    # exact start rather than on the frame grid: [chain start, file start] is silence
    audio_in, chain_at, pos = {}, [], 0
    for sg in segs:
        chain_at.append(pos / F)
        pos += sg.gap + sg.frames
    if with_audio:
        for k, (sg, src) in enumerate(zip(segs, srcs)):
            if audio[k]:
                audio_in[k] = len(segs) + len(audio_in)
                a_seek = max(0.0, chain_at[k] - sg.start)
                args += (["-ss", f"{a_seek:.6f}"] if a_seek > 0 else []) + ["-vn", "-i", src]
        first = next(a for a in audio if a)
        rate = int(first.get("sample_rate") or 48000)
        layout = "mono" if max(int(a.get("channels") or 2) for a in audio if a) == 1 else "stereo"
        aformat = f"aformat=sample_fmts=fltp:sample_rates={rate}:channel_layouts={layout}"
    graph, vs, als = [], "", ""
    for k, sg in enumerate(segs):
        last = k == len(segs) - 1
        keep = sg.gap + sg.frames + (tail if last else 0)
        vf = [f"fps={fps_s}:start_time=0"]
        if hwaccel:
            meta = int((sg.file["probe"].get("video") or {}).get("rotation_meta") or 0)
            rot = (meta + int(plan.camera.get("rotation") or 0)) % 360
            vf += [scale_filter(options.get("long_side"), cuda=True), "hwdownload", "format=nv12"]
            vf += [ROTATE[rot]] if rot in ROTATE else []
        else:
            rot = int(plan.camera.get("rotation") or 0)
            vf += ([ROTATE[rot]] if rot in ROTATE else []) + [scale_filter(options.get("long_side"))]
        # one tpad per chain (two in a row after fps lose the end padding)
        pad = [f"start={sg.gap}"] if sg.gap else []
        pad += [f"stop={tail + margin}"] if last and tail > 0 else ["stop_mode=clone", f"stop={margin}"]
        vf += ["tpad=" + ":".join(pad + ["color=black"]), f"trim=end_frame={keep}", "setpts=PTS-STARTPTS"]
        graph.append(f"[{k}:v:0]" + ",".join(vf) + f"[v{k}]")
        vs += f"[v{k}]"
        if with_audio:
            if k in audio_in:
                delay = round(max(0.0, sg.start - chain_at[k]) * rate)
                af = [f"[{audio_in[k]}:a:0]aresample={rate}:async=1:first_pts=0", f"adelay={delay}S:all=1"]
            else:
                af = [f"anullsrc=r={rate}:cl={layout}"]
            graph.append(",".join(af + ["apad", f"atrim=duration={keep / F:.6f}", "asetpts=PTS-STARTPTS", aformat]) + f"[a{k}]")
            als += f"[a{k}]"
    graph.append(f"{vs}concat=n={len(segs)}:v=1:a=0" + ("[v]" if hwaccel else ",format=yuv420p[v]"))
    if with_audio:
        graph.append(f"{als}concat=n={len(segs)}:v=0:a=1[a]")
    args += ["-filter_complex", ";".join(graph), "-map", "[v]", "-frames:v", str(n_frames)]
    crf = int(options.get("crf", 17))
    if nvenc:
        args += ["-c:v", "h264_nvenc", "-preset", options.get("nvenc_preset", "p5"), "-rc", "vbr",
                 "-cq", str(crf + 2), "-b:v", "0"]
    else:
        args += ["-c:v", "libx264", "-preset", "medium", "-crf", str(crf)]
    if with_audio:
        args += ["-map", "[a]", "-c:a", "aac", "-b:a", "160k", "-t", f"{n_frames / F:.6f}"]
    else:
        args += ["-an"]
    args += ["-movflags", "+faststart", "-progress", "pipe:1", "-nostats", out]
    return args


def normalize_options(options: dict, defaults: dict) -> dict:
    o = dict(defaults)
    o.update({k: v for k, v in (options or {}).items() if v is not None or k == "long_side"})
    if o.get("layout") not in ("freemocap", "flat"):
        o["layout"] = "freemocap"
    if o.get("encoder") not in ("libx264", "h264_nvenc"):
        o["encoder"] = "libx264"
    try:
        o["crf"] = max(0, min(51, int(o.get("crf", 17))))
    except (TypeError, ValueError):
        o["crf"] = 17
    ls = o.get("long_side")
    o["long_side"] = int(ls) if ls not in (None, "", "keep", 0, "0") else 0
    o["audio"] = bool(o.get("audio", True))
    o["overwrite"] = bool(o.get("overwrite", True))
    if o.get("nvenc_preset") not in NVENC_PRESETS:
        o["nvenc_preset"] = "p5"
    try:
        o["parallel"] = max(1, min(MAX_PARALLEL, int(o.get("parallel") or 1)))
    except (TypeError, ValueError):
        o["parallel"] = 1
    if "camera_ids" in o and not isinstance(o["camera_ids"], list):
        o.pop("camera_ids")
    return o


def export(store: SessionStore, name: str, job: Job, take_ids: list[str], camera_ids: list[str],
           options: dict) -> dict:
    paths = store.paths(name)
    data = store.load(name)
    fps = target_fraction(data)
    hw = bool(data["settings"].get("hwaccel"))
    takes = [t for t in data["takes"] if t["id"] in set(take_ids)]
    if not takes:
        raise RuntimeError("no takes selected")
    if not camera_ids:
        raise RuntimeError("no cameras selected")

    work = []
    used_dirs = set()
    for tk in takes:
        plans, skipped = plan_take(data, tk, camera_ids, fps)
        dname = sanitize_filename(tk["name"], tk["id"])
        base, k = dname, 2
        while dname.lower() in used_dirs:
            dname = f"{base}_{k}"
            k += 1
        used_dirs.add(dname.lower())
        work.append((tk, dname, plans, skipped))
    total = max(1, sum(len(p) for _, _, p, _ in work))
    parallel = options.get("parallel", 1)
    job.log(f"exporting {len(takes)} take(s) × {len(camera_ids)} camera(s) at {ff.fps_label(fps)} fps "
            f"({options['encoder']}{' ' + options['nvenc_preset'] if options['encoder'] == 'h264_nvenc' else ''}, "
            f"{'crf' if options['encoder'] == 'libx264' else 'cq'} "
            f"{options['crf'] + (2 if options['encoder'] == 'h264_nvenc' else 0)}, "
            f"{options['long_side'] or 'source'} long side, layout {options['layout']}"
            f"{', GPU decode/scale' if hw else ''}"
            f"{f', {parallel} files at once' if parallel > 1 else ''})")

    count = {"done": 0, "written": 0, "errors": 0}
    skipped_n = 0
    units = []                      # (take, plan, frames) still to encode, in take order
    for tk, dname, plans, skipped in work:
        n = frame_count(tk, fps)
        take_dir = paths.output / dname
        vid_dir = take_dir / "synchronized_videos" if options["layout"] == "freemocap" else take_dir
        vid_dir.mkdir(parents=True, exist_ok=True)
        for s in skipped:
            job.log(f"{tk['name']}: {s['name']} skipped — {s['reason']}", "warn")
            skipped_n += 1
        for plan in plans:
            plan.out = vid_dir / f"{plan.name}_synced.mp4"
            if len(plan.segments) > 1:
                breaks = sum(sg.gap for sg in plan.segments[1:]) / float(fps)
                plan.notes.append(f"joined from {len(plan.segments)} files, {breaks:.3f}s black between")
                job.log(f"{tk['name']}: {plan.name} joined from {len(plan.segments)} files ({breaks:.2f} s black between)")
            if max(plan.pre, plan.post) > 0.5 / float(fps):
                plan.notes.append(f"padded {plan.pre:.3f}s before, {plan.post:.3f}s after")
                job.log(f"{tk['name']}: {plan.name} padded with {plan.pre:.2f} s black before / {plan.post:.2f} s after", "warn")
            if plan.out.exists() and not options["overwrite"]:
                plan.status = "kept"
                job.log(f"{plan.out.name} exists, kept (overwrite off)")
                count["done"] += 1
                continue
            units.append((tk, plan, n))

    lock = threading.Lock()
    running: dict[int, list] = {}   # id(plan) -> [label, fraction]

    def show():
        with lock:
            frac = (count["done"] + sum(f for _, f in running.values())) / total
            labels = [lab for lab, _ in running.values()]
            pos = min(total, count["done"] + len(labels))
        if labels:
            msg = labels[0] if len(labels) == 1 else f"{len(labels)} at once: " + ", ".join(labels)
            job.set_progress(frac, f"{msg} ({pos}/{total})")
        else:
            job.set_progress(frac)

    def encode(unit) -> None:
        tk, plan, n = unit
        job.check_cancel()
        key = id(plan)
        with lock:
            running[key] = [f"{tk['name']} · {plan.name}", 0.0]
        show()

        def prog(x):
            with lock:
                running[key][1] = x
            show()
        try:
            srcs = [paths.source_file(sg.file["rel"]) for sg in plan.segments]
            ok = encode_one(tk, plan, n, srcs, fps, options, hw, prog, job)
        finally:
            with lock:
                running.pop(key, None)
                count["done"] += 1
        with lock:
            count["written" if ok else "errors"] += 1
        show()

    # one ffmpeg per file; with `parallel` > 1 several run at once, across takes
    with ThreadPoolExecutor(max_workers=parallel) as pool:
        futs = [pool.submit(encode, u) for u in units]
        try:
            for fut in as_completed(futs):
                fut.result()
        except BaseException:
            job.cancel_event.set()      # stop the siblings too: their ffmpegs get killed
            for f in futs:
                f.cancel()
            raise
    written, errors = count["written"], count["errors"]

    report = []
    for tk, dname, plans, skipped in work:
        n = frame_count(tk, fps)
        take_dir = paths.output / dname
        # verify: every output of the take has exactly N frames (R3)
        for plan in plans:
            if plan.out is None or not plan.out.exists():
                continue
            plan.frames = ff.count_video_frames(plan.out)
            if plan.frames != n:
                errors += 1
                job.log(f"{tk['name']}/{plan.out.name}: {plan.frames} frames, expected {n}", "error")
        if plans and all(p.frames == n for p in plans if p.out is not None and p.out.exists()):
            job.log(f"{tk['name']}: verified {len(plans)} file(s) × {n} frames")

        info = {
            "take": tk["name"], "id": tk["id"], "start": tk["start"], "end": tk["end"],
            "duration": n / float(fps), "fps": ff.frac_str(fps), "fps_label": ff.fps_label(fps), "frames": n,
            "layout": options["layout"], "exported_at": now(),
            "options": {k: options[k] for k in ("encoder", "nvenc_preset", "crf", "long_side", "audio")},
            "cameras": [{
                "camera_id": p.camera["id"], "name": p.camera["name"],
                "output": p.out.relative_to(take_dir).as_posix() if p.out else None,
                "source": p.file["rel"], "offset": p.file["offset"], "sync": p.file.get("sync"),
                "sources": [{"source": sg.file["rel"], "offset": sg.file["offset"], "sync": sg.file.get("sync"),
                             "seek": round(sg.seek, 6), "gap_frames": sg.gap, "frames": sg.frames}
                            for sg in p.segments],
                "rotation": int(p.camera.get("rotation") or 0), "seek": round(p.seek, 6),
                "pre_pad": round(p.pre, 6), "post_pad": round(p.post, 6),
                "frames": p.frames, "status": p.status,
            } for p in plans],
            "skipped": skipped,
        }
        write_json_atomic(take_dir / "take_info.json", info)
        report.append({"take": tk["name"], "dir": dname, "frames": n,
                       "cameras": len(plans), "skipped": len(skipped)})

    with store.edit(name) as d:
        d["state"]["exported_at"] = now()
    job.set_progress(1.0, f"{written} written" + (f", {errors} error(s)" if errors else ""))
    job.log(f"done: {written} file(s) written, {skipped_n} camera(s) skipped, {errors} error(s)",
            "error" if errors else "info")
    return {"written": written, "skipped": skipped_n, "errors": errors, "takes": report,
            "output": str(paths.output)}


def encode_one(tk: dict, plan: CamPlan, n: int, srcs: list[Path], fps: Fraction, options: dict, hw: bool,
               prog, job: Job) -> bool:
    """Encode one camera of one take into a temp file, then move it into place.
    ``srcs``: the source path of each of the plan's segments."""
    tmp = plan.out.with_name(plan.out.stem + ".tmp.mp4")
    ok, err = False, ""
    for use_hw in ([True, False] if hw else [False]):
        if len(plan.segments) > 1:
            args = build_joined_command(srcs, tmp, plan, fps, n, options, use_hw)
        else:
            args = build_command(srcs[0], tmp, plan, fps, n, options, use_hw)
        t0 = time.time()
        try:
            rc, err = ff.run_progress(args, n / float(fps), prog, job.cancel_event)
        except ff.Cancelled:
            tmp.unlink(missing_ok=True)
            raise
        if rc == 0 and tmp.exists():
            ok = True
            job.log(f"{tk['name']}/{plan.out.name}: encoded in {time.time() - t0:.1f}s")
            break
        if use_hw:
            job.log(f"{plan.out.name}: failed with -hwaccel cuda, retrying on CPU", "warn")
    if not ok:
        tmp.unlink(missing_ok=True)
        plan.status = "error"
        job.log(f"{tk['name']}/{plan.out.name}: ffmpeg failed: {err[-400:]}", "error")
        return False
    try:
        ff.replace_with_retry(tmp, plan.out)
    except OSError as e:  # e.g. the old file is open in a player
        tmp.unlink(missing_ok=True)
        plan.status = "error"
        job.log(f"{tk['name']}/{plan.out.name}: can't replace the existing file ({e}) — is it open somewhere?", "error")
        return False
    plan.status = "written"
    return True


def read_take_info(path: Path) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)

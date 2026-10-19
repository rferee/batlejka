"""Per-take export (§6.6).

For every selected take and camera: one ``<camera>_synced.mp4`` cut from the
camera's best-covering file, conformed to the target fps, padded with black
(and silence) where the camera started late or stopped early, with the
camera's user rotation baked in — the only place pixels are rotated (R1) —
and exactly ``N = round(duration × fps)`` frames (R3).
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path

from . import ffmpeg_tools as ff
from .jobs import Job
from .session_store import SessionStore, now, sanitize_filename, write_json_atomic

ROTATE = {90: "transpose=1", 180: "transpose=1,transpose=1", 270: "transpose=2"}


@dataclass
class CamPlan:
    camera: dict
    file: dict
    name: str             # sanitized camera name used in the file name
    seek: float           # input -ss (file time)
    pre: float            # black before the file starts
    post: float           # what the file is missing at the end
    out: Path = None
    frames: int | None = None
    status: str = "pending"
    notes: list = field(default_factory=list)


def scale_filter(long_side) -> str:
    """Downscale-only fit of the long side; dimensions kept even for yuv420p."""
    if not long_side:
        return "scale=trunc(iw/2)*2:trunc(ih/2)*2"
    L = int(long_side)
    return (f"scale=w='if(gte(iw,ih),min({L},trunc(iw/2)*2),-2)'"
            f":h='if(gte(iw,ih),-2,min({L},trunc(ih/2)*2))'")


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


def plan_take(data: dict, take: dict, camera_ids: list[str]) -> tuple[list[CamPlan], list[dict]]:
    cams = {c["id"]: c for c in data["cameras"]}
    files = [f for f in data["files"] if f.get("probe") and f["probe"].get("video") and f.get("offset") is not None]
    plans, skipped, used = [], [], set()
    for cid in camera_ids:
        cam = cams.get(cid)
        if cam is None:
            continue
        best, best_ov = None, 0.0
        for f in files:
            if f.get("camera") != cid:
                continue
            ov = min(take["end"], f["offset"] + _file_duration(f)) - max(take["start"], f["offset"])
            if ov > best_ov:
                best, best_ov = f, ov
        if best is None:
            skipped.append({"camera_id": cid, "name": cam["name"], "reason": "no footage overlaps this take"})
            continue
        name = sanitize_filename(cam["name"], cid)
        base, k = name, 2
        while name.lower() in used:
            name = f"{base}_{k}"
            k += 1
        used.add(name.lower())
        in_t = take["start"] - best["offset"]
        pre = max(0.0, -in_t)
        seek = max(0.0, in_t)
        duration = take["end"] - take["start"]
        post = max(0.0, duration - pre - (_file_duration(best) - seek))
        plans.append(CamPlan(camera=cam, file=best, name=name, seek=seek, pre=pre, post=post))
    return plans, skipped


def build_command(src: Path, out: Path, plan: CamPlan, fps: Fraction, n_frames: int, options: dict,
                  hwaccel: bool) -> list:
    fps_s = ff.frac_str(fps)
    args: list = ["ffmpeg", "-y", "-v", "error"]
    if hwaccel:
        args += ["-hwaccel", "cuda"]
    if plan.seek > 0:
        args += ["-ss", f"{plan.seek:.6f}"]
    args += ["-i", src, "-map", "0:v:0"]
    vf = []
    if plan.pre > 0:
        vf.append(f"tpad=start_duration={plan.pre:.6f}:color=black")
    vf.append(f"fps={fps_s}:start_time=0")
    rot = int(plan.camera.get("rotation") or 0)
    if rot in ROTATE:
        vf.append(ROTATE[rot])
    vf.append(scale_filter(options.get("long_side")))
    # pad whatever the file is missing at the end, plus a margin; -frames:v caps the count
    vf.append(f"tpad=stop_duration={plan.post + max(1.0 / float(fps), 0.25):.6f}:color=black")
    vf.append("format=yuv420p")
    args += ["-vf", ",".join(vf), "-frames:v", str(n_frames)]
    crf = int(options.get("crf", 17))
    if options.get("encoder") == "h264_nvenc":
        args += ["-c:v", "h264_nvenc", "-preset", "p5", "-rc", "vbr", "-cq", str(crf + 2), "-b:v", "0"]
    else:
        args += ["-c:v", "libx264", "-preset", "medium", "-crf", str(crf)]
    if options.get("audio", True) and plan.file["probe"].get("audio"):
        delay = int(round(plan.pre * 1000))
        args += ["-map", "0:a:0", "-af", f"adelay={delay}:all=1,apad", "-c:a", "aac", "-b:a", "160k",
                 "-t", f"{n_frames / float(fps):.6f}"]
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
        plans, skipped = plan_take(data, tk, camera_ids)
        dname = sanitize_filename(tk["name"], tk["id"])
        base, k = dname, 2
        while dname.lower() in used_dirs:
            dname = f"{base}_{k}"
            k += 1
        used_dirs.add(dname.lower())
        work.append((tk, dname, plans, skipped))
    units = max(1, sum(len(p) for _, _, p, _ in work))
    job.log(f"exporting {len(takes)} take(s) × {len(camera_ids)} camera(s) at {ff.fps_label(fps)} fps "
            f"({options['encoder']}, {'crf' if options['encoder'] == 'libx264' else 'cq'} "
            f"{options['crf'] + (2 if options['encoder'] == 'h264_nvenc' else 0)}, "
            f"{options['long_side'] or 'source'} long side, layout {options['layout']})")

    done = written = errors = skipped_n = 0
    report = []
    for tk, dname, plans, skipped in work:
        n = frame_count(tk, fps)
        take_dir = paths.output / dname
        vid_dir = take_dir / "synchronized_videos" if options["layout"] == "freemocap" else take_dir
        vid_dir.mkdir(parents=True, exist_ok=True)
        for s in skipped:
            job.log(f"{tk['name']}: {s['name']} skipped — {s['reason']}", "warn")
            skipped_n += 1
        for plan in plans:
            job.check_cancel()
            plan.out = vid_dir / f"{plan.name}_synced.mp4"
            label = f"{tk['name']} · {plan.name} ({done + 1}/{units})"
            job.set_progress(done / units, label)
            if max(plan.pre, plan.post) > 0.5 / float(fps):
                plan.notes.append(f"padded {plan.pre:.3f}s before, {plan.post:.3f}s after")
                job.log(f"{tk['name']}: {plan.name} padded with {plan.pre:.2f} s black before / {plan.post:.2f} s after", "warn")
            if plan.out.exists() and not options["overwrite"]:
                plan.status = "kept"
                job.log(f"{plan.out.name} exists, kept (overwrite off)")
                done += 1
                continue
            tmp = plan.out.with_name(plan.out.stem + ".tmp.mp4")
            src = paths.source_file(plan.file["rel"])

            def prog(x, base=done, label=label):
                job.set_progress((base + x) / units, label)

            ok, err = False, ""
            for use_hw in ([True, False] if hw else [False]):
                args = build_command(src, tmp, plan, fps, n, options, use_hw)
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
                errors += 1
                job.log(f"{tk['name']}/{plan.out.name}: ffmpeg failed: {err[-400:]}", "error")
                done += 1
                continue
            ff.replace_with_retry(tmp, plan.out)
            plan.status = "written"
            written += 1
            done += 1

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
            "options": {k: options[k] for k in ("encoder", "crf", "long_side", "audio")},
            "cameras": [{
                "camera_id": p.camera["id"], "name": p.camera["name"],
                "output": p.out.relative_to(take_dir).as_posix() if p.out else None,
                "source": p.file["rel"], "offset": p.file["offset"], "sync": p.file.get("sync"),
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


def read_take_info(path: Path) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)

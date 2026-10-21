"""
fmc_headless.py - run FreeMoCap 1.8.2 calibration and motion capture with no GUI.

This file runs INSIDE the FreeMoCap virtualenv (its own interpreter), never inside
the Batlejka app. It only uses the stdlib plus what FreeMoCap installs.

    <fmc-venv>/python -I fmc_headless.py [--fmc-home DIR] [--verbose] <command> ...

Commands
    check                                   versions + OpenCV sanity check
    calibrate <calib_rec> --square-mm 39 [--board 7x5|5x3] [--groundplane]
    process   <take> [<take> ...] --toml <file.toml> [--processes N] [--params p.json] [--blender ...]
    all       <calib_rec> <take> [<take> ...] --square-mm 39 [...calibrate and process options]
    selftest  --workdir DIR [--processes N]  downloads FreeMoCap's 3-camera test
                                             recording (~38 MB), calibrates, processes,
                                             sanity-checks the skeleton

Folder contract (calibration recording and takes alike)
    <recording>/synchronized_videos/<cam>.mp4   .mp4 only, identical frame counts.
    Camera = file stem. Takes may use a SUBSET of the calibration's cameras (a
    matching sub-calibration is written for them); a camera the calibration doesn't
    know is an error.

Output protocol (stdout)
    FMC_EVENT {json}    progress events, see emit_event() calls
    FMC_RESULT {json}   always the last line; {"ok": bool, ...}
    Lines starting with "[ok]", "[warn]", "[calib]" are human-readable status.
    tqdm progress bars ("processing video: <name>.mp4:  42%|...| 93/222 [...]") go to
    stderr. Exit code 0 = success, 1 = failure (including any failed take).

--fmc-home DIR points FreeMoCap's home directory at DIR, so its freemocap_data/
(logs, calibrations, last_successful_calibration.toml) lands there instead of in
the user's real home and never touches their FreeMoCap GUI state.
"""

import argparse
import json
import logging
import os
import platform
import shutil
import sys
import time
import traceback
from pathlib import Path

EXPECTED_FREEMOCAP_VERSION = "v1.8.2"
VIDEO_FOLDER_NAME = "synchronized_videos"
SAMPLE_DATA_URL = (
    "https://github.com/freemocap/skellysamples/releases/download/"
    "test_data_v06_09_25/freemocap_test_data.zip"
)
SAMPLE_SQUARE_MM = 58  # from the sample's charuco_board_info.json (7x5 board)

# FreeMoCap 1.8.2 log messages -> stage names (pinned version, so these are stable).
STAGE_MESSAGES = [
    ("Detecting 2d skeletons", "tracking_2d"),
    ("Skipping 3d triangulation", "triangulating"),
    ("Triangulating 3d skeletons", "triangulating"),
    ("Creating skeleton model", "skeleton"),
    ("Saving post-processed data", "saving"),
    ("Exporting to", "blender"),
]


# --------------------------------------------------------------------------- output

def emit_event(event_type: str, **fields) -> None:
    print("FMC_EVENT " + json.dumps({"type": event_type, **fields}, default=str), flush=True)


def emit_result(ok: bool, **fields) -> None:
    print("FMC_RESULT " + json.dumps({"ok": ok, **fields}, default=str), flush=True)


def say(msg: str) -> None:
    print(msg, flush=True)


class StageHandler(logging.Handler):
    """Turns FreeMoCap's own log lines into FMC_EVENT stage events."""

    def __init__(self):
        super().__init__(logging.INFO)
        self.take = None
        self.last = None

    def reset(self, take):
        self.take, self.last = take, None

    def emit(self, record):
        try:
            msg = record.getMessage()
        except Exception:  # noqa: BLE001
            return
        for prefix, stage in STAGE_MESSAGES:
            if msg.startswith(prefix):
                if stage != self.last:
                    self.last = stage
                    emit_event("stage", scope="take", take=self.take, stage=stage)
                return


STAGES = StageHandler()


# --------------------------------------------------------------------------- setup

def isolate_home(path: str) -> Path:
    """Must run before freemocap is imported (it creates its log file on import)."""
    home = Path(path).expanduser().resolve()
    home.mkdir(parents=True, exist_ok=True)
    os.environ["HOME"] = str(home)
    if os.name == "nt":
        os.environ["USERPROFILE"] = str(home)  # what Path.home() reads on Windows
    # skellytracker imports ultralytics, which otherwise falls back to a settings dir next to the cwd
    os.environ.setdefault("YOLO_CONFIG_DIR", str(home / "Ultralytics"))
    return home


def check_opencv() -> str:
    """A plain `pip install freemocap` also drags in `opencv-python` (4.11+), which
    shadows the pinned `opencv-contrib-python==4.8.*`. Newer OpenCV removed
    cv2.aruco.estimatePoseCharucoBoard, so calibration dies after detecting the board
    in every video. Catch that up front."""
    import cv2

    if not hasattr(cv2, "aruco") or not hasattr(cv2.aruco, "estimatePoseCharucoBoard"):
        raise RuntimeError(
            f"OpenCV {cv2.__version__} lacks cv2.aruco.estimatePoseCharucoBoard (calibration needs it). "
            "Reinstall the FreeMoCap env from the lock file, or: pip uninstall -y opencv-python "
            'opencv-contrib-python && pip install "opencv-contrib-python==4.8.1.78"'
        )
    return cv2.__version__


def load_freemocap(verbose: bool) -> None:
    """Import the heavy modules once, then quiet FreeMoCap's TRACE-level console logging
    (its log file keeps everything) and hook up stage events."""
    import freemocap  # noqa: F401  (configures logging, creates its log file)
    from freemocap.core_processes.capture_volume_calibration import run_anipose_capture_volume_calibration  # noqa: F401
    from freemocap.core_processes.process_motion_capture_videos import process_recording_headless  # noqa: F401

    root = logging.getLogger()
    if not verbose:
        for handler in root.handlers:
            if isinstance(handler, logging.StreamHandler) and not isinstance(handler, logging.FileHandler):
                handler.setLevel(logging.WARNING)
    root.addHandler(STAGES)


# --------------------------------------------------------------------------- recordings

def list_videos(recording: Path) -> list:
    """Mirror freemocap 1.8.2's get_video_paths: *.mp4 / *.MP4, sorted case-insensitively."""
    folder = recording / VIDEO_FOLDER_NAME
    if not folder.is_dir():
        raise FileNotFoundError(f"No '{VIDEO_FOLDER_NAME}' folder in {recording}")
    videos = {p.resolve() for p in list(folder.glob("*.mp4")) + list(folder.glob("*.MP4"))}
    videos = sorted(videos, key=lambda p: str(p).lower())
    others = [p.name for p in folder.iterdir() if p.is_file() and p.suffix.lower() != ".mp4"]
    if others:
        say(f"[warn] {recording.name}: ignored non-mp4 files (FreeMoCap 1.x only reads .mp4): {others}")
    if not videos:
        raise FileNotFoundError(f"No .mp4 videos in {folder}")
    return videos


def check_recording(recording: Path) -> list:
    """Fail early on what the headless path doesn't check: unreadable files and
    unequal frame counts. Returns camera names (file stems) in FreeMoCap's order."""
    import cv2

    videos = list_videos(recording)
    stats = {}
    for v in videos:
        cap = cv2.VideoCapture(str(v))
        if not cap.isOpened():
            raise RuntimeError(f"OpenCV can't open {v}")
        stats[v.stem] = (int(cap.get(cv2.CAP_PROP_FRAME_COUNT)), round(cap.get(cv2.CAP_PROP_FPS), 3))
        cap.release()
    if len({s[0] for s in stats.values()}) > 1:
        raise ValueError(f"{recording.name}: frame counts differ between cameras: {stats}")
    if len({s[1] for s in stats.values()}) > 1:
        say(f"[warn] {recording.name}: fps differs between cameras: {stats}")
    say(f"[ok] {recording.name}: {len(videos)} cams, {next(iter(stats.values()))[0]} frames each")
    return [v.stem for v in videos]


def read_toml(path: Path) -> dict:
    import toml  # the same parser aniposelib uses

    return toml.load(str(path))


def camera_keys(calib: dict) -> list:
    """Camera sections in the order aniposelib's CameraGroup.load() uses."""
    return [k for k in sorted(calib.keys()) if k != "metadata"]


def camera_names(calib: dict) -> list:
    return [calib[k]["name"] for k in camera_keys(calib)]


def prepare_take_calibration(take: Path, take_cams: list, toml_path: Path) -> tuple:
    """1.8.2 pairs 2D data with calibration cameras purely by POSITION and never
    compares names, so a missing camera would silently triangulate garbage. Write a
    calibration into the take folder that contains exactly the take's cameras, in
    the take's order. Returns (path, is_subset)."""
    import toml

    calib = read_toml(toml_path)
    by_name = {calib[k]["name"]: calib[k] for k in camera_keys(calib)}
    unknown = [c for c in take_cams if c not in by_name]
    if unknown:
        raise ValueError(
            f"{take.name}: cameras {unknown} are not in the calibration {list(by_name)} "
            "(camera = video file stem; keep names identical across the calibration take and all takes)"
        )
    if len(take_cams) < 2:
        raise ValueError(f"{take.name}: needs at least 2 cameras for 3D, has {take_cams}")

    is_subset = len(take_cams) < len(by_name)
    dst = take / f"{take.name}_camera_calibration.toml"
    if dst.resolve() == toml_path.resolve():
        return dst, False
    if take_cams != camera_names(calib):
        # a subset, or the same cameras in another order: renumber cam_0.. in the take's order
        out = {f"cam_{i}": by_name[name] for i, name in enumerate(take_cams)}
        meta = dict(calib.get("metadata", {}))
        meta["subset_of" if is_subset else "reordered_from"] = str(toml_path)
        out["metadata"] = meta
        with open(dst, "w") as f:
            toml.dump(out, f)
        if is_subset:
            missing = [c for c in by_name if c not in take_cams]
            msg = f"{take.name}: using {len(take_cams)}/{len(by_name)} calibrated cameras (missing {missing})"
            say(f"[warn] {msg}")
            emit_event("warn", take=take.name, message=msg)
    else:
        # the full set in the calibration's order: an unchanged copy (provenance; FreeMoCap's GUI looks for it)
        shutil.copy2(toml_path, dst)
    return dst, is_subset


# --------------------------------------------------------------------------- calibrate

def calibrate(calib_recording: Path, square_mm: float, board: str, groundplane: bool) -> dict:
    from freemocap.core_processes.capture_volume_calibration.anipose_camera_calibration import freemocap_anipose
    from freemocap.core_processes.capture_volume_calibration.charuco_stuff.charuco_board_definition import (
        charuco_5x3,
        charuco_7x5,
    )
    from freemocap.core_processes.capture_volume_calibration.run_anipose_capture_volume_calibration import (
        run_anipose_capture_volume_calibration,
    )

    check_opencv()
    t0 = time.time()
    cams = check_recording(calib_recording)
    emit_event("stage", scope="calibration", take=calib_recording.name, stage="detecting_board")

    # Capture the bundle-adjustment error that the calibrator computes and discards.
    captured = {}
    original = freemocap_anipose.CameraGroup.calibrate_videos

    def calibrate_videos_capturing(self, *args, **kwargs):
        out = original(self, *args, **kwargs)
        try:
            captured["error"] = float(out[0])
        except Exception:  # noqa: BLE001
            pass
        return out

    freemocap_anipose.CameraGroup.calibrate_videos = calibrate_videos_capturing
    try:
        # pin_camera_0_to_origin must stay True: in 1.8.2, calibrate_camera_capture_volume()
        # raises UnboundLocalError when it and use_charuco_as_groundplane are both False.
        global_toml, ground = run_anipose_capture_volume_calibration(
            charuco_board_definition={"7x5": charuco_7x5, "5x3": charuco_5x3}[board](),
            charuco_square_size=square_mm,
            calibration_videos_folder_path=calib_recording / VIDEO_FOLDER_NAME,
            pin_camera_0_to_origin=True,
            use_charuco_as_groundplane=groundplane,
            progress_callback=lambda msg: say(f"[calib] {msg}"),
        )
    finally:
        freemocap_anipose.CameraGroup.calibrate_videos = original

    # FreeMoCap also writes a copy next to the recording, named after the folder's stem.
    local_toml = calib_recording / f"{calib_recording.stem}_camera_calibration.toml"
    toml_path = local_toml if local_toml.is_file() else Path(global_toml)

    ground_info = {"requested": groundplane}
    if groundplane and ground is not None:
        ground_info.update(success=bool(ground.success), error=ground.error)
        if not ground.success:
            say(f"[warn] ground plane alignment failed, kept camera-0 origin: {ground.error}")
    info = {
        "toml": toml_path,
        "toml_global": global_toml,
        "cameras": camera_names(read_toml(toml_path)),
        "reprojection_error_px": captured.get("error"),
        "board": board,
        "square_mm": square_mm,
        "groundplane": ground_info,
        "seconds": round(time.time() - t0, 1),
    }
    if info["cameras"] != cams:
        raise RuntimeError(f"calibration cameras {info['cameras']} != videos {cams}")
    say(f"[ok] calibration TOML: {toml_path} (reprojection error {info['reprojection_error_px']})")
    emit_event("calibration_done", **info)
    return info


# --------------------------------------------------------------------------- process

# --params: a flat JSON object of FreeMoCap 1.8.2's own field names, sorted into its models.
# Absent keys keep FreeMoCap's defaults. num_processes comes from --processes.
PARAM_GROUPS = {
    "tracking": ("run_image_tracking", "mediapipe_model_complexity", "min_detection_confidence",
                 "min_tracking_confidence", "static_image_mode", "use_yolo_crop_method", "yolo_model_size",
                 "bounding_box_buffer_percentage", "buffer_size_method"),          # MediapipeTrackingParams
    "triangulation": ("run_3d_triangulation", "minimum_cameras_for_triangulation", "flatten_single_camera_data",
                      "use_triangulate_outlier_rejection", "maximum_cameras_to_drop",
                      "target_reprojection_error"),                                    # AniposeTriangulate3DParametersModel
    "post": ("run_butterworth_filter", "framerate"),                                 # PostProcessingParametersModel
    "filter": ("cutoff_frequency", "order"),                                         # ButterworthFilterParametersModel
}


def split_params(params: dict | None) -> dict:
    # {group: {field: value}} for the keys present; unknown keys are an error
    params = dict(params or {})
    known = {k: g for g, keys in PARAM_GROUPS.items() for k in keys}
    unknown = sorted(k for k in params if k not in known)
    if unknown:
        raise ValueError(f"unknown FreeMoCap parameter(s): {unknown}")
    out = {g: {} for g in PARAM_GROUPS}
    for k, v in params.items():
        out[known[k]][k] = v
    return out


def build_processing_params(params: dict | None, processes: int):
    from freemocap.data_layer.recording_models.post_processing_parameter_models import (
        AniposeTriangulate3DParametersModel,
        ButterworthFilterParametersModel,
        PostProcessingParametersModel,
        ProcessingParameterModel,
    )
    from skellytracker.trackers.mediapipe_tracker.mediapipe_model_info import MediapipeTrackingParams

    g = split_params(params)
    post = dict(g["post"])
    filt = dict(g["filter"])
    if "framerate" in post:
        # 1.8.2's filter reads post_processing_parameters_model.framerate; keep the (unused)
        # sampling_rate in step so the recording_parameters.json FreeMoCap writes isn't misleading
        filt["sampling_rate"] = post["framerate"]
    return ProcessingParameterModel(
        tracking_parameters_model=MediapipeTrackingParams(num_processes=max(1, int(processes)), **g["tracking"]),
        anipose_triangulate_3d_parameters_model=AniposeTriangulate3DParametersModel(**g["triangulation"]),
        post_processing_parameters_model=PostProcessingParametersModel(
            butterworth_filter_parameters=ButterworthFilterParametersModel(**filt), **post),
    )


def applied_params(model) -> dict:
    # the flat parameters a model ends up with (FreeMoCap's defaults filled in)
    t = model.tracking_parameters_model
    a = model.anipose_triangulate_3d_parameters_model
    p = model.post_processing_parameters_model
    out = {k: getattr(t, k) for k in PARAM_GROUPS["tracking"]}
    out.update({k: getattr(a, k) for k in PARAM_GROUPS["triangulation"]})
    out.update({k: getattr(p, k) for k in PARAM_GROUPS["post"]})
    out.update({k: getattr(p.butterworth_filter_parameters, k) for k in PARAM_GROUPS["filter"]})
    return out


def process(take: Path, toml_path: Path, processes: int, run_blender: bool, blender_exe, params_in=None) -> dict:
    from freemocap.core_processes.process_motion_capture_videos.process_recording_headless import (
        process_recording_headless,
    )

    t0 = time.time()
    take_cams = check_recording(take)
    used_toml, is_subset = prepare_take_calibration(take, take_cams, toml_path)

    params = build_processing_params(params_in, processes)
    STAGES.reset(take.name)
    process_recording_headless(
        recording_path=take,
        path_to_camera_calibration_toml=used_toml,
        path_to_blender_executable=blender_exe,
        recording_processing_parameter_model=params,
        run_blender=run_blender,
        make_jupyter_notebook=False,
        use_tqdm=True,
    )
    output = take / "output_data"
    if not any(output.glob("*_body_3d_xyz.npy")):
        raise RuntimeError(f"{take.name}: FreeMoCap finished but wrote no *_body_3d_xyz.npy in {output}")
    info = {
        "take": take.name,
        "path": take,
        "output_data": output,
        "cameras": take_cams,
        "subset": is_subset,
        "toml": used_toml,
        "blend": next(iter(take.glob("*.blend")), None),
        "seconds": round(time.time() - t0, 1),
        "params": applied_params(params),
        "processes": params.tracking_parameters_model.num_processes,
    }
    say(f"[ok] processed {take.name} -> {output}")
    return info


def load_params(path) -> dict | None:
    if not path:
        return None
    with open(path, "r", encoding="utf-8") as fh:
        params = json.load(fh)
    if not isinstance(params, dict):
        raise ValueError(f"--params {path}: expected a JSON object")
    split_params(params)                      # fail on unknown keys before any work
    return params


def process_many(takes: list, toml_path: Path, args) -> tuple:
    done, failed = [], {}
    params_in = load_params(getattr(args, "params", None))
    if params_in:
        say(f"[params] {json.dumps(params_in, sort_keys=True)}")
    for i, take in enumerate(takes):
        emit_event("take_start", take=take.name, index=i, total=len(takes))
        try:
            info = process(take, toml_path, args.processes, args.blender, args.blender_exe, params_in)
            done.append(info)
            emit_event("take_done", **info)
        except Exception as e:  # noqa: BLE001 - keep going with the other takes
            traceback.print_exc()
            failed[take.name] = repr(e)
            emit_event("take_failed", take=take.name, error=repr(e))
    return done, failed


# --------------------------------------------------------------------------- check / selftest

def check() -> dict:
    import cv2
    import freemocap
    import mediapipe

    check_opencv()
    info = {
        "freemocap": freemocap.__version__,
        "python": platform.python_version(),
        "opencv": cv2.__version__,
        "mediapipe": mediapipe.__version__,
        "executable": sys.executable,
        "home": str(Path.home()),
    }
    if freemocap.__version__ != EXPECTED_FREEMOCAP_VERSION:
        raise RuntimeError(f"expected freemocap {EXPECTED_FREEMOCAP_VERSION}, found {freemocap.__version__}")
    return info


def download_sample(workdir: Path) -> Path:
    import io
    import zipfile

    import requests  # installed with freemocap

    data = workdir / "freemocap_test_data"
    if (data / VIDEO_FOLDER_NAME).is_dir():
        return data
    workdir.mkdir(parents=True, exist_ok=True)
    emit_event("stage", scope="selftest", stage="downloading")
    with requests.get(SAMPLE_DATA_URL, stream=True, timeout=(10, 120)) as r:
        r.raise_for_status()
        total = int(r.headers.get("content-length") or 0)
        buf, got, last = io.BytesIO(), 0, 0.0
        for chunk in r.iter_content(1 << 20):
            buf.write(chunk)
            got += len(chunk)
            if time.time() - last > 0.5:
                emit_event("download", done=got, total=total)
                last = time.time()
    emit_event("download", done=got, total=total)
    zipfile.ZipFile(buf).extractall(workdir)
    if not (data / VIDEO_FOLDER_NAME).is_dir():
        raise RuntimeError(f"unexpected sample layout in {workdir}")
    return data


def selftest(workdir: Path, args) -> dict:
    import numpy as np

    t0 = time.time()
    data = download_sample(workdir)
    calib_rec, take = workdir / "selftest_calibration", workdir / "selftest_take"
    for rec in (calib_rec, take):
        if rec.exists():
            shutil.rmtree(rec)
        shutil.copytree(data / VIDEO_FOLDER_NAME, rec / VIDEO_FOLDER_NAME)

    calib = calibrate(calib_rec, SAMPLE_SQUARE_MM, "7x5", False)
    done, failed = process_many([take], Path(calib["toml"]), args)
    if failed:
        raise RuntimeError(f"processing failed: {failed}")

    body = np.load(next((take / "output_data").glob("*_body_3d_xyz.npy")))  # (frames, 33, 3), mm
    nan_fraction = float(np.isnan(body).mean())

    def bone(a, b):  # MediaPipe Pose landmark indices
        return float(np.nanmedian(np.linalg.norm(body[:, a] - body[:, b], axis=1)))

    upper_arm, thigh = bone(11, 13), bone(23, 25)  # left shoulder-elbow, left hip-knee
    error = calib["reprojection_error_px"]
    checks = {
        "reprojection_error_below_2px": error is not None and error < 2.0,
        "nan_fraction_below_0.2": nan_fraction < 0.2,
        "upper_arm_150_450mm": 150 < upper_arm < 450,
        "thigh_250_600mm": 250 < thigh < 600,
    }
    return {
        "passed": all(checks.values()),
        "checks": checks,
        "reprojection_error_px": error,
        "nan_fraction": round(nan_fraction, 4),
        "upper_arm_mm": round(upper_arm, 1),
        "thigh_mm": round(thigh, 1),
        "frames": int(body.shape[0]),
        "seconds": round(time.time() - t0, 1),
    }


# --------------------------------------------------------------------------- cli

def main() -> int:
    parser = argparse.ArgumentParser(description="Headless FreeMoCap 1.8.2 calibration + processing")
    parser.add_argument("--fmc-home", help="run FreeMoCap with this as its home dir (isolates freemocap_data)")
    parser.add_argument("--verbose", action="store_true", help="keep FreeMoCap's console logging")
    sub = parser.add_subparsers(dest="cmd", required=True)

    def calib_args(p):
        p.add_argument("--square-mm", type=float, required=True, help="black square edge length in mm")
        p.add_argument("--board", choices=["7x5", "5x3"], default="7x5")
        p.add_argument("--groundplane", action="store_true",
                       help="align world to the board (board flat on the floor in the calibration clip)")

    def proc_args(p):
        p.add_argument("--processes", type=int, default=1, help="2D tracking processes (one per camera max)")
        p.add_argument("--params", type=Path, default=None,
                       help="JSON object of FreeMoCap processing parameters (its own field names; absent = its default)")
        p.add_argument("--blender", action="store_true", help="also export a .blend")
        p.add_argument("--blender-exe", default=None)

    sub.add_parser("check")
    p = sub.add_parser("calibrate")
    p.add_argument("calib_recording", type=Path)
    calib_args(p)
    p = sub.add_parser("process")
    p.add_argument("takes", type=Path, nargs="+")
    p.add_argument("--toml", type=Path, required=True)
    proc_args(p)
    p = sub.add_parser("all")
    p.add_argument("calib_recording", type=Path)
    p.add_argument("takes", type=Path, nargs="+")
    calib_args(p)
    proc_args(p)
    p = sub.add_parser("selftest")
    p.add_argument("--workdir", type=Path, required=True)
    p.add_argument("--processes", type=int, default=1)
    args = parser.parse_args()
    if args.cmd == "selftest":
        args.blender, args.blender_exe = False, None

    try:
        if args.fmc_home:
            isolate_home(args.fmc_home)
        load_freemocap(args.verbose)

        if args.cmd == "check":
            emit_result(True, **check())
            return 0

        if args.cmd == "selftest":
            result = selftest(args.workdir.resolve(), args)
            emit_result(result["passed"], **result)
            return 0 if result["passed"] else 1

        if args.cmd == "calibrate":
            emit_result(True, **calibrate(args.calib_recording.resolve(), args.square_mm, args.board, args.groundplane))
            return 0

        takes = [t.resolve() for t in args.takes]
        calibration = None
        if args.cmd == "all":
            calib_rec = args.calib_recording.resolve()
            calib_cams = check_recording(calib_rec)
            for take in takes:  # fail before the slow calibration if a take can't match it
                unknown = [c for c in check_recording(take) if c not in calib_cams]
                if unknown:
                    raise ValueError(f"{take.name}: cameras {unknown} are not in calibration recording {calib_cams}")
            calibration = calibrate(calib_rec, args.square_mm, args.board, args.groundplane)
            toml_path = Path(calibration["toml"])
        else:
            toml_path = args.toml.resolve()
            if not toml_path.is_file():
                raise FileNotFoundError(f"Calibration TOML not found: {toml_path}")

        done, failed = process_many(takes, toml_path, args)
        emit_result(not failed, calibration=calibration, toml=toml_path, processed=done, failed=failed)
        return 0 if not failed else 1

    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        emit_result(False, error=repr(e))
        return 1


if __name__ == "__main__":
    # The guard matters: 2D tracking can spawn worker processes, and Windows
    # re-imports this file in every one of them.
    sys.exit(main())

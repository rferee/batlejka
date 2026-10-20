"""FreeMoCap integration without FreeMoCap: the output parsers of fmc/runner.py and the
take-calibration writer of fmc/fmc_headless.py (which runs in the FreeMoCap venv, so it's
loaded here from its file; it only needs ``toml`` for this)."""
from __future__ import annotations

import importlib.util
import json

import pytest

from mocap_preprocess import config
from mocap_preprocess.fmc import runner

toml = pytest.importorskip("toml")


def test_parse_events_and_result():
    kind, ev = runner.parse_line('FMC_EVENT {"type": "stage", "scope": "take", "take": "t1", "stage": "triangulating"}')
    assert kind == "event" and ev["stage"] == "triangulating" and ev["take"] == "t1"
    kind, res = runner.parse_line('FMC_RESULT {"ok": false, "error": "boom"}')
    assert kind == "result" and res == {"ok": False, "error": "boom"}
    # broken JSON or a non-object stays text instead of crashing the pump
    assert runner.parse_line("FMC_EVENT {not json")[0] == "text"
    assert runner.parse_line("FMC_RESULT [1, 2]")[0] == "text"
    assert runner.parse_line("[ok] calibration: 3 cams, 222 frames each") == ("text", "[ok] calibration: 3 cams, 222 frames each")


def test_parse_tqdm_bars():
    line = ("processing video: iPad - Table_synced.mp4:  42%|████▏     | 93/222 [00:04<00:05, 22.31it/s]")
    kind, bar = runner.parse_line(line)
    assert kind == "tqdm"
    assert bar == {"name": "iPad - Table_synced.mp4", "pct": 42, "n": 93, "total": 222}
    # done bar, no rate yet, leading carriage-return residue stripped by the reader
    kind, bar = runner.parse_line("processing video: cam_2.mp4: 100%|██████████| 222/222 [00:09<00:00]")
    assert kind == "tqdm" and bar["n"] == bar["total"] == 222
    assert runner.parse_line("Detecting 2d skeletons...")[0] == "text"


def test_strip_ansi():
    raw = "\x1b[32m2026-10-21 01:02:03\x1b[0m | \x1b[1mWARNING\x1b[0m | something"
    assert runner.strip_ansi(raw) == "2026-10-21 01:02:03 | WARNING | something"
    kind, bar = runner.parse_line(runner.strip_ansi("\x1b[Aprocessing video: a.mp4:  10%|#| 1/10 [00:01<00:09]"))
    assert kind == "tqdm" and bar["n"] == 1


def test_bars_average_cameras_interleaved():
    bars = runner.Bars()
    assert bars.fraction(3) == 0.0
    bars.update({"name": "a.mp4", "pct": 50, "n": 50, "total": 100})
    bars.update({"name": "b.mp4", "pct": 100, "n": 100, "total": 100})
    assert bars.fraction() == pytest.approx(0.75)
    assert bars.fraction(4) == pytest.approx(1.5 / 4)      # cameras without a bar yet count as 0
    bars.update({"name": "a.mp4", "pct": 100, "n": 100, "total": 100})
    assert bars.fraction(2) == pytest.approx(1.0)
    bars.reset()
    assert bars.fraction(2) == 0.0


def test_describe_event():
    msg, level = runner.describe_event({"type": "take_failed", "take": "t1", "error": "ValueError('x')"})
    assert level == "error" and "t1" in msg and "ValueError" in msg
    msg, level = runner.describe_event({"type": "calibration_done", "reprojection_error_px": 0.2634, "cameras": ["a", "b"]})
    assert level == "done" and "0.26 px" in msg and "a, b" in msg
    assert runner.describe_event({"type": "download", "done": 1, "total": 2}) is None


def test_headless_argv_isolates_home():
    argv = runner.headless_argv("process", "C:/x y/take", "--processes", 4)
    assert argv[0] == str(runner.venv_python())
    assert argv[1:5] == ["-I", "-u", "-X", "utf8"]
    assert argv[5].endswith("fmc_headless.py")
    assert argv[6:8] == ["--fmc-home", str(config.fmc_dir() / "home")]
    assert argv[8:] == ["process", "C:/x y/take", "--processes", "4"]


# --------------------------------------------------------------------------- subset TOML

def _headless():
    spec = importlib.util.spec_from_file_location("fmc_headless_under_test", runner.SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _cam(name, k):
    return {"name": name, "size": [1920, 1080], "matrix": [[1000.0 + k, 0.0, 960.0], [0.0, 1000.0 + k, 540.0], [0.0, 0.0, 1.0]],
            "distortions": [0.01 * k, 0.0, 0.0, 0.0, 0.0], "rotation": [0.0, 0.1 * k, 0.0], "translation": [100.0 * k, 0.0, 0.0]}


@pytest.fixture
def calib(tmp_path):
    data = {f"cam_{i}": _cam(n, i) for i, n in enumerate(["a_synced", "b_synced", "c_synced", "d_synced"])}
    data["metadata"] = {"adjusted": False, "error": 0.26}
    path = tmp_path / "calibration_camera_calibration.toml"
    with open(path, "w") as fh:
        toml.dump(data, fh)
    return path


def _take(tmp_path, name):
    take = tmp_path / name
    take.mkdir()
    return take


def test_subset_calibration_renumbers_in_take_order(tmp_path, calib, capsys):
    fmc = _headless()
    take = _take(tmp_path, "take_1")
    path, subset = fmc.prepare_take_calibration(take, ["a_synced", "c_synced", "d_synced"], calib)
    assert subset is True
    assert path == take / "take_1_camera_calibration.toml"
    out = toml.load(str(path))
    keys = [k for k in sorted(out) if k != "metadata"]
    assert keys == ["cam_0", "cam_1", "cam_2"]
    assert [out[k]["name"] for k in keys] == ["a_synced", "c_synced", "d_synced"]
    src = toml.load(str(calib))
    assert out["cam_1"] == src["cam_2"]                     # c's parameters travel with it
    assert out["metadata"]["subset_of"] == str(calib)
    assert out["metadata"]["error"] == 0.26
    events = [json.loads(ln[len("FMC_EVENT "):]) for ln in capsys.readouterr().out.splitlines() if ln.startswith("FMC_EVENT ")]
    assert any(e["type"] == "warn" and "b_synced" in e["message"] for e in events)


def test_full_set_is_copied_unchanged(tmp_path, calib):
    fmc = _headless()
    take = _take(tmp_path, "take_2")
    path, subset = fmc.prepare_take_calibration(take, ["a_synced", "b_synced", "c_synced", "d_synced"], calib)
    assert subset is False
    assert path.read_bytes() == calib.read_bytes()


def test_unknown_camera_or_too_few_fail(tmp_path, calib):
    fmc = _headless()
    take = _take(tmp_path, "take_3")
    with pytest.raises(ValueError, match="not in the calibration"):
        fmc.prepare_take_calibration(take, ["a_synced", "z_synced"], calib)
    with pytest.raises(ValueError, match="at least 2"):
        fmc.prepare_take_calibration(take, ["a_synced"], calib)
    assert not (take / "take_3_camera_calibration.toml").exists()

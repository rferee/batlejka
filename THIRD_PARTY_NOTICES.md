# Third-party notices

Batlejka itself is licensed under the GNU Affero General Public License v3.0 (see
`LICENSE`). The pieces below are not ours and keep their own licenses.

## Bundled in this repository

| What | Where | License |
|---|---|---|
| Font Awesome Free 6.5.2 (solid icon set: core + solid CSS, `fa-solid-900` webfont) | `mocap_preprocess/static/vendor/fontawesome/` | Icons CC BY 4.0, fonts SIL OFL 1.1, code MIT — see `LICENSE.txt` there; the attribution comments in the files are kept as the license asks |
| JetBrains Mono (the UI typeface) | `mocap_preprocess/static/vendor/jetbrains-mono/` | SIL Open Font License 1.1 — see `OFL.txt` there |

## Run alongside, not bundled

| What | How it is used | License |
|---|---|---|
| [FreeMoCap](https://freemocap.org/) v1.8.2 | Installed by the Setup panel into its own Python environment under `.tools/` and run as a separate process for calibration and 3D tracking. Batlejka is a wrapper around it; the mocap is theirs. | AGPL-3.0 |
| FFmpeg / FFprobe | Found on `PATH` (or `MOCAP_FFMPEG` / `MOCAP_FFPROBE`) and run as a subprocess for probing, proxies, audio extraction and export | LGPL/GPL, depending on the build you install |
| Python packages in `requirements.txt` (FastAPI, Starlette, uvicorn, NumPy, SciPy, uv) | Installed into `.venv` by `run.bat` | Their own permissive licenses (MIT, BSD, Apache-2.0) |

If you redistribute Batlejka, keep this file, `LICENSE`, and the license files under
`mocap_preprocess/static/vendor/`.

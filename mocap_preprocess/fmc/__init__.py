"""FreeMoCap integration.

``fmc_headless.py`` runs inside the FreeMoCap venv and is never imported by the app;
the other modules here run in the app: ``env`` (install / repair / remove / check),
``runner`` (calls fmc_headless.py, parses its output), ``mocap`` (calibration and
processing jobs for a session).
"""

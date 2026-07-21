# PyInstaller spec for the headless sidecar binary that Tauri bundles.
#
# Run: python -m PyInstaller sidecar/sidecar.spec  (from the repo root)
# Output: dist/fflogs-efficiency-analyzer-sidecar/  (onedir: exe + _internal/)
#
# onedir, not onefile: the self-extracting onefile bootloader is the #1 AV
# false-positive trigger and re-extracts to %TEMP% on every launch. The
# companion script sidecar/build_sidecar.py stages the exe (target-triple
# suffixed, for Tauri's externalBin) plus _internal/ (bundled via Tauri's
# bundle.resources) into the Tauri project's binaries/ folder.
#
# SIDECAR_VERSION_FILE (env, optional): path to a PyInstaller version-info
# file; build_sidecar.py generates one from tauri.conf.json's version so the
# exe carries a Windows version resource (NSIS can skip replacing
# version-less exes on update).

import os
import sys

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

# collect_submodules imports the package at spec-eval time (before Analysis
# applies pathex), so make sure python/ (the repo's import root, parent of this
# spec's sidecar/ dir) is importable. SPECPATH is injected by PyInstaller.
_PY_ROOT = os.path.dirname(SPECPATH)
if _PY_ROOT not in sys.path:
    sys.path.insert(0, _PY_ROOT)

# Job packages are imported lazily by jobs/__init__.py's registry, so
# PyInstaller's static scan can't see them. Collect every jobs.* submodule
# automatically — adding a new job package then bundles with no spec edit
# (matches the "adding a job is a data exercise" story in CLAUDE.md).
_job_modules = collect_submodules('jobs')

# sim_pool is imported lazily (function-level) by main.py and re-imported by spawned
# pool workers, so the static scan can miss it — pin it. multiprocessing's PyInstaller
# runtime hook is auto-applied (main.py imports multiprocessing), which + freeze_support()
# makes spawn work in the frozen exe; a pool failure still falls back to in-process.
_extra = ['sidecar.sim_pool']

# Hand-authored premade ("PF") mit plans are JSON DATA files loaded at runtime by
# mitplan/premade.py via Path(__file__).parent/"premade"/<id>.json — PyInstaller's
# code scan never sees them, so they must be bundled explicitly or has_premade()
# returns False in the packaged app (the "Use PF mit plan" button never appears).
# collect_data_files places them at <bundle>/mitplan/premade/, mirroring the source
# tree so the __file__-relative load resolves unchanged. Any new premade/<id>.json
# then bundles automatically — no spec edit.
_premade_data = collect_data_files('mitplan', includes=['premade/*.json'])

a = Analysis(
    ['main.py'],
    pathex=['..'],   # repo root, so jobs/, fflogs_api, etc. resolve as imports
    binaries=[],
    datas=_premade_data,
    hiddenimports=_job_modules + _extra,
    hookspath=[],
    runtime_hooks=[],
    excludes=[
        # Sidecar is headless — never needs Qt/Tk/etc.
        'PySide6', 'PyQt5', 'PyQt6', 'tkinter',
        'matplotlib', 'IPython', 'jupyter',
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,   # onedir: binaries/datas ship in COLLECT's _internal/
    name='fflogs-efficiency-analyzer-sidecar',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,            # UPX optional; off by default — no toolchain assumed
    console=True,         # stdin/stdout transport requires a console subsystem
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    version=os.environ.get('SIDECAR_VERSION_FILE') or None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name='fflogs-efficiency-analyzer-sidecar',
)

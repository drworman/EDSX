# -*- mode: python ; coding: utf-8 -*-
#
# PyInstaller spec for EDSX.
#
# EDSX is a single stdlib-only module, so there is almost nothing to
# collect: reference data is downloaded at runtime and cached under the
# user's Documents folder, never shipped in the binary.
#
# The one data file is `version`. It is the single source of truth for the
# version string and edsx.py reads it at import time — from sys._MEIPASS when
# frozen — so it has to land at the top of the bundle, the same relative
# position it occupies next to edsx.py in a checkout. Without it the binary
# starts and reports its version as "unknown", which the release workflow's
# smoke test catches.
#
# The exclusions below matter more than they look. PyInstaller's analysis
# pulls in whatever it can reach, and a stdlib-only CLI has no business
# carrying tkinter or the test suite. Dropping them keeps the artefact
# small enough that people will actually download it.

import os
from pathlib import Path

block_cipher = None

# SPECPATH is injected into the spec's namespace by PyInstaller.
ROOT = Path(SPECPATH).resolve().parent  # noqa: F821

analysis = Analysis(
    ["../edsx.py"],
    pathex=[],
    binaries=[],
    datas=[(str(ROOT / "version"), ".")],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "tkinter",
        "unittest",
        "pydoc_data",
        "test",
        "lib2to3",
        "sqlite3",
        "xml",
        "email.test",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(analysis.pure, analysis.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    analysis.scripts,
    analysis.binaries,
    analysis.zipfiles,
    analysis.datas,
    [],
    name="EDSX",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    # A command-line tool must keep its console on Windows. This is the
    # reason EDSX needs none of the stdout reattachment machinery
    # that EDLD and EDSG carry in win_console.py.
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

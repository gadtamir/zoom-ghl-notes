# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec — produces a tray app for the current OS.

macOS  → ZoomGHL.app  (LSUIElement=True so it lives in the menu bar, no Dock icon)
Windows → ZoomGHL.exe (no console window)

Usage:
    pyinstaller zghl.spec --noconfirm --clean
"""

import platform
from pathlib import Path

HERE = Path(SPEC).resolve().parent
SRC = HERE / "src"
ASSETS = HERE / "build_assets"

IS_MAC = platform.system() == "Darwin"
IS_WIN = platform.system() == "Windows"

APP_NAME = "ZoomGHL"
icon_path = str(ASSETS / ("icon.icns" if IS_MAC else "icon.ico"))


a = Analysis(
    [str(HERE / "main.py")],
    pathex=[str(HERE)],
    binaries=[],
    datas=[],
    hiddenimports=[
        # pystray loads its platform backend lazily; help PyInstaller find it.
        "pystray._darwin" if IS_MAC else "pystray._win32",
        # Pillow modules used at runtime by pystray.
        "PIL._tkinter_finder",
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=[
        "matplotlib",
        "numpy",
        "scipy",
        "pandas",
        "PyQt5",
        "PyQt6",
        "PySide2",
        "PySide6",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=None)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=APP_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,            # no terminal window
    icon=icon_path if Path(icon_path).exists() else None,
    disable_windowed_traceback=False,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    name=APP_NAME,
)

if IS_MAC:
    app = BUNDLE(
        coll,
        name=f"{APP_NAME}.app",
        icon=icon_path if Path(icon_path).exists() else None,
        bundle_identifier="com.morethan.zoomghl",
        info_plist={
            "LSUIElement": True,                    # menu bar app, no Dock icon
            "CFBundleDisplayName": APP_NAME,
            "CFBundleName": APP_NAME,
            "CFBundleShortVersionString": "0.1.0",
            "CFBundleVersion": "0.1.0",
            "NSHighResolutionCapable": True,
            "LSMinimumSystemVersion": "11.0",
        },
    )

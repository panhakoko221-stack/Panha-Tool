# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path


PROJECT_ROOT = Path(SPECPATH)
ICON_FILE = PROJECT_ROOT / "assets" / "theara.ico"
KHMER_FONT_FILE = PROJECT_ROOT / "assets" / "fonts" / "NotoSansKhmer.ttf"
KHMER_FONT_LICENSE = PROJECT_ROOT / "assets" / "fonts" / "OFL-NotoSansKhmer.txt"
VERSION_FILE = PROJECT_ROOT / "version_info.txt"

a = Analysis(
    [str(PROJECT_ROOT / "main.py")],
    pathex=[str(PROJECT_ROOT)],
    binaries=[],
    datas=[
        (str(ICON_FILE), "assets"),
        (str(KHMER_FONT_FILE), "assets/fonts"),
        (str(KHMER_FONT_LICENSE), "assets/fonts"),
    ],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Theara",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    icon=str(ICON_FILE),
    version=str(VERSION_FILE),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="Theara",
)

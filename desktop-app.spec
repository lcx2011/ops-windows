# PyInstaller one-folder build for the Windows desktop application.
# WebView2 remains a Windows system component; the build only bundles the
# application and its local static assets.
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files


_spec_path = SPECPATH if isinstance(SPECPATH, (str, bytes)) else SPECPATH[0]
ROOT = Path(_spec_path).resolve()
datas = [
    (str(ROOT / "static"), "static"),
    (str(ROOT / "VERSION"), "."),
]
datas += collect_data_files("webview")
hiddenimports = [
    "webview.platforms.winforms",
    "webview.platforms.edgechromium",
    "webview._version",
]
excludes = [
    "webview.platforms.android",
    "webview.platforms.cocoa",
    "webview.platforms.gtk",
    "webview.platforms.qt",
    "webview.platforms.mshtml",
    "PyQt5",
    "PySide2",
    "PySide6",
    "PyGObject",
    "kivy",
    "cefpython3",
]

a = Analysis(
    [str(ROOT / "desktop_app.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    name="LocalOps",
    exclude_binaries=True,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    icon=str(ROOT / "static" / "assets" / "favicon.ico"),
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    a.zipfiles,
    a.zipped_data,
    name="LocalOps",
)

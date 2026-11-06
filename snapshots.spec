# -*- mode: python ; coding: utf-8 -*-
# Build with:  pyinstaller --noconfirm snapshots.spec      (or just run build.bat)
import importlib.util
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_submodules

datas = [("snapshots/static", "snapshots/static")]  # the web interface
binaries = []
hiddenimports = collect_submodules("uvicorn") + ["multipart", "python_multipart"]
# The face, content-search and text-reading models are not bundled: they are downloaded on first use.
# OpenCV, NumPy and onnxruntime are found by PyInstaller's own hooks.

# pillow-heif ships native codec libraries that PyInstaller will not find by itself.
heif_datas, heif_binaries, heif_hidden = collect_all("pillow_heif")
datas += heif_datas
binaries += heif_binaries
hiddenimports += heif_hidden

# JPEG XL: only imagecodecs' JPEG XL module is needed, not its many other codecs (it loads them lazily).
if importlib.util.find_spec("imagecodecs") is not None:
    from PyInstaller.utils.hooks import collect_dynamic_libs

    _wanted = {"_jpegxl", "_shared", "_imcd"}
    hiddenimports += [m for m in collect_submodules("imagecodecs")
                      if not m.rsplit(".", 1)[-1].startswith("_") or m.rsplit(".", 1)[-1] in _wanted]
    binaries += collect_dynamic_libs("imagecodecs")

# rawpy (camera RAW files) wraps LibRaw, a native library; collect it the same way.
if importlib.util.find_spec("rawpy") is not None:
    raw_datas, raw_binaries, raw_hidden = collect_all("rawpy")
    datas += raw_datas
    binaries += raw_binaries
    hiddenimports += raw_hidden

# The app window: pywebview draws it with Microsoft's WebView2 through pythonnet. PyInstaller has
# hooks for all three; the Windows-only parts are named here so a missing one fails loudly.
# Search by content and text reading run on onnxruntime; its hook collects the native library.
hiddenimports += ["onnxruntime", "onnxruntime.capi._pybind_state"]

if importlib.util.find_spec("webview") is not None:
    hiddenimports += ["webview.platforms.winforms", "webview.platforms.edgechromium", "clr"]

icon = "assets/icon.ico" if Path("assets/icon.ico").exists() else None

a = Analysis(
    ["run_snapshots.py"],
    pathex=["."],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["pytest", "httpx"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="Snapshots",
    icon=icon,
    console=False,  # a normal Windows program: no black window. Output goes to snapshots.log
    upx=False,
    runtime_tmpdir=None,
)

# Build: python -m PyInstaller --noconfirm --clean packaging/reviewrelay.spec
from pathlib import Path
import os
import sys


def dependency_search_path(python_root, windows_root):
    return os.pathsep.join(str(path) for path in (
        Path(python_root), Path(python_root) / "DLLs",
        Path(windows_root) / "System32", Path(windows_root)))


# Analysis must not pick incompatible ICU/CRT/OpenSSL DLLs from tools injected
# into the developer shell's PATH. Package hooks supply their own library dirs.
# This changes only the build process; the installed application's PATH stays normal.
os.environ["PATH"] = dependency_search_path(sys.base_prefix, os.environ["SystemRoot"])

project = Path(SPECPATH).parent
a = Analysis(
    [str(project / "packaging" / "reviewrelay_entry.py")],
    pathex=[str(project / "src"), str(project / "packaging")],
    binaries=[], datas=[], hiddenimports=["playwright.async_api"],
    hookspath=[], hooksconfig={}, runtime_hooks=[],
    excludes=["pytest", "tkinter", "PyQt5", "PyQt6", "PySide2", "reviewrelay.dev"],
    noarchive=False,
)
# The official Playwright hook supplies its Node driver and JS resources.
# Browser downloads are external, even if installed beneath the driver folder.
a.datas = [entry for entry in a.datas if ".local-browsers" not in Path(entry[0]).parts]
a.binaries = [entry for entry in a.binaries if ".local-browsers" not in Path(entry[0]).parts]
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="ReviewRelay",
          debug=False, strip=False, upx=False, console=False,
          disable_windowed_traceback=False, contents_directory="_internal")
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="ReviewRelay")

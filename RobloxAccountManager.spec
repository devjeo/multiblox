# PyInstaller spec for the Roblox Account Manager (Windows, one-file, no console).
#
# Build (from this folder, on Windows, in a clean virtualenv):
#     pip install pyinstaller pystray pillow
#     pyinstaller --clean --noconfirm RobloxAccountManager.spec
# Result: dist\\RobloxAccountManager.exe
#
# Every local module is listed explicitly because gui.py imports several of them on demand
# (schedule_window, join_dialog, pystray, PIL), and PyInstaller can miss those.
# All data and log files live under %APPDATA%\\RobloxAccountManager, never next to the exe.
from pathlib import Path

root = Path(SPECPATH)
local_modules = sorted(p.stem for p in root.glob("*.py") if not p.stem.startswith("test_"))

a = Analysis(
    [str(root / "gui.py")],
    pathex=[str(root)],
    hiddenimports=local_modules + ["pystray", "pystray._win32", "PIL", "PIL.Image", "PIL.ImageDraw"],
    excludes=["unittest", "test", "pydoc"],
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, a.binaries, a.datas, [],
    name="RobloxAccountManager",
    console=False,        # windowed app; errors are shown in the log panel
    upx=False,            # UPX-packed exes trigger far more antivirus false positives
    uac_admin=False,      # set True to always prompt for administrator (recommended for multi-instance)
)

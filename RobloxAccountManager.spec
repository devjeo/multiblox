# PyInstaller spec: builds dist\RobloxAccountManager.exe (one file, no console window).
# Build:  pyinstaller --clean --noconfirm RobloxAccountManager.spec
# Then:   ISCC.exe installer.iss      (see installer.iss)
#
# Some modules are imported only when a window opens (join_dialog, schedule_window, ...), and the tray
# icon libraries are optional, so PyInstaller's scanner can miss them. Every app module is therefore
# listed explicitly below (local_modules), plus pystray and PIL for the system tray.
from pathlib import Path

HERE = Path(SPECPATH)

# every app module except tests and the build helpers
local_modules = sorted(
    p.stem for p in HERE.glob("*.py")
    if not p.stem.startswith("test_") and p.stem not in ("gui",)
)

hiddenimports = local_modules + [
    "pystray", "pystray._win32", "PIL", "PIL.Image", "PIL.ImageDraw",
]

a = Analysis(
    ["gui.py"],
    pathex=[str(HERE)],
    binaries=[],
    datas=[],
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["unittest", "pytest"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="RobloxAccountManager",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    console=False,      # windowed app; the log lives in %APPDATA%\RobloxAccountManager
    disable_windowed_traceback=False,
    # No UAC manifest here: the installer's "Always run as administrator" task sets that per user.
    uac_admin=False,
)

@echo off
rem One-click build: exe with PyInstaller, then the installer with Inno Setup.
rem Run from this folder in a normal Command Prompt (Windows, 64-bit Python 3.10+).
setlocal
where python >nul 2>nul || (echo Python is not on PATH. Install it from python.org and tick "Add to PATH". & exit /b 1)

python -m pip install --upgrade pip pyinstaller pystray pillow || exit /b 1
python -m unittest discover -p "test_*.py" || (echo Tests failed. Fix them before building. & exit /b 1)
python -m PyInstaller --clean --noconfirm RobloxAccountManager.spec || exit /b 1

set ISCC="%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if not exist %ISCC% set ISCC="%ProgramFiles%\Inno Setup 6\ISCC.exe"
if not exist %ISCC% set ISCC="%LocalAppData%\Programs\Inno Setup 6\ISCC.exe"
if not exist %ISCC% (
  echo Inno Setup 6 not found. Install it from jrsoftware.org, then run:  ISCC.exe installer.iss
  exit /b 1
)
%ISCC% installer.iss || exit /b 1
echo.
echo Done. Installer: installer_output\RobloxAccountManager-Setup-2.0.0.exe

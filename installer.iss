; Inno Setup 6 script: wraps dist\RobloxAccountManager.exe into one installer exe.
; Build order: 1) pyinstaller --clean --noconfirm RobloxAccountManager.spec
;              2) ISCC.exe installer.iss      (or open this file in Inno Setup and press F9)
; Result: installer_output\RobloxAccountManager-Setup-<version>.exe  <- this is the file you share.
; Keep MyAppVersion equal to version.py (test_release.py checks it).

#define MyAppName "Roblox Account Manager"
#define MyAppVersion "2.0.0"
#define MyAppExe "RobloxAccountManager.exe"

[Setup]
; AppId identifies this app for upgrades and uninstall. Never change it after you ship.
AppId={{43C84455-17A6-4E81-9234-AD720063B068}}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
VersionInfoVersion={#MyAppVersion}
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
; Per-user install by default (no admin prompt); the user can still choose "for all users".
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=installer_output
OutputBaseFilename=RobloxAccountManager-Setup-{#MyAppVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
LicenseFile=LICENSE
UninstallDisplayIcon={app}\{#MyAppExe}
; Offers to close a running copy during upgrade or uninstall.
CloseApplications=yes

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Shortcuts:"; Flags: unchecked
Name: "runasadmin"; Description: "Always run as administrator (recommended for multi-instance)"; GroupDescription: "Options:"

[Files]
Source: "dist\{#MyAppExe}"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExe}"
Name: "{group}\Uninstall {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExe}"; Tasks: desktopicon

[Registry]
; Windows' own "run this program as an administrator" compatibility flag. Removed on uninstall.
Root: HKCU; Subkey: "Software\Microsoft\Windows NT\CurrentVersion\AppCompatFlags\Layers"; ValueType: string; ValueName: "{app}\{#MyAppExe}"; ValueData: "~ RUNASADMIN"; Flags: uninsdeletevalue; Tasks: runasadmin

[Run]
Filename: "{app}\{#MyAppExe}"; Description: "Launch {#MyAppName}"; Flags: nowait postinstall skipifsilent shellexec

; Uninstall removes the program only. Accounts, settings and logs in %APPDATA%\RobloxAccountManager are
; deliberately kept so reinstalling never loses your encrypted cookies; delete that folder by hand to wipe them.

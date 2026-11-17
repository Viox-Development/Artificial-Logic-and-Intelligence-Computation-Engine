; ALICE Windows installer (Inno Setup 6), built by .github/workflows/windows.yml:
;   iscc /DAppVersion=X.Y.Z /DSourceDir=..\dist\app installer\alice.iss
; No admin rights: installs to %LOCALAPPDATA%\ALICE. The source (with git history, for auto-update) is inside the
; installer; install.ps1 then sets up Python, the venv and ALICE itself (internet needed). Optional features (voice,
; computer control, browser, background core, phone) are picked afterwards in the app's setup wizard.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef SourceDir
  #define SourceDir "..\dist\app"
#endif

[Setup]
AppId={{8F1C5E2A-6B7D-4A3E-9C21-5A1CE0000001}
AppName=ALICE
AppVersion={#AppVersion}
AppPublisher=Viox Development
DefaultDirName={localappdata}\ALICE
DefaultGroupName=ALICE
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=..\dist
OutputBaseFilename=ALICE-Setup
SetupIconFile=..\alice\app\static\alice.ico
UninstallDisplayIcon={app}\alice\app\static\alice.ico
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesInstallIn64BitMode=x64compatible

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"
Name: "turkish"; MessagesFile: "compiler:Languages\Turkish.isl"

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Run]
; Finds Python (installs it with winget if missing), creates .venv, installs ALICE and makes the shortcut. The window
; stays visible so the progress shows.
Filename: "powershell.exe"; \
  Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\install.ps1"" -FromInstaller"; \
  WorkingDir: "{app}"; StatusMsg: "Installing Python and ALICE..."; Flags: waituntilterminated
; Then ALICE opens on its setup wizard: language, a model, and the optional features to install.
Filename: "{app}\.venv\Scripts\pythonw.exe"; Parameters: "-m alice app"; WorkingDir: "{app}"; \
  Description: "Open ALICE and finish setup"; Flags: postinstall nowait skipifsilent skipifdoesntexist

[UninstallRun]
; Stop the core so the files can be deleted, and remove its autostart entry; user data (~\.alice) is kept.
Filename: "{app}\.venv\Scripts\python.exe"; Parameters: "-c ""from alice.app.system import stop_core; stop_core()"""; Flags: runhidden skipifdoesntexist; RunOnceId: "StopCore"
Filename: "{app}\.venv\Scripts\python.exe"; Parameters: "-m alice core uninstall"; Flags: runhidden skipifdoesntexist; RunOnceId: "Autostart"

[UninstallDelete]
Type: filesandordirs; Name: "{app}\.venv"
Type: filesandordirs; Name: "{app}"

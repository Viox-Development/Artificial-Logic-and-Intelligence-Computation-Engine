; ALICE Windows kurulumu (Inno Setup 6). GitHub Actions'ta derlenir (.github/workflows/windows.yml):
;   iscc /DAppVersion=0.3.N /DSourceDir=..\dist\app installer\alice.iss
; Yönetici izni istemez: %LOCALAPPDATA%\ALICE'ye kurar. Kaynak kod (git geçmişiyle, otomatik güncelleme için) kurulumun
; içindedir; Python, sanal ortam ve paketler kurulumun sonunda install.ps1 ile hazırlanır (internet gerekir).

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
Name: "turkish"; MessagesFile: "compiler:Languages\Turkish.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "autostart"; Description: "Oturum açılınca ALICE arka planda başlasın (sesli asistan, bildirimler)"; Flags: checkedonce

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Run]
; Python'u bulur (yoksa winget ile kurar), .venv açar, paketleri kurar, kısayolları oluşturur. Görünür pencere:
; kurulum birkaç dakika sürer, kullanıcı ilerlemeyi görsün.
Filename: "powershell.exe"; \
  Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\install.ps1"" -FromInstaller {code:AutostartArg}"; \
  WorkingDir: "{app}"; StatusMsg: "Python ve paketler kuruluyor (birkaç dakika sürebilir)..."; Flags: waituntilterminated
Filename: "{app}\.venv\Scripts\pythonw.exe"; Parameters: "-m alice app"; WorkingDir: "{app}"; \
  Description: "ALICE'i şimdi aç"; Flags: postinstall nowait skipifsilent skipifdoesntexist

[UninstallRun]
; Çekirdeği kapat (dosyalar silinebilsin); kullanıcı verisi (~\.alice) silinmez.
Filename: "{app}\.venv\Scripts\python.exe"; Parameters: "-c ""from alice.app.system import stop_core; stop_core()"""; Flags: runhidden skipifdoesntexist; RunOnceId: "StopCore"
Filename: "{app}\.venv\Scripts\python.exe"; Parameters: "-m alice core uninstall"; Flags: runhidden skipifdoesntexist; RunOnceId: "Autostart"

[UninstallDelete]
Type: filesandordirs; Name: "{app}\.venv"
Type: filesandordirs; Name: "{app}"

[Code]
function AutostartArg(Param: String): String;
begin
  if WizardIsTaskSelected('autostart') then
    Result := '-Autostart'
  else
    Result := '';
end;

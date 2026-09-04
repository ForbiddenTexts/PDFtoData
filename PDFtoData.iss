; ---------------------------------------------------------------------------
;  PDF to Data - Inno Setup script
;
;  PER-USER install by design (%LOCALAPPDATA%\Programs\PDFtoData):
;    * no admin rights and no UAC prompt to install
;    * the in-app updater can therefore write to the install folder itself,
;      which is the whole reason we do not install to Program Files
;
;  Build:  ISCC.exe PDFtoData.iss /DAppVersion=1.0.0
;  (build_release.ps1 passes /DAppVersion and /DBundleDir for you)
; ---------------------------------------------------------------------------

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef BundleDir
  #define BundleDir "dist\bundle"
#endif

#define AppName        "PDF to Data"
#define AppSlug        "PDFtoData"
#define AppPublisher   "PDF to Data"
#define AppExe         "PDFtoData.exe"

[Setup]
AppId={{8C2D9E71-4F3A-4C58-9B6E-5A1D7E0F2B44}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
VersionInfoVersion={#AppVersion}

; --- per-user: no admin, no UAC, updater can write here ---------------------
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
DefaultDirName={localappdata}\Programs\{#AppSlug}
DisableDirPage=no
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
UsePreviousAppDir=yes

OutputDir=dist
OutputBaseFilename={#AppSlug}-Setup-{#AppVersion}
Compression=lzma2/max
SolidCompression=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
WizardStyle=modern
LicenseFile=LICENSE.txt
UninstallDisplayName={#AppName}
UninstallDisplayIcon={app}\{#AppExe}
; The bundle carries an embedded Python and a JRE - it is large.
DiskSpanning=no

#ifexist "assets\icon.ico"
SetupIconFile=assets\icon.ico
#endif

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon";  Description: "Create a &desktop shortcut";      GroupDescription: "Shortcuts:"
Name: "startmenu";    Description: "Create a &Start Menu shortcut";   GroupDescription: "Shortcuts:"; Flags: checkedonce

[Files]
; The whole assembled bundle: exe + runtime\python + runtime\jre + version.json
; + update_helper.ps1. recursesubdirs/createallsubdirs keeps the tree intact.
Source: "{#BundleDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#AppName}";              Filename: "{app}\{#AppExe}"; Tasks: startmenu
Name: "{group}\Uninstall {#AppName}";    Filename: "{uninstallexe}";  Tasks: startmenu
Name: "{autodesktop}\{#AppName}";        Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExe}"; Description: "&Launch {#AppName} now"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; Update leftovers that are created after install. User data in %APPDATA%\PDFtoData
; is deliberately NOT removed - settings and logs survive an uninstall.
Type: filesandordirs; Name: "{app}\.backup-*"
Type: files;          Name: "{app}\.started-ok"
Type: files;          Name: "{app}\.update-failed.json"
Type: files;          Name: "{app}\*.old"
Type: filesandordirs; Name: "{app}\runtime"

[Code]
// Warn (once) if the app is running, since setup cannot replace a locked exe.
function InitializeSetup(): Boolean;
begin
  Result := True;
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then
  begin
    // Nothing extra to do: version.json ships inside the bundle.
  end;
end;

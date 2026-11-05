; Installer for Snapshots (Inno Setup 6). The workflow builds it after the exe:
;     iscc /DAppVersion=0.14.1 installer\snapshots.iss      ->  dist\Snapshots-Setup.exe
; It installs for the current user only (no administrator rights, no UAC prompt), and Windows lists it
; under Settings > Apps. Your photos and Snapshots' data (%LOCALAPPDATA%\Snapshots) are never touched
; by installing, upgrading or uninstalling, unless you say so when uninstalling.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

[Setup]
; The same id on every version: a newer installer upgrades the installed copy in place.
AppId={{A1BF8561-0908-4AA1-9C6E-D898DFFD42BA}
AppName=Snapshots
AppVersion={#AppVersion}
AppVerName=Snapshots {#AppVersion}
AppPublisher=Hans De Kryger
AppPublisherURL=https://github.com/TheDutchMan13/Snapshots
AppSupportURL=https://github.com/TheDutchMan13/Snapshots/issues
AppUpdatesURL=https://github.com/TheDutchMan13/Snapshots/releases
VersionInfoVersion={#AppVersion}.0
VersionInfoDescription=Snapshots setup
; Per user: C:\Users\<you>\AppData\Local\Programs\Snapshots. "dialog" lets someone choose "for all users".
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
DefaultDirName={autopf}\Snapshots
DisableProgramGroupPage=yes
DisableDirPage=auto
OutputDir=..\dist
OutputBaseFilename=Snapshots-Setup
SetupIconFile=..\assets\icon.ico
UninstallDisplayIcon={app}\Snapshots.exe
UninstallDisplayName=Snapshots
LicenseFile=..\LICENSE
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64
ArchitecturesInstallIn64BitMode=x64
MinVersion=10.0
; Upgrading while Snapshots is open: it is asked to close, and not started again by the installer.
CloseApplications=yes
RestartApplications=no
UsedUserAreasWarning=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Put an icon on the &desktop"; GroupDescription: "Icons:"; Flags: unchecked

[Files]
Source: "..\dist\Snapshots.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\LICENSE"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
; The Start menu icon is the same "Snapshots.lnk" the app's own Settings page makes.
Name: "{autoprograms}\Snapshots"; Filename: "{app}\Snapshots.exe"; WorkingDir: "{app}"
Name: "{autodesktop}\Snapshots"; Filename: "{app}\Snapshots.exe"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{app}\Snapshots.exe"; Description: "Open Snapshots"; Flags: nowait postinstall skipifsilent runasoriginaluser

[UninstallRun]
; A running Snapshots would keep its files in use.
Filename: "{sys}\taskkill.exe"; Parameters: "/F /IM Snapshots.exe"; Flags: runhidden; RunOnceId: "StopSnapshots"

[Code]
const
  DataQuestion = 'Also delete what Snapshots keeps about your library (its database, thumbnails, ' +
    'accounts and settings)?' + #13#10 + #13#10 +
    'Your photos and videos are never deleted. If you keep this data, installing Snapshots again ' +
    'picks up where you left off.';

function DataFolder: String;
begin
  Result := ExpandConstant('{localappdata}\Snapshots');
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usPostUninstall then
  begin
    // The "Start with Windows" switch in Snapshots' Settings lives in the current user's Run key.
    RegDeleteValue(HKCU, 'Software\Microsoft\Windows\CurrentVersion\Run', 'Snapshots');
    RegDeleteValue(HKCU, 'Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run', 'Snapshots');
    // Asked, never assumed (and never in a silent uninstall).
    if (not UninstallSilent) and DirExists(DataFolder) then
      if MsgBox(DataQuestion, mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES then
        DelTree(DataFolder, True, True, True);
  end;
end;

; Inno Setup script. Compile via build.ps1 (it passes /DAppVersion=<version>).
; Installs per user without administrator rights; settings and passwords are NOT removed on uninstall.
#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

[Setup]
AppId={{B7C1A3E2-5D4F-4A6B-9C8E-2F1D0A7B3C54}
AppName=Intercom Gateway
AppVersion={#AppVersion}
AppPublisher=Carlo Keysser
DefaultDirName={autopf}\Intercom Gateway
DefaultGroupName=Intercom Gateway
UninstallDisplayIcon={app}\IntercomGateway.exe
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
Compression=lzma2
SolidCompression=yes
OutputDir=..\dist\installer
OutputBaseFilename=intercom-gateway-{#AppVersion}-setup
WizardStyle=modern
; a running gateway (the app started itself in the background) locks the files: close it for the update
CloseApplications=yes
RestartApplications=no
LicenseFile=..\LICENSE

[Languages]
Name: "german"; MessagesFile: "compiler:Languages\German.isl"

[Tasks]
Name: "desktopicon"; Description: "Verknüpfung auf dem Desktop anlegen"; Flags: unchecked
Name: "autostart"; Description: "Intercom Gateway beim Anmelden automatisch öffnen"; Flags: unchecked

[Files]
Source: "..\dist\IntercomGateway\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion
Source: "..\gateway.example.toml"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\README.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\LICENSE"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\Intercom Gateway"; Filename: "{app}\IntercomGateway.exe"
Name: "{group}\Intercom Gateway deinstallieren"; Filename: "{uninstallexe}"
Name: "{autodesktop}\Intercom Gateway"; Filename: "{app}\IntercomGateway.exe"; Tasks: desktopicon

[Registry]
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "IntercomGateway"; \
  ValueData: """{app}\IntercomGateway.exe"""; Flags: uninsdeletevalue; Tasks: autostart

[Run]
Filename: "{app}\IntercomGateway.exe"; Description: "Intercom Gateway starten"; Flags: nowait postinstall skipifsilent

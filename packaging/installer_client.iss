; Inno Setup script of the intercom client. Compile via build-client.ps1 (it passes /DAppVersion=<version>).
; Installs per user without administrator rights; settings and passwords are NOT removed on uninstall.
#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

[Setup]
AppId={{C962A667-77BF-48C2-9029-D7B0D399178D}
AppName=Intercom Client
AppVersion={#AppVersion}
AppPublisher=Carlo Keysser
DefaultDirName={autopf}\Intercom Client
DefaultGroupName=Intercom Client
UninstallDisplayIcon={app}\IntercomClient.exe
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
Compression=lzma2
SolidCompression=yes
OutputDir=..\dist\installer
OutputBaseFilename=intercom-client-{#AppVersion}-setup
WizardStyle=modern
; a running client locks its files: close it for the update
CloseApplications=yes
RestartApplications=no
LicenseFile=..\LICENSE

[Languages]
Name: "german"; MessagesFile: "compiler:Languages\German.isl"

[Tasks]
Name: "desktopicon"; Description: "Verknüpfung auf dem Desktop anlegen"; Flags: unchecked
Name: "autostart"; Description: "Intercom Client beim Anmelden automatisch öffnen"; Flags: unchecked

[Files]
Source: "..\dist\IntercomClient\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion
Source: "..\client.example.toml"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\README.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\LICENSE"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\Intercom Client"; Filename: "{app}\IntercomClient.exe"
Name: "{group}\Intercom Client deinstallieren"; Filename: "{uninstallexe}"
Name: "{autodesktop}\Intercom Client"; Filename: "{app}\IntercomClient.exe"; Tasks: desktopicon

[Registry]
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "IntercomClient"; \
  ValueData: """{app}\IntercomClient.exe"""; Flags: uninsdeletevalue; Tasks: autostart

[Run]
Filename: "{app}\IntercomClient.exe"; Description: "Intercom Client starten"; Flags: nowait postinstall skipifsilent

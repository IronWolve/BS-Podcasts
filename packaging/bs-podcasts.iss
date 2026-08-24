#define MyAppName "BS Podcasts"
#define MyAppVersion "0.1.0"
#ifndef SourceExe
  #error SourceExe must point to the built BS Podcasts.exe
#endif
#ifndef OutputDir
  #define OutputDir "."
#endif

[Setup]
AppId={{D718D0CE-3E52-4C48-B3D3-4F8A019C7544}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
DefaultDirName={localappdata}\Programs\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
OutputDir={#OutputDir}
OutputBaseFilename=BS-Podcasts-Setup
Compression=lzma2
SolidCompression=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayIcon={app}\BS Podcasts.exe
WizardStyle=modern

[Files]
Source: "{#SourceExe}"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\BS Podcasts.exe"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\BS Podcasts.exe"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional shortcuts:"; Flags: unchecked

[Run]
Filename: "{app}\BS Podcasts.exe"; Description: "Launch {#MyAppName}"; Flags: nowait postinstall skipifsilent

; Inno Setup 6 script for IDE Lyrics Studio
; Compile after running build-exe.ps1:
;   iscc desktop-app\installer.iss

#define MyAppName "IDE Lyrics Studio"
#define MyAppVersion "1.8.4"
#define MyAppPublisher "IDE Lyrics Studio"
#define MyAppExeName "IDE-Lyrics-Studio.exe"

[Setup]
AppId={{8F4C2A6E-9B31-4D7E-A5C0-3E7B1F94D208}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={localappdata}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
AllowNoIcons=yes
PrivilegesRequired=lowest
LicenseFile=..\LICENSE
OutputDir=dist
OutputBaseFilename=IDE-Lyrics-Studio-Setup-{#MyAppVersion}
#ifexist "ide-lyrics.ico"
SetupIconFile=ide-lyrics.ico
#endif
UninstallDisplayIcon={app}\{#MyAppExeName}
UninstallDisplayName={#MyAppName}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
RestartApplications=no
#if VER >= 0x06030000
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
#else
ArchitecturesAllowed=x64
ArchitecturesInstallIn64BitMode=x64
#endif

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked
Name: "autostart"; Description: "Launch {#MyAppName} at Windows startup"; GroupDescription: "Startup:"; Flags: unchecked

[Files]
Source: "dist\{#MyAppExeName}"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\LICENSE"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Parameters: "--show"; WorkingDir: "{app}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Parameters: "--show"; WorkingDir: "{app}"; Tasks: desktopicon

[Registry]
; Uses the same HKCU Run value name ("IDELyricsStudio") as the app's tray toggle
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "IDELyricsStudio"; ValueData: """{app}\{#MyAppExeName}"" --tray"; Flags: uninsdeletevalue; Tasks: autostart
; Remove the startup entry on uninstall even if the user enabled it later inside the app
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: none; ValueName: "IDELyricsStudio"; Flags: uninsdeletevalue; Tasks: not autostart

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(MyAppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent

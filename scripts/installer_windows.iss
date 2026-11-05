; Compiled only on the GitHub-hosted Windows runner. The installed application
; keeps the verified onedir layout required by the managed tunnel subprocess.
#ifndef AppVersion
  #define AppVersion "0.1.0"
#endif

[Setup]
AppId={{44AC23ED-EB22-4A42-BB8A-5CF6806DC748}
AppName=Chill
AppVersion={#AppVersion}
DefaultDirName={autopf}\Chill
DefaultGroupName=Chill
DisableProgramGroupPage=yes
OutputDir=..\dist
OutputBaseFilename=Chill-Setup
SetupIconFile=..\ui\assets\chill.ico
UninstallDisplayIcon={app}\Chill.exe
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=admin
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
CloseApplications=yes
RestartApplications=no

[Files]
Source: "..\dist\Chill\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\Chill"; Filename: "{app}\Chill.exe"
Name: "{group}\Uninstall Chill"; Filename: "{uninstallexe}"

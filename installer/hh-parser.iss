; hh-parser.iss
; Inno Setup script: делает HH-Parser-Setup.exe из dist\HH-Parser.exe

#define MyAppName "HH: Парсер базы компаний"
#define MyAppVersion "0.1.0"
#define MyAppPublisher "VickentyZherebov"
#define MyAppExeName "HH-Parser.exe"
#define SourceExe "dist\HH-Parser.exe"

[Setup]
AppId={{8C8F9D36-3F24-4B69-9F15-3A4B0F7D5C11}}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}

DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}

OutputDir=dist-installer
OutputBaseFilename=HH-Parser-Setup

Compression=lzma2
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=admin

[Tasks]
Name: "desktopicon"; Description: "Создать ярлык на рабочем столе"; GroupDescription: "Ярлыки:"; Flags: unchecked

[Files]
Source: "{#SourceExe}"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Запустить {#MyAppName}"; Flags: nowait postinstall skipifsilent

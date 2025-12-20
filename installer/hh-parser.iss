; installer/hh-parser.iss
; Inno Setup script: собираем Setup.exe из результата flet build windows

#define MyAppName "HH: Парсер базы компаний"
#define MyAppVersion "0.1.0"
#define MyAppPublisher "VickentyZherebov"
#define MyAppExeName "hh-parser.exe"     ; <-- если exe называется иначе, поменяй
#define BuildDir "build\windows"         ; <-- flet build кладёт сюда результат :contentReference[oaicite:2]{index=2}

[Setup]
AppId={{8C8F9D36-3F24-4B69-9F15-3A4B0F7D5C11}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}

DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}

; Куда положить Setup.exe (относительно корня репы)
OutputDir=dist-installer
OutputBaseFilename=HH-Parser-Setup
Compression=lzma2
SolidCompression=yes

; Если хочешь установку строго в Program Files 64-bit:
ArchitecturesAllowed=x64
ArchitecturesInstallIn64BitMode=x64

; Убирает "This will install into Program Files (x86)" сюрпризы
DisableProgramGroupPage=yes

[Tasks]
Name: "desktopicon"; Description: "Создать ярлык на рабочем столе"; GroupDescription: "Ярлыки:"; Flags: unchecked

[Files]
; Берём всю папку build\windows со всем содержимым
; createallsubdirs нужен, чтобы создавались пустые подпапки (если они есть). :contentReference[oaicite:3]{index=3}
Source: "{#BuildDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Запустить {#MyAppName}"; Flags: nowait postinstall skipifsilent

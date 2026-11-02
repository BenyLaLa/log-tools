#ifndef AppVersion
  #define AppVersion "0.2.3"
#endif
#ifndef BundleDir
  #define BundleDir "..\dist\desktop\LogTools"
#endif
#ifndef OutputDir
  #define OutputDir "..\dist"
#endif
[Setup]
AppId={{85697C7E-449A-4465-8A54-317BA93AC2E9}
AppName=LogTools
AppVersion={#AppVersion}
AppPublisher=LogTools
DefaultDirName={localappdata}\Programs\LogTools
DefaultGroupName=LogTools
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir={#OutputDir}
OutputBaseFilename=LogTools-Setup-{#AppVersion}-x64
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\LogTools.exe
CloseApplications=yes
RestartApplications=no
SetupLogging=yes
MinVersion=10.0

[Languages]
Name: chinese; MessagesFile: "compiler:Default.isl,ChineseSimplified.isl"

[Files]
Source: "{#BundleDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\LogTools"; Filename: "{app}\LogTools.exe"
Name: "{autodesktop}\LogTools"; Filename: "{app}\LogTools.exe"; Tasks: desktopicon

[Tasks]
Name: desktopicon; Description: "创建桌面快捷方式"; Flags: unchecked

[Run]
Filename: "{app}\LogTools.exe"; Description: "打开 LogTools 配置界面"; Flags: nowait postinstall skipifsilent

; User settings, Windows credentials and logs live outside {app} and are preserved.

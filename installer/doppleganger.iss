; Doppleganger installer (Inno Setup 6). Built on CI:  iscc /DMyAppVersion=0.1.42 installer\doppleganger.iss
;
; - Per-user install (no admin) to %LOCALAPPDATA%\Programs\Doppleganger, so the app can update itself.
; - Start-menu shortcut (+ optional desktop shortcut) and an entry in Settings > Apps.
; - Uninstalling removes the Doppleganger camera too. Your loops and settings in
;   %APPDATA%\Doppleganger are kept, in case you reinstall.
; - Optional: trust the Doppleganger publisher certificate on this PC (self-signed),
;   so Windows shows "Doppleganger" as a known publisher for this app.

#ifndef MyAppVersion
  #define MyAppVersion "0.0.0"
#endif
#define MyAppName "Doppleganger"
#define MyAppExe "Doppleganger.exe"
#define CertSubject "Doppleganger"

[Setup]
AppId={{15FD5392-9957-4C70-8B19-BF99EF9DC62C}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher=busybeavernc
AppPublisherURL=https://github.com/busybeavernc/doppleganger
AppSupportURL=https://github.com/busybeavernc/doppleganger/issues
AppComments=A seamless switch.
DefaultDirName={localappdata}\Programs\{#MyAppName}
DisableDirPage=yes
DisableProgramGroupPage=yes
DisableReadyPage=yes
PrivilegesRequired=lowest
OutputDir=..\dist
OutputBaseFilename=DopplegangerSetup
SetupIconFile=..\doppleganger\assets\icon.ico
UninstallDisplayIcon={app}\{#MyAppExe}
UninstallDisplayName={#MyAppName}
WizardStyle=modern
WizardImageFile=wizard.bmp,wizard-2x.bmp
WizardSmallImageFile=wizard-small.bmp,wizard-small-2x.bmp
Compression=lzma2/max
SolidCompression=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
CloseApplications=force
RestartApplications=no

[Messages]
WelcomeLabel2=This installs [name/ver] on your computer.%n%nDoppleganger records a seamless loop of you and lets you switch between it and your live webcam in Zoom, Teams and more.%n%nA seamless switch.

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; Flags: unchecked
Name: "trustpublisher"; Description: "Trust the Doppleganger publisher on this PC (removes the ""unknown publisher"" warning for Doppleganger only; Windows will ask you to confirm)"; Flags: unchecked

[Files]
Source: "..\dist\{#MyAppExe}"; DestDir: "{app}"; Flags: ignoreversion
Source: "doppleganger-publisher.cer"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{userprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExe}"; Comment: "A seamless switch."
Name: "{userdesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExe}"; Tasks: desktopicon

[Run]
Filename: "{sys}\certutil.exe"; Parameters: "-user -addstore Root ""{app}\doppleganger-publisher.cer"""; \
  StatusMsg: "Trusting the Doppleganger publisher..."; Flags: runhidden waituntilterminated; Tasks: trustpublisher
Filename: "{sys}\certutil.exe"; Parameters: "-user -addstore TrustedPublisher ""{app}\doppleganger-publisher.cer"""; \
  Flags: runhidden waituntilterminated; Tasks: trustpublisher
; The HappyHacks Virtual Camera (what Zoom/Teams use). Windows asks for permission once.
Filename: "{app}\{#MyAppExe}"; Parameters: "--install-camera"; StatusMsg: "Installing the HappyHacks Virtual Camera (approve the Windows prompt)..."; \
  Flags: waituntilterminated skipifsilent
Filename: "{app}\{#MyAppExe}"; Description: "Open Doppleganger"; Flags: nowait postinstall skipifsilent

[UninstallRun]
; Remove the HappyHacks Virtual Camera (Windows asks for permission if it's installed).
Filename: "{app}\{#MyAppExe}"; Parameters: "--remove-camera"; Flags: waituntilterminated; RunOnceId: "RemoveCamera"
; Stop trusting the publisher certificate (no-op if it was never trusted).
Filename: "{sys}\certutil.exe"; Parameters: "-user -delstore TrustedPublisher ""{#CertSubject}"""; Flags: runhidden waituntilterminated; RunOnceId: "UntrustPublisher"
Filename: "{sys}\certutil.exe"; Parameters: "-user -delstore Root ""{#CertSubject}"""; Flags: runhidden waituntilterminated; RunOnceId: "UntrustRoot"

[Registry]
; "Start Doppleganger when Windows starts" (set from the app's Settings) is removed on uninstall.
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: none; ValueName: "Doppleganger"; \
  Flags: uninsdeletevalue dontcreatekey

[UninstallDelete]
Type: files; Name: "{app}\Doppleganger.exe.new"

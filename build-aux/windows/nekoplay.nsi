; nekoplay.nsi
;
; SPDX-License-Identifier: GPL-3.0-or-later
;
; Wraps the portable folder built by bundle.sh into an installer.
; Driven by installer.sh, which passes the paths and version in.

Unicode true
ManifestDPIAware true

!include "MUI2.nsh"
!include "x64.nsh"
!include "LogicLib.nsh"
!include "FileFunc.nsh"

!ifndef APP_VERSION
  !define APP_VERSION "0.0.0"
!endif
!ifndef DIST_DIR
  !error "DIST_DIR must be passed with -DDIST_DIR=..."
!endif
!ifndef SRC_DIR
  !error "SRC_DIR must be passed with -DSRC_DIR=..."
!endif
!ifndef OUT_FILE
  !define OUT_FILE "NekoPlay-Setup.exe"
!endif

!define APP_NAME "NekoPlay"
!define APP_PUBLISHER "Nyarch Linux"
!define APP_EXE "nekoplay.exe"
!define APP_URL "https://github.com/NyarchLinux/NekoPlay"
!define UNINST_KEY "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP_NAME}"
!define PROGID "NekoPlay.Video"

Name "${APP_NAME} ${APP_VERSION}"
OutFile "${OUT_FILE}"
InstallDir "$PROGRAMFILES64\${APP_NAME}"
InstallDirRegKey HKLM "Software\${APP_NAME}" "InstallDir"
RequestExecutionLevel admin
SetCompressor /SOLID lzma
SetCompressorDictSize 64

VIAddVersionKey "ProductName" "${APP_NAME}"
VIAddVersionKey "CompanyName" "${APP_PUBLISHER}"
VIAddVersionKey "FileDescription" "${APP_NAME} Setup"
VIAddVersionKey "FileVersion" "${APP_VERSION}"
VIAddVersionKey "ProductVersion" "${APP_VERSION}"
VIAddVersionKey "LegalCopyright" "GPL-3.0-or-later"
VIProductVersion "${APP_VERSION}.0"

!define MUI_ICON "${SRC_DIR}\src\nekoplay.ico"
!define MUI_UNICON "${SRC_DIR}\src\nekoplay.ico"
!define MUI_ABORTWARNING
!define MUI_FINISHPAGE_RUN "$INSTDIR\${APP_EXE}"
!define MUI_FINISHPAGE_RUN_TEXT "Play something"

!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_LICENSE "${SRC_DIR}\LICENSE"
!insertmacro MUI_PAGE_COMPONENTS
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH

!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES

!insertmacro MUI_LANGUAGE "English"

; Video containers only. Audio and images are left alone: this registers
; NekoPlay as an option under Open With rather than seizing the default,
; which is the most an installer is allowed to do since Windows 8 anyway.
!macro EachVideoExt macro
  !insertmacro ${macro} ".mkv"
  !insertmacro ${macro} ".mp4"
  !insertmacro ${macro} ".m4v"
  !insertmacro ${macro} ".avi"
  !insertmacro ${macro} ".mov"
  !insertmacro ${macro} ".webm"
  !insertmacro ${macro} ".wmv"
  !insertmacro ${macro} ".flv"
  !insertmacro ${macro} ".mpg"
  !insertmacro ${macro} ".mpeg"
  !insertmacro ${macro} ".ts"
  !insertmacro ${macro} ".m2ts"
  !insertmacro ${macro} ".ogv"
!macroend

!macro RegisterExt ext
  WriteRegStr HKLM "Software\Classes\${ext}\OpenWithProgids" "${PROGID}" ""
!macroend

!macro UnregisterExt ext
  DeleteRegValue HKLM "Software\Classes\${ext}\OpenWithProgids" "${PROGID}"
!macroend

Function .onInit
  ${IfNot} ${RunningX64}
    MessageBox MB_ICONSTOP "${APP_NAME} needs 64-bit Windows."
    Abort
  ${EndIf}
  SetRegView 64
  ; This installs under Program Files and writes to HKLM, so its shortcuts
  ; belong to every account on the machine rather than whichever one
  ; happened to answer the elevation prompt.
  SetShellVarContext all

  ; Clear out an older install first, so its files cannot outlive it.
  ReadRegStr $0 HKLM "${UNINST_KEY}" "UninstallString"
  ${If} $0 != ""
    ; A silent run has nobody to answer a prompt, so it just replaces.
    ${IfNot} ${Silent}
      MessageBox MB_OKCANCEL|MB_ICONQUESTION \
        "${APP_NAME} is already installed. Remove the old version first?" \
        IDOK +2
      Abort
    ${EndIf}
    ReadRegStr $1 HKLM "${UNINST_KEY}" "InstallLocation"
    ExecWait '"$0" /S _?=$1'
    Delete "$0"
    RMDir "$1"
  ${EndIf}
FunctionEnd

Section "${APP_NAME}" SecCore
  SectionIn RO
  SetRegView 64
  SetOutPath "$INSTDIR"
  File /r "${DIST_DIR}\*.*"

  WriteRegStr HKLM "Software\${APP_NAME}" "InstallDir" "$INSTDIR"

  ; The player itself, for the Open With list and the association section.
  WriteRegStr HKLM "Software\Classes\${PROGID}" "" "Video"
  WriteRegStr HKLM "Software\Classes\${PROGID}\DefaultIcon" "" "$INSTDIR\${APP_EXE},0"
  WriteRegStr HKLM "Software\Classes\${PROGID}\shell\open\command" "" '"$INSTDIR\${APP_EXE}" "%1"'
  WriteRegStr HKLM "Software\Classes\Applications\${APP_EXE}\shell\open\command" "" '"$INSTDIR\${APP_EXE}" "%1"'

  CreateShortcut "$SMPROGRAMS\${APP_NAME}.lnk" "$INSTDIR\${APP_EXE}"

  WriteUninstaller "$INSTDIR\Uninstall.exe"

  WriteRegStr HKLM "${UNINST_KEY}" "DisplayName" "${APP_NAME}"
  WriteRegStr HKLM "${UNINST_KEY}" "DisplayVersion" "${APP_VERSION}"
  WriteRegStr HKLM "${UNINST_KEY}" "DisplayIcon" "$INSTDIR\${APP_EXE},0"
  WriteRegStr HKLM "${UNINST_KEY}" "Publisher" "${APP_PUBLISHER}"
  WriteRegStr HKLM "${UNINST_KEY}" "URLInfoAbout" "${APP_URL}"
  WriteRegStr HKLM "${UNINST_KEY}" "InstallLocation" "$INSTDIR"
  WriteRegStr HKLM "${UNINST_KEY}" "UninstallString" '"$INSTDIR\Uninstall.exe"'
  WriteRegStr HKLM "${UNINST_KEY}" "QuietUninstallString" '"$INSTDIR\Uninstall.exe" /S'
  WriteRegDWORD HKLM "${UNINST_KEY}" "NoModify" 1
  WriteRegDWORD HKLM "${UNINST_KEY}" "NoRepair" 1

  ${GetSize} "$INSTDIR" "/S=0K" $0 $1 $2
  IntFmt $0 "0x%08X" $0
  WriteRegDWORD HKLM "${UNINST_KEY}" "EstimatedSize" "$0"
SectionEnd

Section "Desktop shortcut" SecDesktop
  CreateShortcut "$DESKTOP\${APP_NAME}.lnk" "$INSTDIR\${APP_EXE}"
SectionEnd

Section "Offer to open video files" SecAssoc
  SetRegView 64
  !insertmacro EachVideoExt RegisterExt
  System::Call 'shell32::SHChangeNotify(i 0x08000000, i 0, i 0, i 0)'
SectionEnd

!insertmacro MUI_FUNCTION_DESCRIPTION_BEGIN
  !insertmacro MUI_DESCRIPTION_TEXT ${SecCore} \
    "The player, and everything it needs to run."
  !insertmacro MUI_DESCRIPTION_TEXT ${SecDesktop} \
    "Put a shortcut on the desktop."
  !insertmacro MUI_DESCRIPTION_TEXT ${SecAssoc} \
    "List NekoPlay under Open With for common video files. Your current \
default player is left as it is."
!insertmacro MUI_FUNCTION_DESCRIPTION_END

Function un.onInit
  SetRegView 64
  SetShellVarContext all
FunctionEnd

Section "Uninstall"
  SetRegView 64

  ; Only wipe a directory that actually holds this app.
  ${If} ${FileExists} "$INSTDIR\${APP_EXE}"
    RMDir /r "$INSTDIR"
  ${EndIf}

  Delete "$SMPROGRAMS\${APP_NAME}.lnk"
  Delete "$DESKTOP\${APP_NAME}.lnk"

  !insertmacro EachVideoExt UnregisterExt
  DeleteRegKey HKLM "Software\Classes\${PROGID}"
  DeleteRegKey HKLM "Software\Classes\Applications\${APP_EXE}"
  DeleteRegKey HKLM "Software\${APP_NAME}"
  DeleteRegKey HKLM "${UNINST_KEY}"

  System::Call 'shell32::SHChangeNotify(i 0x08000000, i 0, i 0, i 0)'

  ; Settings live in the registry and the config folder under
  ; %LOCALAPPDATA%; both are left alone so a reinstall picks them back up.
SectionEnd

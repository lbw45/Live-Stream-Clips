; NSIS 安装包脚本
; 产物：live-stream-clips-<version>.exe（单文件自解压安装包）
;
; 打包内容（不把 runtime 打进包）：
;   程序本体（launcher\LiveStreamClips.exe + _internal\）+ app\ 源码 + 启停脚本 + 卸载程序
;   runtime（python / ffmpeg / cuda / models）常驻工作区目录，不在安装包内。
;
; 用法（需安装 NSIS，makensis 在 PATH）：
;   makensis /DVERSION=1.0.0 build\installer.nsi

Unicode true
!include "MUI2.nsh"

; ---- 基础信息 ----
!ifndef VERSION
  !define VERSION "1.0.0"
!endif
!define APPNAME "LiveStreamClips"
!define APPID "LiveStreamClips"
!define COMPANY "LiveStreamClips"
!define INSTALLER_NAME "live-stream-clips-${VERSION}.exe"
!define WORKSPACE_ROOT "$PROFILE\LiveStreamClips-workspace"   ; 工作区常驻，安装/卸载都不动它

Name "${APPNAME}"
Icon "${ICON_FILE}"
UninstallIcon "${ICON_FILE}"
OutFile "${INSTALLER_NAME}"
InstallDir "$PROGRAMFILES\${APPID}"
InstallDirRegKey HKCU "Software\${APPID}" "InstallDir"
RequestExecutionLevel user          ; 当前用户安装，无需管理员
SetCompressor /SOLID lzma

; ---- 现代安装向导 ----
!define MUI_ABORTWARNING
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!define MUI_FINISHPAGE_RUN "$INSTDIR\launcher\LiveStreamClips.exe"
!define MUI_FINISHPAGE_RUN_TEXT "立即启动 ${APPNAME}"
!insertmacro MUI_PAGE_FINISH
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_LANGUAGE "SimpChinese"

Section "install"
  SetOutPath "$INSTDIR"
  ; 程序本体：桌面 exe + _internal（PyInstaller onedir 产物）+ app 源码 + 启停脚本
  File /r "..\launcher\LiveStreamClips.exe"
  File /r "..\launcher\_internal"
  File /r "..\app"
  File /r "..\launcher\.runtime"
  File "..\launcher\orchestrator.py"

  ; 写注册表（当前用户，卸载项 + 安装目录）
  WriteRegStr HKCU "Software\${APPID}" "InstallDir" "$INSTDIR"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APPID}" \
    "DisplayName" "${APPNAME}"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APPID}" \
    "DisplayVersion" "${VERSION}"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APPID}" \
    "Publisher" "${COMPANY}"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APPID}" \
    "UninstallString" '"$INSTDIR\Uninstall LiveStreamClips.exe"'

  ; 快捷方式（桌面 + 开始菜单）
  CreateShortCut "$DESKTOP\${APPNAME}.lnk" "$INSTDIR\launcher\LiveStreamClips.exe"
  CreateDirectory "$SMPROGRAMS\${APPNAME}"
  CreateShortCut "$SMPROGRAMS\${APPNAME}\${APPNAME}.lnk" "$INSTDIR\launcher\LiveStreamClips.exe"
  CreateShortCut "$SMPROGRAMS\${APPNAME}\Uninstall LiveStreamClips.lnk" \
    "$INSTDIR\Uninstall LiveStreamClips.exe"

  ; 卸载程序
  WriteUninstaller "$INSTDIR\Uninstall LiveStreamClips.exe"
SectionEnd

Section "Uninstall"
  ; 只删程序，不删工作区用户数据（WORKSPACE_ROOT 独立）
  Delete "$DESKTOP\${APPNAME}.lnk"
  RMDir /r "$SMPROGRAMS\${APPNAME}"
  Delete "$INSTDIR\launcher\LiveStreamClips.exe"
  Delete "$INSTDIR\launcher\orchestrator.py"
  RMDir /r "$INSTDIR\launcher\_internal"
  RMDir /r "$INSTDIR\launcher\.runtime"
  RMDir /r "$INSTDIR\app"
  Delete "$INSTDIR\Uninstall LiveStreamClips.exe"
  RMDir "$INSTDIR\launcher"
  RMDir "$INSTDIR"
  DeleteRegKey HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APPID}"
  DeleteRegKey HKCU "Software\${APPID}"
SectionEnd

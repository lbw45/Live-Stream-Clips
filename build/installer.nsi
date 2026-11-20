; Live Stream Clips - Windows installer (NSIS 3.x, Unicode)
; Build:  makensis build\installer.nsi      (after running build\make_installer.py)
; Output: LiveStreamClips-Setup-<version>.exe
;
; Contents: app sources + launcher + EMBEDDED portable runtime (python + all deps).
; FFmpeg is an optional download section (with progress). The uninstaller lets the
; user pick what to remove: program / runtime / models / user data (default keep).

Unicode true
!include "MUI2.nsh"
!include "LogicLib.nsh"
!include "x64.nsh"

!ifndef VERSION
  !define VERSION "1.0.1"
!endif
!define APPNAME "Live Stream Clips"
!define APPID "LiveStreamClips"
!define COMPANY "LiveStreamClips"
!define UNINST_EXE "Uninstall ${APPNAME}.exe"

Name "${APPNAME}"
OutFile "LiveStreamClips-Setup-${VERSION}.exe"
InstallDir "$LOCALAPPDATA\${APPID}"
InstallDirRegKey HKCU "Software\${APPID}" "InstallDir"
RequestExecutionLevel user
SetCompressor /SOLID lzma
SetCompressorDictSize 64

!define MUI_ABORTWARNING
!define MUI_ICON   "staging\launcher\ui\assets\app.ico"
!define MUI_UNICON "staging\launcher\ui\assets\app.ico"
Icon    "staging\launcher\ui\assets\app.ico"
UninstallIcon "staging\launcher\ui\assets\app.ico"
!define MUI_FINISHPAGE_RUN "$INSTDIR\launcher\.runtime\start.bat"
!define MUI_FINISHPAGE_RUN_TEXT "Start ${APPNAME} now"
!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_COMPONENTS
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH
!insertmacro MUI_UNPAGE_WELCOME
!insertmacro MUI_UNPAGE_COMPONENTS
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_LANGUAGE "SimpChinese"

; ============================================================
; Install sections
; ============================================================

; ---- [1] Program files (always installed) ----
Section "程序文件（必需）" SEC_APP
  SectionIn RO
  SetOutPath "$INSTDIR\app"
  File /r "staging\app\*.*"
  SetOutPath "$INSTDIR\launcher"
  File /r "staging\launcher\*.*"
  SetOutPath "$INSTDIR"
  File "staging\README.md"
  File "staging\LICENSE"
  File "staging\THIRD_PARTY_NOTICES.md"
  File "staging\requirements.txt"
  File "staging\.gitignore"

  ; workspace skeleton (user data lives here, never overwritten on upgrade)
  SetOutPath "$INSTDIR\workspace"
  CreateDirectory "$INSTDIR\workspace\bin"
  CreateDirectory "$INSTDIR\workspace\models"
  CreateDirectory "$INSTDIR\workspace\live_raw"
  CreateDirectory "$INSTDIR\workspace\music"
  CreateDirectory "$INSTDIR\workspace\uploads"
  CreateDirectory "$INSTDIR\workspace\records"
  CreateDirectory "$INSTDIR\workspace\runtime_data"
SectionEnd

; ---- [2] Runtime environment (skipped when present & healthy) ----
Section "运行环境（便携 Python + 全套依赖）" SEC_RUNTIME
  ${If} ${FileExists} "$INSTDIR\workspace\runtime\python\python.exe"
    DetailPrint "检测到已有运行环境，正在校验依赖 …"
    nsExec::ExecToStack '"$INSTDIR\workspace\runtime\python\python.exe" -c "import webview, fastapi, uvicorn, httpx, PIL, numpy, playwright, faster_whisper, pyJianYingDraft"'
    Pop $0
    ${If} $0 == 0
      DetailPrint "依赖校验通过 —— 跳过环境安装（不重复下载）。"
      GoTo runtime_done
    ${Else}
      DetailPrint "已有环境校验失败（退出码 $0），将重新部署运行环境覆盖 …"
    ${EndIf}
  ${EndIf}
  DetailPrint "解压便携 Python 运行环境（约 1.2 GB，需要一到几分钟）…"
  SetOutPath "$INSTDIR\workspace\runtime\python"
  File /r "staging\runtime_env\*.*"
  DetailPrint "运行环境部署完成。"
runtime_done:
SectionEnd

; ---- [3] ffmpeg (optional download, ~90MB with progress) ----
Section "FFmpeg（可选：一键下载，渲染与转写必需，约 90MB）" SEC_FFMPEG
  ${If} ${FileExists} "$INSTDIR\workspace\bin\ffmpeg.exe"
    DetailPrint "已有 ffmpeg.exe，跳过下载。"
    GoTo ff_done
  ${EndIf}
  DetailPrint "下载 FFmpeg（gyan.dev release essentials，约 90MB）…"
  CreateDirectory "$TEMP\lsc_ff"
  NSISdl::download /TIMEOUT=60000 "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip" "$TEMP\lsc_ff\ffmpeg.zip"
  Pop $0
  ${If} $0 == "success"
    DetailPrint "下载完成，解压中 …"
    nsExec::ExecToLog 'tar -xf "$TEMP\lsc_ff\ffmpeg.zip" -C "$TEMP\lsc_ff"'
    CopyFiles /SILENT "$TEMP\lsc_ff\ffmpeg-*\bin\ffmpeg.exe" "$INSTDIR\workspace\bin\"
    CopyFiles /SILENT "$TEMP\lsc_ff\ffmpeg-*\bin\ffprobe.exe" "$INSTDIR\workspace\bin\"
    ${If} ${FileExists} "$INSTDIR\workspace\bin\ffmpeg.exe"
      DetailPrint "FFmpeg 已就绪。"
    ${Else}
      DetailPrint "FFmpeg 解压失败 —— 请手动把 ffmpeg.exe / ffprobe.exe 放进 workspace\bin。"
    ${EndIf}
  ${Else}
    DetailPrint "FFmpeg 下载失败（$0）—— 可稍后手动下载放进 workspace\bin。"
  ${EndIf}
  Delete "$TEMP\lsc_ff\ffmpeg.zip"
ff_done:
SectionEnd

; ---- [4] Desktop / start-menu shortcuts ----
Section "桌面与开始菜单快捷方式" SEC_SHORTCUT
  CreateShortCut "$DESKTOP\${APPNAME}.lnk" "$INSTDIR\launcher\.runtime\start.bat" "" "$INSTDIR\launcher\ui\assets\app.ico"
  CreateDirectory "$SMPROGRAMS\${APPNAME}"
  CreateShortCut "$SMPROGRAMS\${APPNAME}\${APPNAME}.lnk" "$INSTDIR\launcher\.runtime\start.bat" "" "$INSTDIR\launcher\ui\assets\app.ico"
SectionEnd

Section -Post
  WriteRegStr HKCU "Software\${APPID}" "InstallDir" "$INSTDIR"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APPID}" "DisplayName" "${APPNAME}"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APPID}" "DisplayVersion" "${VERSION}"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APPID}" "Publisher" "${COMPANY}"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APPID}" "UninstallString" '"$INSTDIR\${UNINST_EXE}"'
  WriteRegDWORD HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APPID}" "EstimatedSize" 1280
  WriteUninstaller "$INSTDIR\${UNINST_EXE}"
SectionEnd

; ============================================================
; Uninstall (component-selectable; user data defaults OFF)
; ============================================================

Section "un.停止后台服务" un.stop
  SectionIn RO
  DetailPrint "停止后台服务 …"
  ${If} ${FileExists} "$INSTDIR\workspace\runtime\python\python.exe"
    nsExec::ExecToLog '"$INSTDIR\workspace\runtime\python\python.exe" "$INSTDIR\launcher\orchestrator.py" stop'
    Pop $0
  ${EndIf}
  Sleep 1500
SectionEnd

Section "un.程序文件" un.app
  SectionIn RO
  Delete "$DESKTOP\${APPNAME}.lnk"
  RMDir /r "$SMPROGRAMS\${APPNAME}"
  RMDir /r "$INSTDIR\app"
  RMDir /r "$INSTDIR\launcher"
  Delete "$INSTDIR\README.md"
  Delete "$INSTDIR\LICENSE"
  Delete "$INSTDIR\THIRD_PARTY_NOTICES.md"
  Delete "$INSTDIR\requirements.txt"
  Delete "$INSTDIR\.gitignore"
  DeleteRegKey HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APPID}"
  DeleteRegKey HKCU "Software\${APPID}"
SectionEnd

Section "un.运行环境（便携 Python + ffmpeg）" un.runtime
  RMDir /r "$INSTDIR\workspace\runtime"
  RMDir /r "$INSTDIR\workspace\bin"
SectionEnd

Section "un.模型文件（Whisper）" un.model
  RMDir /r "$INSTDIR\workspace\models"
SectionEnd

Section /o "un.我的使用数据（录制、成片、素材、配置 —— 默认保留！）" un.userdata
  RMDir /r "$INSTDIR\workspace\live_raw"
  RMDir /r "$INSTDIR\workspace\clips"
  RMDir /r "$INSTDIR\workspace\edits"
  RMDir /r "$INSTDIR\workspace\exports"
  RMDir /r "$INSTDIR\workspace\music"
  RMDir /r "$INSTDIR\workspace\uploads"
  RMDir /r "$INSTDIR\workspace\records"
  RMDir /r "$INSTDIR\workspace\board"
  RMDir /r "$INSTDIR\workspace\runtime_data"
  Delete "$INSTDIR\workspace\settings.json"
SectionEnd

Section "un.卸载程序自身" un.self
  SectionIn RO
  Delete "$INSTDIR\${UNINST_EXE}"
  RMDir "$INSTDIR\workspace"
  RMDir "$INSTDIR"
SectionEnd

Function .onInit
  ${IfNot} ${RunningX64}
    MessageBox MB_ICONSTOP "${APPNAME} 需要 64 位 Windows。"
    Abort
  ${EndIf}
FunctionEnd

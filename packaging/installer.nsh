; ============================================================
;  PuMail 自定义 NSIS 安装脚本 (installer.nsh)
;  功能：自定义选项页面（开始菜单 / 安装后删除安装包）
;  桌面快捷方式始终创建，不显示选项
; ============================================================

!include nsDialogs.nsh
!include LogicLib.nsh

; -- 品牌 --
BrandingText "PuMail"

; -- 浅色主题进度条 --
InstallColors 0078D7 4DA3FF
InstProgressFlags smooth

; -- 仅在安装器中定义（不影响卸载器）--
!ifndef BUILD_UNINSTALLER

  Var keepStartMenuShortcut
  Var keepInstaller
  Var _tmp_state
  Var hwndStartMenu
  Var hwndInstaller

  ; -- 自定义选项页面 --
  !macro customPageAfterChangeDir
    Page custom InstallerOptionsPage InstallerOptionsPageLeave
  !macroend

  Function InstallerOptionsPage
    nsDialogs::Create 1044
    Pop $0

    ${NSD_CreateLabel} 0 0u 100% 14u "请选择安装选项："
    Pop $0

    StrCpy $keepStartMenuShortcut ${BST_CHECKED}
    StrCpy $keepInstaller ${BST_CHECKED}

    ${NSD_CreateCheckbox} 10u 20u 90% 12u "固定到开始菜单"
    Pop $hwndStartMenu
    SendMessage $hwndStartMenu ${BM_SETCHECK} ${BST_CHECKED} 0

    ${NSD_CreateCheckbox} 10u 36u 90% 12u "安装完成后保留安装包"
    Pop $hwndInstaller
    SendMessage $hwndInstaller ${BM_SETCHECK} ${BST_CHECKED} 0

    nsDialogs::Show
  FunctionEnd

  Function InstallerOptionsPageLeave
    ${NSD_GetState} $hwndStartMenu $_tmp_state
    StrCpy $keepStartMenuShortcut $_tmp_state
    ${NSD_GetState} $hwndInstaller $_tmp_state
    StrCpy $keepInstaller $_tmp_state
  FunctionEnd

  !macro customInstall
    ; 桌面快捷方式显式使用同一份猫图标，避免回退到 Electron 默认图标。
    CreateShortcut "$DESKTOP\PuMail.lnk" "$INSTDIR\${APP_EXECUTABLE_FILENAME}" "" "$INSTDIR\resources\icon.ico" 0

    ${if} $keepStartMenuShortcut == ${BST_CHECKED}
      CreateDirectory "$SMPROGRAMS\PuMail"
      CreateShortcut "$SMPROGRAMS\PuMail\PuMail.lnk" "$INSTDIR\${APP_EXECUTABLE_FILENAME}" "" "$INSTDIR\resources\icon.ico" 0
      WriteINIStr "$SMPROGRAMS\PuMail\卸载 PuMail.url" "InternetShortcut" "URL" "$INSTDIR\uninstall.exe"
    ${endIf}

    ${if} $keepInstaller != ${BST_CHECKED}
      ; 安装器进程退出前无法删除自身，使用短路径并持续重试。
      GetFullPathName /SHORT $1 "$EXEPATH"
      Delete "$TEMP\pumail_cleanup.bat"
      FileOpen $0 "$TEMP\pumail_cleanup.bat" w
      FileWrite $0 "@echo off"
      FileWriteByte $0 13
      FileWriteByte $0 10
      FileWrite $0 ":retry"
      FileWriteByte $0 13
      FileWriteByte $0 10
      FileWrite $0 "del /f /q $1 >nul 2>&1"
      FileWriteByte $0 13
      FileWriteByte $0 10
      FileWrite $0 "if exist $1 (timeout /t 1 /nobreak >nul & goto retry)"
      FileWriteByte $0 13
      FileWriteByte $0 10
      FileWrite $0 "del /f /q %~f0 >nul 2>&1"
      FileWriteByte $0 13
      FileWriteByte $0 10
      FileWrite $0 "exit /b"
      FileWriteByte $0 13
      FileWriteByte $0 10
      FileClose $0
      ; 通过 cmd 启动，避免直接 Exec .bat 在不同 Windows 关联设置下失效。
      Exec '"$SYSDIR\cmd.exe" /C ""$TEMP\pumail_cleanup.bat""'
    ${endIf}
  !macroend

!endif

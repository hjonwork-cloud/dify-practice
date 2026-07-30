@echo off
chcp 65001 > nul
echo.
echo ============================================
echo   동원홈푸드 SAP Bridge Agent 설치
echo ============================================
echo.

:: 현재 폴더에서 exe 찾기
set "EXE=%~dp0SAPBridgeAgent.exe"
set "INSTALL_DIR=%USERPROFILE%\DongwonSAP"

if not exist "%EXE%" (
    echo [오류] SAPBridgeAgent.exe 파일이 없습니다.
    echo        이 setup.bat 과 같은 폴더에 SAPBridgeAgent.exe 를 두세요.
    pause
    exit /b 1
)

:: 설치 폴더 생성
echo [1/3] 설치 폴더 생성: %INSTALL_DIR%
if not exist "%INSTALL_DIR%" mkdir "%INSTALL_DIR%"

:: exe 복사
echo [2/3] 파일 복사 중...
copy /Y "%EXE%" "%INSTALL_DIR%\SAPBridgeAgent.exe" > nul
echo       완료: %INSTALL_DIR%\SAPBridgeAgent.exe

:: 시작프로그램 단축아이콘 생성 (PowerShell 이용)
echo [3/3] 시작프로그램 등록 (PC 켤 때 자동 실행)...
powershell -NoProfile -Command ^
  "$s=[Environment]::GetFolderPath('Startup');" ^
  "$lnk=$s+'\SAP Bridge Agent.lnk';" ^
  "$sh=New-Object -ComObject WScript.Shell;" ^
  "$sc=$sh.CreateShortcut($lnk);" ^
  "$sc.TargetPath='%INSTALL_DIR%\SAPBridgeAgent.exe';" ^
  "$sc.WorkingDirectory='%INSTALL_DIR%';" ^
  "$sc.WindowStyle=7;" ^
  "$sc.Description='Dongwon HomeFoods SAP Bridge Agent';" ^
  "$sc.Save();" ^
  "Write-Host '단축아이콘 등록 완료'"

echo.
echo ============================================
echo   설치 완료!
echo ============================================
echo.
echo   - 지금 바로 에이전트를 실행합니다...
echo   - 처음 실행 시 인증서를 자동 등록합니다 (몇 초 소요)
echo   - 다음부터 PC 켜면 자동으로 실행됩니다
echo.
start "" "%INSTALL_DIR%\SAPBridgeAgent.exe"
timeout /t 3 > nul

echo   에이전트가 백그라운드에서 실행 중입니다.
echo   포털에서 "판가 적용 후 DM 발송" 버튼을 사용할 수 있습니다.
echo.
pause

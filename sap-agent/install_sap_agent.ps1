# SAP 브릿지 에이전트 설치 스크립트
# 실행: PowerShell에서 .\install_sap_agent.ps1

$agentDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$agentScript = Join-Path $agentDir "sap_bridge_agent.py"
$pythonExe = "e:\git-copilot\SSU_aiplatform\.venv\Scripts\python.exe"

# python.exe 찾기 (venv 없으면 시스템 Python)
if (-not (Test-Path $pythonExe)) {
    $pythonExe = (Get-Command python -ErrorAction SilentlyContinue).Source
}
if (-not $pythonExe) {
    Write-Host "❌ Python을 찾을 수 없습니다. Python을 설치하세요." -ForegroundColor Red
    exit 1
}

Write-Host "=== SAP 브릿지 에이전트 설치 ===" -ForegroundColor Cyan
Write-Host "Python: $pythonExe"
Write-Host "에이전트: $agentScript"

# pywin32 설치 확인
Write-Host "`npywin32 설치 확인 중..." -ForegroundColor Yellow
& $pythonExe -c "import win32com.client; print('pywin32 OK')" 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Host "pywin32 설치 중..." -ForegroundColor Yellow
    & (Join-Path (Split-Path $pythonExe) "pip.exe") install pywin32
}

# cryptography 설치 확인 (HTTPS 인증서 생성에 필요)
Write-Host "`ncryptography 설치 확인 중..." -ForegroundColor Yellow
& $pythonExe -c "import cryptography; print('cryptography OK')" 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Host "cryptography 설치 중..." -ForegroundColor Yellow
    & (Join-Path (Split-Path $pythonExe) "pip.exe") install cryptography
}

# 자체 서명 인증서 생성 (없으면)
$certDir  = Join-Path $env:USERPROFILE ".dongwon_sap_bridge"
$certFile = Join-Path $certDir "cert.pem"
if (-not (Test-Path $certFile)) {
    Write-Host "`n인증서 생성 중..." -ForegroundColor Yellow
    & $pythonExe -c "
import os, sys
sys.path.insert(0, r'$agentDir')
from sap_bridge_agent import _ensure_cert
_ensure_cert()
print('인증서 생성 완료')
"
}

# Windows 신뢰 저장소에 인증서 등록 (관리자 권한 필요)
Write-Host "`nWindows 신뢰 저장소에 인증서 등록 중..." -ForegroundColor Yellow
try {
    $cert = New-Object System.Security.Cryptography.X509Certificates.X509Certificate2($certFile)
    $store = New-Object System.Security.Cryptography.X509Certificates.X509Store(
        [System.Security.Cryptography.X509Certificates.StoreName]::Root,
        [System.Security.Cryptography.X509Certificates.StoreLocation]::CurrentUser
    )
    $store.Open([System.Security.Cryptography.X509Certificates.OpenFlags]::ReadWrite)
    $store.Add($cert)
    $store.Close()
    Write-Host "✅ 인증서 신뢰 등록 완료 (CurrentUser\Root)" -ForegroundColor Green
    Write-Host "   Chrome/Edge 브라우저에서 https://localhost:7788 접속 시 경고 없음" -ForegroundColor Gray
} catch {
    Write-Host "⚠️  인증서 자동 등록 실패: $_" -ForegroundColor Yellow
    Write-Host "   수동 등록: certmgr.msc → 신뢰할 수 있는 루트 인증 기관 → 인증서 → 가져오기" -ForegroundColor Yellow
    Write-Host "   인증서 파일: $certFile" -ForegroundColor Yellow
}

# Windows 시작 프로그램 등록 (현재 사용자)
$startupFolder = [Environment]::GetFolderPath("Startup")
$shortcutPath = Join-Path $startupFolder "SAP 브릿지 에이전트.lnk"

$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($shortcutPath)
$shortcut.TargetPath = $pythonExe
$shortcut.Arguments = "`"$agentScript`""
$shortcut.WorkingDirectory = $agentDir
$shortcut.WindowStyle = 7  # 최소화 시작
$shortcut.Description = "SAP 브릿지 에이전트 - 동원홈푸드 영업 플랫폼"
$shortcut.Save()

Write-Host "`n✅ 시작 프로그램 등록 완료" -ForegroundColor Green
Write-Host "   위치: $shortcutPath"

# 지금 바로 실행
$ans = Read-Host "`n지금 바로 에이전트를 시작하시겠습니까? (y/n)"
if ($ans -eq "y") {
    Write-Host "에이전트 시작 중..." -ForegroundColor Yellow
    Start-Process $pythonExe -ArgumentList "`"$agentScript`"" -WindowStyle Minimized
    Start-Sleep 2
    
    # 연결 확인
    try {
        $resp = Invoke-WebRequest "https://localhost:7788/" -UseBasicParsing -TimeoutSec 3 -SkipCertificateCheck
        Write-Host "✅ 에이전트 실행 확인 (https://localhost:7788)" -ForegroundColor Green
    } catch {
        Write-Host "⚠️  에이전트 응답 없음 — 잠시 후 다시 확인하세요" -ForegroundColor Yellow
    }
}

Write-Host "`n설치 완료. PC 재시작 후 자동으로 실행됩니다." -ForegroundColor Cyan

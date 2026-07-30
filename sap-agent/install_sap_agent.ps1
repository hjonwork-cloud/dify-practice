# SAP Bridge Agent Install Script
# Run: PowerShell -ExecutionPolicy Bypass -File .\install_sap_agent.ps1

$agentDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$agentScript = Join-Path $agentDir "sap_bridge_agent.py"
$pythonExe = "e:\git-copilot\SSU_aiplatform\.venv\Scripts\python.exe"

if (-not (Test-Path $pythonExe)) {
    $pythonExe = (Get-Command python -ErrorAction SilentlyContinue).Source
}
if (-not $pythonExe) {
    Write-Host "ERROR: Python not found. Please install Python first." -ForegroundColor Red
    exit 1
}

Write-Host "=== SAP Bridge Agent Setup ===" -ForegroundColor Cyan
Write-Host "Python : $pythonExe"
Write-Host "Agent  : $agentScript"

# pywin32
Write-Host "`n[1/4] Checking pywin32..." -ForegroundColor Yellow
& $pythonExe -c "import win32com.client; print('pywin32 OK')" 2>&1
if ($LASTEXITCODE -ne 0) {
    & (Join-Path (Split-Path $pythonExe) "pip.exe") install pywin32
}

# cryptography
Write-Host "`n[2/4] Checking cryptography..." -ForegroundColor Yellow
& $pythonExe -c "import cryptography; print('cryptography OK')" 2>&1
if ($LASTEXITCODE -ne 0) {
    & (Join-Path (Split-Path $pythonExe) "pip.exe") install cryptography
}

# Generate cert
$certDir  = Join-Path $env:USERPROFILE ".dongwon_sap_bridge"
$certFile = Join-Path $certDir "cert.pem"
Write-Host "`n[3/4] Generating certificate..." -ForegroundColor Yellow
if (-not (Test-Path $certFile)) {
    $genCode = @"
import sys
sys.path.insert(0, r'$agentDir')
from sap_bridge_agent import _ensure_cert
_ensure_cert()
print('Certificate created')
"@
    & $pythonExe -c $genCode
} else {
    Write-Host "Certificate already exists: $certFile" -ForegroundColor Gray
}

# Trust cert in Windows store
Write-Host "`n[4/4] Registering certificate in Windows trust store..." -ForegroundColor Yellow
try {
    $cert = New-Object System.Security.Cryptography.X509Certificates.X509Certificate2($certFile)
    $store = New-Object System.Security.Cryptography.X509Certificates.X509Store(
        [System.Security.Cryptography.X509Certificates.StoreName]::Root,
        [System.Security.Cryptography.X509Certificates.StoreLocation]::CurrentUser
    )
    $store.Open([System.Security.Cryptography.X509Certificates.OpenFlags]::ReadWrite)
    $store.Add($cert)
    $store.Close()
    Write-Host "OK  Certificate trusted (CurrentUser\Root)" -ForegroundColor Green
    Write-Host "    Chrome/Edge will not warn on https://localhost:7788" -ForegroundColor Gray
} catch {
    Write-Host "WARN: Auto-registration failed: $_" -ForegroundColor Yellow
    Write-Host "      Manual: certmgr.msc -> Trusted Root CA -> Import: $certFile" -ForegroundColor Yellow
}

# Startup shortcut
$startupFolder = [Environment]::GetFolderPath("Startup")
$shortcutPath = Join-Path $startupFolder "SAP Bridge Agent.lnk"
$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($shortcutPath)
$shortcut.TargetPath = $pythonExe
$shortcut.Arguments = "`"$agentScript`""
$shortcut.WorkingDirectory = $agentDir
$shortcut.WindowStyle = 7
$shortcut.Description = "Dongwon HomeFoods SAP Bridge Agent"
$shortcut.Save()
Write-Host "`nOK  Startup shortcut registered: $shortcutPath" -ForegroundColor Green

# Start now?
$ans = Read-Host "`nStart the agent now? (y/n)"
if ($ans -eq "y") {
    Start-Process $pythonExe -ArgumentList "`"$agentScript`"" -WindowStyle Minimized
    Start-Sleep 3
    try {
        Invoke-WebRequest "https://localhost:7788/sap/status" -UseBasicParsing -TimeoutSec 5 -SkipCertificateCheck | Out-Null
        Write-Host "OK  Agent running at https://localhost:7788" -ForegroundColor Green
    } catch {
        Write-Host "WARN: Agent not responding yet - wait a moment and try again" -ForegroundColor Yellow
    }
}

Write-Host "`n[DONE] Installation complete." -ForegroundColor Cyan

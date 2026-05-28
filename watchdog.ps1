# ============================================================
# watchdog.ps1 - uvicorn server + ngrok tunnel watchdog
# ============================================================

$PYTHON     = "e:\git-copilot\.conda\python.exe"
$API_DIR    = "e:\git-copilot\dify-practice\api"
$PORT       = 8000
$NGROK_DOMAIN = "perfunctorily-stumpless-leticia.ngrok-free.dev"
$LOG_FILE   = "e:\git-copilot\dify-practice\watchdog.log"
$CHECK_SEC  = 30   # 점검 주기 (초)

function Write-Log {
    param([string]$msg)
    $ts = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    $line = "[$ts] $msg"
    Write-Host $line
    [System.IO.File]::AppendAllText($LOG_FILE, "$line`n", [System.Text.Encoding]::UTF8)
}

function Is-PortListening {
    return ($null -ne (netstat -ano | Select-String ":$PORT" | Select-String "LISTENING"))
}

function Is-NgrokAlive {
    # 포트 4040(ngrok web UI)이 열려있으면 살아있다고 판단
    $port4040 = netstat -ano | Select-String ":4040" | Select-String "LISTENING"
    if (-not $port4040) { return $false }
    # 포트가 열려있으면 살아있다고 간주 (터널 연결 중일 수 있음)
    return $true
}

function Start-Uvicorn {
    Write-Log "[INFO] Starting uvicorn..."
    $proc = Start-Process -PassThru -WindowStyle Hidden `
        -FilePath $PYTHON `
        -ArgumentList "-m uvicorn main:app --host 0.0.0.0 --port $PORT --reload" `
        -WorkingDirectory $API_DIR
    Start-Sleep -Seconds 5
    if (Is-PortListening) {
        Write-Log "[OK] uvicorn started (PID $($proc.Id))"
    } else {
        Write-Log "[FAIL] uvicorn failed to start"
    }
}

function Start-Ngrok {
    Write-Log "[INFO] Starting ngrok..."
    # 포트 4040이 이미 열려있으면 실행하지 않음
    $port4040 = netstat -ano | Select-String ":4040" | Select-String "LISTENING"
    if ($port4040) {
        Write-Log "[INFO] ngrok port 4040 already open, nothing to do"
        return
    }
    # 포트 4040 없음 → 클라우드 세션 해제 대기 후 시작
    Write-Log "[INFO] Waiting 60s for cloud session release..."
    Start-Sleep -Seconds 60
    Start-Process -WindowStyle Hidden `
        -FilePath "ngrok" `
        -ArgumentList "http $PORT --domain=$NGROK_DOMAIN"
    Start-Sleep -Seconds 5
    if (Is-NgrokAlive) {
        Write-Log "[OK] ngrok tunnel up (https://$NGROK_DOMAIN)"
    } else {
        Write-Log "[FAIL] ngrok failed to start (will retry next cycle)"
    }
}

Write-Log "======= watchdog START (interval: ${CHECK_SEC}sec) ======="

while ($true) {
    # uvicorn 확인
    if (-not (Is-PortListening)) {
        Write-Log "[WARN] uvicorn not found -> restarting..."
        Start-Uvicorn
    }

    # ngrok 확인
    if (-not (Is-NgrokAlive)) {
        Write-Log "[WARN] ngrok tunnel not found -> restarting..."
        Start-Ngrok
    }

    Start-Sleep -Seconds $CHECK_SEC
}

# Baemin crawl scheduler script
# - Skips if today's data already collected (1-run-per-day guarantee)
# - Sends mail on success

$PY       = "e:\git-copilot\SSU_aiplatform\.venv\Scripts\python.exe"
$BASE_DIR = "e:\git-copilot\dify-practice"
$SCRIPT   = "$BASE_DIR\api\crawl_platform_prices.py"
$LOG_DIR  = "$BASE_DIR\logs"
$TODAY    = (Get-Date).ToString("yyyy-MM-dd")
$LOG_FILE = "$LOG_DIR\baemin_" + (Get-Date).ToString("yyyyMMdd") + ".txt"

# Load env from .env.local
$ENV_FILE = "$BASE_DIR\.env.local"
if (Test-Path $ENV_FILE) {
    Get-Content $ENV_FILE | ForEach-Object {
        if ($_ -match '^\s*([^#][^=]+)=(.+)$') {
            [System.Environment]::SetEnvironmentVariable($matches[1].Trim(), $matches[2].Trim(), 'Process')
        }
    }
}
$env:PYTHONIOENCODING = "utf-8"

if (-not (Test-Path $LOG_DIR)) { New-Item -ItemType Directory -Path $LOG_DIR | Out-Null }

function Log($msg) {
    $line = "[" + (Get-Date).ToString("HH:mm:ss") + "] " + $msg
    Write-Host $line
    Add-Content -Path $LOG_FILE -Value $line -Encoding UTF8
}

# --- Check if today's baemin data already collected ---
Log "Checking baemin data for $TODAY ..."

$COUNT_STR = & $PY "$BASE_DIR\_baemin_check_today.py" $TODAY 2>$null
$BAEMIN_COUNT = 0
try { $BAEMIN_COUNT = [int](($COUNT_STR -split "`n" | Select-Object -Last 1).Trim()) } catch {}

if ($BAEMIN_COUNT -gt 0) {
    Log "Already collected $BAEMIN_COUNT records today - skipping"
    exit 0
}

Log "Starting baemin crawl..."

# --- Run crawl ---
$START = Get-Date
& $PY $SCRIPT --baemin 2>&1 | ForEach-Object {
    Write-Host $_
    Add-Content -Path $LOG_FILE -Value $_ -Encoding UTF8
}
$EXIT_CODE = $LASTEXITCODE
$ELAPSED   = [int]((Get-Date) - $START).TotalSeconds

if ($EXIT_CODE -eq 0) {
    Log "Crawl done in ${ELAPSED}s"
    $MAIL_OUT = & $PY "$BASE_DIR\_baemin_send_mail.py" $TODAY $LOG_FILE $ELAPSED 2>&1
    Log ("Mail: " + ($MAIL_OUT -join " "))
} else {
    Log "Crawl failed (exit=$EXIT_CODE) - will retry at next schedule"
}

exit 0

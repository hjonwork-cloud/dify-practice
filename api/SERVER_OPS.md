# 서버 운영 및 트러블슈팅 가이드

## 구성 요약

| 항목 | 내용 |
|---|---|
| Python 환경 | `e:\git-copilot\.conda\python.exe` |
| 서버 포트 | `8000` |
| ngrok 고정 도메인 | `https://perfunctorily-stumpless-leticia.ngrok-free.app` |
| 카카오 스킬 URL | `https://perfunctorily-stumpless-leticia.ngrok-free.app/kakao/skill` |
| 로그 파일 | `e:\git-copilot\dify-practice\api\uvicorn.log` |

---

## 1. 서버 시작

### uvicorn (API 서버)
```powershell
Set-Location "e:\git-copilot\dify-practice\api"
Start-Job -ScriptBlock {
    Set-Location "e:\git-copilot\dify-practice\api"
    & "e:\git-copilot\.conda\python.exe" -m uvicorn main:app --host 0.0.0.0 --port 8000 2>&1 |
    Out-File "e:\git-copilot\dify-practice\api\uvicorn.log"
} | Out-Null
```

### ngrok (터널)
```powershell
Start-Job -ScriptBlock {
    & "C:\Users\DW-RT\AppData\Local\Microsoft\WinGet\Links\ngrok.exe" http 8000
} | Out-Null
```

---

## 2. 상태 확인

### uvicorn 동작 확인
```powershell
Invoke-WebRequest -Uri "http://localhost:8000/docs" -UseBasicParsing -TimeoutSec 5 |
    Select-Object -ExpandProperty StatusCode
# 200 → 정상
```

### ngrok 터널 확인
```powershell
Invoke-WebRequest -Uri "http://localhost:4040/api/tunnels" -UseBasicParsing -TimeoutSec 5 |
    Select-Object -ExpandProperty Content | ConvertFrom-Json |
    Select-Object -ExpandProperty tunnels | Select-Object public_url
```

### 카카오 스킬 응답 테스트
```powershell
$body = '{"userRequest":{"utterance":"메뉴","user":{"id":"test"}},"contexts":[],"bot":{"id":"test"},"action":{"params":{},"clientExtra":{}}}'
$r = Invoke-WebRequest -Uri "http://localhost:8000/kakao/skill" -Method POST `
     -ContentType "application/json" -Body $body -UseBasicParsing -TimeoutSec 30
Write-Host "STATUS:" $r.StatusCode
$r.Content.Substring(0, [Math]::Min(500, $r.Content.Length))
```

### 실시간 로그 확인
```powershell
Get-Content "e:\git-copilot\dify-practice\api\uvicorn.log" -Tail 50
```

---

## 3. 트러블슈팅

### ❌ 챗봇이 응답하지 않는 경우

**1단계 — uvicorn 확인**
```powershell
Invoke-WebRequest -Uri "http://localhost:8000/docs" -UseBasicParsing -TimeoutSec 5 |
    Select-Object -ExpandProperty StatusCode
```
- `200` → 서버 정상, 다음 단계로
- 연결 실패 → uvicorn 재시작 (섹션 1 참조)

**2단계 — ngrok 확인**
```powershell
Invoke-WebRequest -Uri "http://localhost:4040/api/tunnels" -UseBasicParsing -TimeoutSec 5 |
    Select-Object -ExpandProperty Content
```
- 응답 있음 → 터널 active
- 연결 실패 → ngrok 재시작 (섹션 1 참조)

**3단계 — 로그에서 에러 확인**
```powershell
Get-Content "e:\git-copilot\dify-practice\api\uvicorn.log" -Tail 100 |
    Select-String "ERROR|Exception|Traceback"
```

---

### ❌ uvicorn 포트 충돌 (Address already in use)

```powershell
# 8000 포트 사용 중인 프로세스 확인
netstat -ano | findstr ":8000"

# PID로 프로세스 종료 (예: PID=12345)
Stop-Process -Id 12345 -Force

# 또는 python 프로세스 전체 종료
Get-Process -Name python -ErrorAction SilentlyContinue | Stop-Process -Force
```

---

### ❌ ngrok 세션 만료 / 터널 끊김

ngrok free 플랜은 장시간 실행 시 세션이 끊길 수 있음.

```powershell
# ngrok 프로세스 확인
Get-Process -Name ngrok -ErrorAction SilentlyContinue

# 종료 후 재시작
Get-Process -Name ngrok -ErrorAction SilentlyContinue | Stop-Process -Force
Start-Job -ScriptBlock {
    & "C:\Users\DW-RT\AppData\Local\Microsoft\WinGet\Links\ngrok.exe" http 8000
} | Out-Null
Start-Sleep -Seconds 5
Invoke-WebRequest -Uri "http://localhost:4040/api/tunnels" -UseBasicParsing -TimeoutSec 5 |
    Select-Object -ExpandProperty Content | ConvertFrom-Json |
    Select-Object -ExpandProperty tunnels | Select-Object public_url
```

---

### ❌ DB 연결 오류 (databricks / Spark 관련)

로그에서 `databricks`, `SparkConnectException`, `DEADLINE_EXCEEDED` 등 확인 시:

```powershell
# 로그에서 DB 관련 에러만 필터
Get-Content "e:\git-copilot\dify-practice\api\uvicorn.log" -Tail 200 |
    Select-String "databricks|Connection|timeout|Deadline"
```

- 일시적 네트워크 오류 → 잠시 후 재시도
- 지속 발생 → `config.py` 토큰/클러스터 정보 확인

---

### ❌ ImportError / ModuleNotFoundError

conda 환경 패키지 누락 가능성:

```powershell
& "e:\git-copilot\.conda\python.exe" -m pip install <패키지명>
```

---

## 4. 한 번에 전체 재시작 (원라이너)

```powershell
# 기존 프로세스 정리 → uvicorn → ngrok → 상태 확인
Get-Process -Name python,ngrok -ErrorAction SilentlyContinue | Stop-Process -Force
Start-Sleep -Seconds 1
Set-Location "e:\git-copilot\dify-practice\api"
Start-Job -ScriptBlock {
    Set-Location "e:\git-copilot\dify-practice\api"
    & "e:\git-copilot\.conda\python.exe" -m uvicorn main:app --host 0.0.0.0 --port 8000 2>&1 |
    Out-File "e:\git-copilot\dify-practice\api\uvicorn.log"
} | Out-Null
Start-Job -ScriptBlock {
    & "C:\Users\DW-RT\AppData\Local\Microsoft\WinGet\Links\ngrok.exe" http 8000
} | Out-Null
Start-Sleep -Seconds 6
Write-Host "[uvicorn]" (Invoke-WebRequest -Uri "http://localhost:8000/docs" -UseBasicParsing -TimeoutSec 5).StatusCode
$url = (Invoke-WebRequest -Uri "http://localhost:4040/api/tunnels" -UseBasicParsing -TimeoutSec 5 |
    Select-Object -ExpandProperty Content | ConvertFrom-Json).tunnels[0].public_url
Write-Host "[ngrok]" $url
```

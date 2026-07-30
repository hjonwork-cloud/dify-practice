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


---

## 8. Azure App Service 배포 (프로덕션)

### 8.1 시작 명령(Startup Command) — 필수 변경 (2026-07-30)

**[변경 이유]**
기존 명령은 `gunicorn -w 4 -k uvicorn.workers.UvicornWorker main:app` 였는데,
워커 4개가 뜨면서 다음 문제가 발생했다:

1. `_ConnectionPool` (main.py) 이 모듈 전역 싱글턴이라 워커 프로세스마다 독립.
   → warmup 이 특정 워커의 풀만 예열해서, 다른 워커로 라우팅된 요청은
      여전히 15~20초 콜드 스타트 발생 (`pick_s` 급증).
2. `_warmup_cache` / `_databricks_keepalive` / `_auto_refresh_scheduler` 가
   각 워커에서 4중 중복 실행 → refresh 동시 실행 시
   Delta `MetadataChangedException` (Conflicting commit) 발생.
3. 41명 규모, I/O bound(Databricks) 특성상 uvicorn 단일 워커의 async 처리로 충분.

**[Azure Portal 변경 방법]**
`App Service → 구성(Configuration) → 일반 설정(General settings) → 시작 명령(Startup Command)` 를
아래로 변경:

```bash
python startup.py && gunicorn -c gunicorn.conf.py main:app
```

이후 `App Service → 다시 시작(Restart)`.

**[gunicorn.conf.py 요약]**
- `workers = 1` (기본, `GUNICORN_WORKERS` 환경변수로 오버라이드 가능)
- `worker_class = uvicorn.workers.UvicornWorker`
- `timeout = 180` (Databricks 초기 쿼리 대비)
- `preload_app = False` (백그라운드 스레드가 워커 프로세스에서 살아야 함)

### 8.2 확인 방법

Azure 로그 스트림에서 아래가 **한 번만** 나오는지 확인:

```
[gunicorn.conf] workers=1 class=uvicorn.workers.UvicornWorker timeout=180 preload=False
Booting worker with pid: XXXX      ← 한 줄만
[warmup] 브랜드 리포트 사전계산 테이블 워밍업 완료   ← 한 번만
[singleton] keepalive: pid=XXXX 리더 획득
[singleton] refresh-scheduler: pid=XXXX 리더 획득
```

만약 `Booting worker with pid` 이 여러 줄 나오면 시작 명령이 아직 안 바뀐 것.

### 8.3 워커를 다시 늘려야 할 경우 (사용자 100명 이상 등)

env 로만 조정:

```
GUNICORN_WORKERS=2
```

이 경우에도 파일락(`/home/data/.portal_singleton_*.lock`) 으로
`keepalive`, `refresh-scheduler` 는 리더 워커 1개에서만 실행된다.
`warmup` 은 각 워커의 커넥션 풀 예열을 위해 모든 워커에서 실행.

### 8.4 관련 커밋

- gunicorn.conf.py 신규 (`workers=1` 기본)
- portal_router.py: `_acquire_singleton_lock` 도입
- portal_refresh.py: T_BRANDS SQL 의 `거래체` → `거래처` 오타 수정 (일반외식 매출/거래처수 정확화)


---

## 8. Azure App Service 배포 (프로덕션)

### 8.1 시작 명령(Startup Command) — 필수 변경 (2026-07-30)

**[변경 이유]**
기존 명령은 `gunicorn -w 4 -k uvicorn.workers.UvicornWorker main:app` 였는데,
워커 4개가 뜨면서 다음 문제가 발생했다:

1. `_ConnectionPool` (main.py) 이 모듈 전역 싱글턴이라 워커 프로세스마다 독립.
   → warmup 이 특정 워커의 풀만 예열해서, 다른 워커로 라우팅된 요청은
      여전히 15~20초 콜드 스타트 발생 (`pick_s` 급증).
2. `_warmup_cache` / `_databricks_keepalive` / `_auto_refresh_scheduler` 가
   각 워커에서 4중 중복 실행 → refresh 동시 실행 시
   Delta `MetadataChangedException` (Conflicting commit) 발생.
3. 41명 규모, I/O bound(Databricks) 특성상 uvicorn 단일 워커의 async 처리로 충분.

**[Azure Portal 변경 방법]**
`App Service → 구성(Configuration) → 일반 설정(General settings) → 시작 명령(Startup Command)` 를
아래로 변경:

```bash
python startup.py && gunicorn -c gunicorn.conf.py main:app
```

이후 `App Service → 다시 시작(Restart)`.

**[gunicorn.conf.py 요약]**
- `workers = 1` (기본, `GUNICORN_WORKERS` 환경변수로 오버라이드 가능)
- `worker_class = uvicorn.workers.UvicornWorker`
- `timeout = 180` (Databricks 초기 쿼리 대비)
- `preload_app = False` (백그라운드 스레드가 워커 프로세스에서 살아야 함)

### 8.2 확인 방법

Azure 로그 스트림에서 아래가 **한 번만** 나오는지 확인:

```
[gunicorn.conf] workers=1 class=uvicorn.workers.UvicornWorker timeout=180 preload=False
Booting worker with pid: XXXX      ← 한 줄만
[warmup] 브랜드 리포트 사전계산 테이블 워밍업 완료   ← 한 번만
[singleton] keepalive: pid=XXXX 리더 획득
[singleton] refresh-scheduler: pid=XXXX 리더 획득
```

만약 `Booting worker with pid` 이 여러 줄 나오면 시작 명령이 아직 안 바뀐 것.

### 8.3 워커를 다시 늘려야 할 경우 (사용자 100명 이상 등)

env 로만 조정:

```
GUNICORN_WORKERS=2
```

이 경우에도 파일락(`/home/data/.portal_singleton_*.lock`) 으로
`keepalive`, `refresh-scheduler` 는 리더 워커 1개에서만 실행된다.
`warmup` 은 각 워커의 커넥션 풀 예열을 위해 모든 워커에서 실행.

### 8.4 관련 커밋

- gunicorn.conf.py 신규 (`workers=1` 기본)
- portal_router.py: `_acquire_singleton_lock` 도입
- portal_refresh.py: T_BRANDS SQL 의 `거래체` → `거래처` 오타 수정 (일반외식 매출/거래처수 정확화)

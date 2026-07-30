"""
Gunicorn 설정 파일 — Azure App Service 배포용.

핵심 목적: **워커 프로세스 수를 1로 고정**한다.

배경(2026-07-30 이슈):
    기존 Azure 시작 명령이 `gunicorn -w 4 -k uvicorn.workers.UvicornWorker main:app`
    였는데, 이 경우 4개 워커 프로세스가 독립적으로 뜨면서 아래 문제가 발생함:

    1) `_ConnectionPool` (main.py) 이 모듈 전역 싱글턴 → 워커별로 4개의 독립된 풀.
       → warmup 스레드가 특정 워커에서만 커넥션을 예열하므로,
         다른 3개 워커로 요청이 라우팅되면 여전히 콜드 스타트(15~20s).
    2) `_warmup_cache` / `_databricks_keepalive` / `_auto_refresh_scheduler`
       threading 이 각 워커에서 중복 실행 → 로그 4배, refresh 4중 동시 실행,
       Delta MetadataChangedException(`Conflicting commit`) 발생.
    3) 사용자 규모(41명, I/O bound) 대비 4워커는 과도. 단일 워커 + async(uvicorn)
       로 충분.

해결: workers = 1 (환경변수 GUNICORN_WORKERS 로 오버라이드 가능).

주의: preload_app = False 유지.
    True 로 하면 master 프로세스에서 app import 가 일어나고,
    portal_router 하단의 `threading.Thread(...).start()` 들이 master 에서 실행되지만
    fork 시점에 스레드는 사라져서 실제 요청을 받는 워커에는 warmup/keepalive 가 없게 됨.
    workers=1 + preload_app=False 로 두면 그 유일한 워커 안에서 정상 동작.

Azure App Service 시작 명령(예):
    python startup.py && gunicorn -c gunicorn.conf.py main:app
"""

from __future__ import annotations

import multiprocessing
import os

# ── 워커 수 ──────────────────────────────────────────────────────────
# 기본 1 (콜드스타트/스케쥴러 중복 방지). 필요 시 env 로 늘릴 수 있으나
# 그럴 때는 refresh 스케쥴러 singleton 가드가 있어야 안전(portal_router 에서 처리).
workers = int(os.getenv("GUNICORN_WORKERS", "1"))

# uvicorn 워커(ASGI + async I/O)
worker_class = os.getenv("GUNICORN_WORKER_CLASS", "uvicorn.workers.UvicornWorker")

# ── 타임아웃/커넥션 ────────────────────────────────────────────────
# Databricks 초기 쿼리가 15~30s 걸릴 수 있어 여유 있게.
timeout = int(os.getenv("GUNICORN_TIMEOUT", "180"))
graceful_timeout = int(os.getenv("GUNICORN_GRACEFUL_TIMEOUT", "30"))
keepalive = int(os.getenv("GUNICORN_KEEPALIVE", "5"))

# preload 는 False 유지 (위 주석 참조)
preload_app = False

# ── 바인딩 ──────────────────────────────────────────────────────────
# Azure App Service 는 PORT env 를 주입한다.
bind = os.getenv("GUNICORN_BIND", f"0.0.0.0:{os.getenv('PORT', '8000')}")

# ── 로깅 ────────────────────────────────────────────────────────────
accesslog = "-"   # stdout
errorlog = "-"    # stderr
loglevel = os.getenv("GUNICORN_LOG_LEVEL", "info")

# ── 안정성 ─────────────────────────────────────────────────────────
# 워커 재시작 지터(같은 시각에 몰려서 재시작하는 것 방지).
max_requests = int(os.getenv("GUNICORN_MAX_REQUESTS", "0"))  # 0 = 무제한
max_requests_jitter = int(os.getenv("GUNICORN_MAX_REQUESTS_JITTER", "50"))


def when_ready(server):  # noqa: D401
    """Gunicorn master 가 워커를 fork 하기 직전 로그."""
    server.log.info(
        f"[gunicorn.conf] workers={workers} class={worker_class} "
        f"timeout={timeout} preload={preload_app}"
    )

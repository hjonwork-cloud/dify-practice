"""
플랫폼 가격 크롤러 스케줄러 (APScheduler)
- 매일 새벽 3:00 KST (Asia/Seoul) 실행
- Azure App Service 프로세스 내부에서 백그라운드 스레드로 구동
"""
from __future__ import annotations

import os
import subprocess
import sys
import logging
import threading
from pathlib import Path

logger = logging.getLogger(__name__)

# crawl_platform_prices.py 위치 (api/ 의 상위 폴더)
_CRAWL_SCRIPT = str(Path(__file__).parent.parent / "crawl_platform_prices.py")
_PYTHON       = sys.executable

_scheduler = None
_scheduler_lock = threading.Lock()


def _run_crawl():
    """크롤러를 별도 프로세스로 실행 (메인 앱 영향 없음)."""
    logger.info("[scheduler] 크롤러 시작")
    token = os.getenv("DATABRICKS_TOKEN", "")
    env = {**os.environ, "DATABRICKS_TOKEN": token, "PYTHONIOENCODING": "utf-8"}
    try:
        result = subprocess.run(
            [_PYTHON, _CRAWL_SCRIPT],
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=7200,  # 최대 2시간
        )
        if result.returncode == 0:
            # 마지막 5줄만 로그
            lines = [l for l in result.stdout.splitlines() if l.strip()]
            tail = "\n".join(lines[-5:]) if lines else "(출력 없음)"
            logger.info(f"[scheduler] 크롤러 완료\n{tail}")
        else:
            logger.error(f"[scheduler] 크롤러 실패 (returncode={result.returncode})\n{result.stderr[-500:]}")
    except subprocess.TimeoutExpired:
        logger.error("[scheduler] 크롤러 타임아웃 (2시간 초과)")
    except Exception as e:
        logger.exception(f"[scheduler] 크롤러 실행 오류: {e}")


def start():
    """스케줄러 시작 (앱 startup 시 호출). 이미 실행 중이면 무시."""
    global _scheduler
    with _scheduler_lock:
        if _scheduler is not None:
            return

        try:
            from apscheduler.schedulers.background import BackgroundScheduler
            from apscheduler.triggers.cron import CronTrigger
        except ImportError:
            logger.warning("[scheduler] apscheduler 미설치 — 자동 크롤링 비활성화 (pip install apscheduler)")
            return

        _scheduler = BackgroundScheduler(timezone="Asia/Seoul")
        _scheduler.add_job(
            _run_crawl,
            trigger=CronTrigger(hour=3, minute=0, timezone="Asia/Seoul"),
            id="daily_crawl",
            name="플랫폼 가격 일일 크롤링 (03:00 KST)",
            replace_existing=True,
            misfire_grace_time=3600,  # 1시간 내 지연 허용
        )
        _scheduler.start()

        next_run = _scheduler.get_job("daily_crawl").next_run_time
        logger.info(f"[scheduler] 시작됨 — 다음 실행: {next_run} (KST)")


def stop():
    """스케줄러 종료 (앱 shutdown 시 호출)."""
    global _scheduler
    with _scheduler_lock:
        if _scheduler is not None:
            _scheduler.shutdown(wait=False)
            _scheduler = None
            logger.info("[scheduler] 종료됨")


def status() -> dict:
    """현재 스케줄러 상태 반환."""
    if _scheduler is None:
        return {"running": False}
    job = _scheduler.get_job("daily_crawl")
    return {
        "running": True,
        "next_run": str(job.next_run_time) if job else None,
        "script": _CRAWL_SCRIPT,
    }

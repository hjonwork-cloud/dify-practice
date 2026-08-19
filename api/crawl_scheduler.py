"""
플랫폼 가격 크롤러 스케줄러 (APScheduler)
- 매일 새벽 3:00 KST (Asia/Seoul) 실행
- Azure App Service 프로세스 내부에서 백그라운드 스레드로 구동
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import logging
import threading
import time
from datetime import date, datetime, timezone, timedelta
from pathlib import Path

_KST = timezone(timedelta(hours=9))

logger = logging.getLogger(__name__)

# crawl_platform_prices.py 위치 탐색
# Azure App Service: /home/site/wwwroot/crawl_platform_prices.py
# 로컬: crawl_scheduler.py 기준 상위 폴더
def _find_crawl_script() -> str:
    candidates = [
        Path(__file__).parent / "crawl_platform_prices.py",           # api/ 안에 복사본 (Azure/로컬 공통)
        Path("/home/site/wwwroot/crawl_platform_prices.py"),          # Azure App Service 루트
        Path(__file__).parent.parent / "crawl_platform_prices.py",    # 로컬 (api/../)
        Path(os.getcwd()) / "crawl_platform_prices.py",               # cwd 기준
    ]
    for p in candidates:
        if p.exists():
            return str(p)
    return str(candidates[0])

_CRAWL_SCRIPT = _find_crawl_script()
_PYTHON       = sys.executable

_scheduler = None
_scheduler_lock = threading.Lock()


def _parse_crawl_output(stdout: str, duration_sec: float) -> dict:
    """크롤러 stdout에서 통계 파싱."""
    today = datetime.now(_KST).date().isoformat()
    total = 0
    baemin_count = 0
    food_count = 0
    seller_summary = []
    failed_sellers = []

    for line in stdout.splitlines():
        # "  ✓ 배민 1234: 500건 저장 (누적 500건)"
        m = re.search(r'✓ 배민 (\S+): (\d+)건', line)
        if m:
            cnt = int(m.group(2))
            baemin_count += cnt
            seller_summary.append({"platform": "baemin", "seller_id": m.group(1),
                                   "seller_name": m.group(1), "count": cnt})
        # "  ✓ 식봄 1388: 12240건 저장 (누적 ...)"
        m = re.search(r'✓ 식봄 (\S+): (\d+)건', line)
        if m:
            cnt = int(m.group(2))
            food_count += cnt
            seller_summary.append({"platform": "foodspring", "seller_id": m.group(1),
                                   "seller_name": m.group(1), "count": cnt})
        # "  ✗ 배민/식봄 xxx 저장 실패"
        if '✗' in line and '저장 실패' in line:
            failed_sellers.append(line.strip().lstrip('✗').strip())
        # "✓ 완료! 총 53,240건 저장"
        m = re.search(r'총 ([\d,]+)건 저장', line)
        if m:
            total = int(m.group(1).replace(',', ''))

    if total == 0:
        total = baemin_count + food_count

    # 셀러 요약 수집건수 내림차순 정렬
    seller_summary.sort(key=lambda x: x["count"], reverse=True)

    return {
        "crawl_date":      today,
        "total_saved":     total,
        "baemin_count":    baemin_count,
        "food_count":      food_count,
        "seller_summary":  seller_summary,
        "failed_sellers":  failed_sellers,
        "duration_sec":    round(duration_sec, 1),
    }


def _run_crawl():
    """크롤러를 별도 프로세스로 실행 후 리포트 메일 발송."""
    logger.info("[scheduler] 크롤러 시작")
    token = os.getenv("DATABRICKS_TOKEN", "")
    env   = {**os.environ, "DATABRICKS_TOKEN": token, "PYTHONIOENCODING": "utf-8"}
    t0    = time.time()
    try:
        result = subprocess.run(
            [_PYTHON, _CRAWL_SCRIPT],
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=7200,
        )
        duration = time.time() - t0

        if result.returncode == 0:
            lines = [l for l in result.stdout.splitlines() if l.strip()]
            tail  = "\n".join(lines[-5:]) if lines else "(출력 없음)"
            logger.info(f"[scheduler] 크롤러 완료 ({duration:.0f}s)\n{tail}")

            # 리포트 파싱 & 메일 발송
            try:
                from crawl_mailer import send_report
                report = _parse_crawl_output(result.stdout, duration)
                send_report(report)
            except Exception as e:
                logger.warning(f"[scheduler] 메일 발송 실패: {e}")
        else:
            stderr_tail = result.stderr[-1000:] if result.stderr else "(stderr 없음)"
            logger.error(f"[scheduler] 크롤러 실패 (code={result.returncode})\n{stderr_tail}")
            # 실패 시에도 메일 발송 시도
            try:
                from crawl_mailer import send_report
                report = _parse_crawl_output(result.stdout or "", duration)
                report["failed_sellers"].append(f"크롤러 비정상 종료 (code={result.returncode})")
                report["stderr"] = stderr_tail  # 메일에 포함용
                send_report(report)
            except Exception:
                pass

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

        logger.info(f"[scheduler] crawl script 경로: {_CRAWL_SCRIPT} (존재여부: {Path(_CRAWL_SCRIPT).exists()})")

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

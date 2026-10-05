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


def _parse_crawl_output(stdout: str, duration_sec: float, suffix: str = "") -> dict:
    """크롤러 stdout에서 통계 파싱 — summary JSON 우선, stdout fallback.
    suffix: 플랫폼별로 분리 저장된 summary 파일 구분자 ("", "_food", "_todaysales", "_baemin").
    """
    today = datetime.now(_KST).date().isoformat()

    # summary JSON 우선 읽기 (crawl_platform_prices.py가 저장)
    log_dir = Path(__file__).parent.parent / "logs"
    summary_path = log_dir / f"summary{suffix}_{today}.json"
    if summary_path.exists():
        try:
            import json as _json
            with open(summary_path, encoding="utf-8") as f:
                data = _json.load(f)
            data["duration_sec"] = round(duration_sec, 1)
            data.setdefault("stderr", "")
            return data
        except Exception as e:
            logger.warning(f"[scheduler] summary JSON 읽기 실패, stdout fallback: {e}")

    # fallback: stdout 파싱
    total = 0
    baemin_count = 0
    food_count = 0
    todaysales_count = 0
    seller_summary = []
    failed_sellers = []

    for line in stdout.splitlines():
        # "  ✓ 배민 셀러명(id): 500건 저장" or "  ✓ 배민 id: 500건 저장"
        m = re.search(r'✓ 배민 (.+?)\(?(\w+)\)?:\s*(\d+)건', line)
        if not m:
            m = re.search(r'✓ 배민 (\S+):\s*(\d+)건', line)
            if m:
                cnt = int(m.group(2))
                baemin_count += cnt
                seller_summary.append({"platform": "baemin", "seller_id": m.group(1),
                                       "seller_name": m.group(1), "count": cnt})
        else:
            name, sid, cnt = m.group(1).strip(), m.group(2), int(m.group(3))
            baemin_count += cnt
            seller_summary.append({"platform": "baemin", "seller_id": sid,
                                   "seller_name": name, "count": cnt})

        # "  ✓ 식봄 셀러명(id): 500건 저장" or "  ✓ 식봄 id: 500건 저장"
        m = re.search(r'✓ 식봄 (.+?)\(?(\w+)\)?:\s*(\d+)건', line)
        if not m:
            m = re.search(r'✓ 식봄 (\S+):\s*(\d+)건', line)
            if m:
                cnt = int(m.group(2))
                food_count += cnt
                seller_summary.append({"platform": "foodspring", "seller_id": m.group(1),
                                       "seller_name": m.group(1), "count": cnt})
        else:
            name, sid, cnt = m.group(1).strip(), m.group(2), int(m.group(3))
            food_count += cnt
            seller_summary.append({"platform": "foodspring", "seller_id": sid,
                                   "seller_name": name, "count": cnt})

        # "  ✓ 오늘얼마 셀러명(id): 500건 저장" or "  ✓ 오늘얼마 id: 500건 저장"
        m = re.search(r'✓ 오늘얼마 (.+?)\(?(\w+)\)?:\s*(\d+)건', line)
        if not m:
            m = re.search(r'✓ 오늘얼마 (\S+):\s*(\d+)건', line)
            if m:
                cnt = int(m.group(2))
                todaysales_count += cnt
                seller_summary.append({"platform": "todaysales", "seller_id": m.group(1),
                                       "seller_name": m.group(1), "count": cnt})
        else:
            name, sid, cnt = m.group(1).strip(), m.group(2), int(m.group(3))
            todaysales_count += cnt
            seller_summary.append({"platform": "todaysales", "seller_id": sid,
                                   "seller_name": name, "count": cnt})

        if '✗' in line and '저장 실패' in line:
            failed_sellers.append(line.strip().lstrip('✗').strip())
        m = re.search(r'총 ([\d,]+)건 저장', line)
        if m:
            total = int(m.group(1).replace(',', ''))

    if total == 0:
        total = baemin_count + food_count + todaysales_count

    seller_summary.sort(key=lambda x: x["count"], reverse=True)
    return {
        "crawl_date":     today,
        "total_saved":    total,
        "baemin_count":   baemin_count,
        "food_count":     food_count,
        "todaysales_count": todaysales_count,
        "seller_summary": seller_summary,
        "failed_sellers": failed_sellers,
        "duration_sec":   round(duration_sec, 1),
    }


def _run_single_platform(flag: str, platform_key: str, suffix: str, timeout_sec: int):
    """플랫폼 1개를 독립 프로세스로 실행하고, 성공/실패/타임아웃 어떤 경우든
    반드시 해당 플랫폼 완료(또는 실패) 리포트 메일을 발송한다.

    ── 배경 ──────────────────────────────────────────────────────────────
    과거에는 "--food --todaysales"를 하나의 서브프로세스로 묶어 실행했기
    때문에:
      1) 오늘얼마 쪽에서 멈추거나 전체 소요시간이 길어지면(타임아웃) 식봄이
         이미 정상적으로 다 수집되고 저장까지 끝났어도 메일이 "둘 다" 아예
         발송되지 않았다 (타임아웃 처리 분기에서 메일 발송 코드 자체가
         호출되지 않았음).
      2) 타임아웃이 아니더라도 메일 발송 단계에서 예외가 나면
         `except Exception: pass`로 완전히 침묵 처리되어 원인 추적이
         불가능했다.
    이 함수는 플랫폼별로 완전히 독립된 프로세스/타임아웃/메일 발송을 수행해
    한쪽이 멈추거나 실패해도 다른 쪽 결과·메일에 전혀 영향이 없도록 한다.
    """
    logger.info(f"[scheduler] {platform_key} 크롤러 시작 (timeout={timeout_sec}s)")
    token = os.getenv("DATABRICKS_TOKEN", "")
    env = {**os.environ, "DATABRICKS_TOKEN": token,
           "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}
    t0 = time.time()

    mail_fn = None
    try:
        from crawl_mailer import send_foodspring_report, send_todaysales_report, send_baemin_report
        mail_fn = {"foodspring": send_foodspring_report,
                   "todaysales": send_todaysales_report,
                   "baemin":     send_baemin_report}.get(platform_key)
    except Exception as e:
        logger.warning(f"[scheduler] crawl_mailer import 실패 — {platform_key} 메일 발송 불가: {e}")

    def _send(report: dict):
        if mail_fn is None:
            logger.warning(f"[scheduler] {platform_key} 메일 미발송(발송 함수 없음)")
            return
        try:
            mail_fn(report)
        except Exception as e:
            logger.error(f"[scheduler] {platform_key} 메일 발송 중 예외: {e}")

    try:
        result = subprocess.run(
            [_PYTHON, _CRAWL_SCRIPT, flag],
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=timeout_sec,
        )
        duration = time.time() - t0

        if result.returncode == 0:
            lines = [l for l in result.stdout.splitlines() if l.strip()]
            tail  = "\n".join(lines[-5:]) if lines else "(출력 없음)"
            logger.info(f"[scheduler] {platform_key} 완료 ({duration:.0f}s)\n{tail}")
            report = _parse_crawl_output(result.stdout, duration, suffix)
            _send(report)
        else:
            stderr_tail = result.stderr[-1000:] if result.stderr else "(stderr 없음)"
            logger.error(f"[scheduler] {platform_key} 실패 (code={result.returncode})\n{stderr_tail}")
            report = _parse_crawl_output(result.stdout or "", duration, suffix)
            report.setdefault("failed_sellers", [])
            report["failed_sellers"].append(f"크롤러 비정상 종료 (code={result.returncode})")
            report["stderr"] = stderr_tail
            _send(report)

    except subprocess.TimeoutExpired as te:
        # ⚠️ 과거 버그: 이 분기에서 로그만 남기고 메일을 전혀 보내지 않아
        # "완료 메일이 안 온다"는 증상의 핵심 원인이었다. 타임아웃이 나도
        # 반드시 실패 리포트 메일을 보낸다 (te.stdout/te.stderr에 킬 직전까지
        # 캡처된 출력이 들어있으므로 그걸로 partial summary라도 구성).
        duration = time.time() - t0
        logger.error(f"[scheduler] {platform_key} 타임아웃 ({timeout_sec}s 초과) — 프로세스 강제 종료")
        partial_stdout = te.stdout or ""
        try:
            report = _parse_crawl_output(partial_stdout, duration, suffix)
        except Exception:
            report = {"crawl_date": datetime.now(_KST).date().isoformat(),
                       "total_saved": 0, "seller_summary": [], "failed_sellers": []}
        report.setdefault("failed_sellers", [])
        report["failed_sellers"].append(
            f"⏱ 타임아웃: {timeout_sec//60}분 내에 완료되지 않아 강제 종료됨"
        )
        report["stderr"] = (te.stderr or "")[-1000:] if te.stderr else \
            f"프로세스가 {timeout_sec}초 제한시간을 초과하여 강제 종료되었습니다."
        _send(report)

    except Exception as e:
        duration = time.time() - t0
        logger.exception(f"[scheduler] {platform_key} 크롤러 실행 오류: {e}")
        report = {
            "crawl_date":     datetime.now(_KST).date().isoformat(),
            "total_saved":    0,
            "seller_summary": [],
            "failed_sellers": [f"실행 오류: {e}"],
            "duration_sec":   round(duration, 1),
            "stderr":         str(e),
        }
        _send(report)


def _run_crawl():
    """스케줄 실행 진입점 — 식봄/오늘얼마를 서로 독립된 프로세스로 순차 실행.
    (배민은 현재 로컬 PC에서 별도 실행 — 기존 운영 방식 유지)
    플랫폼별 독립 타임아웃 + 결과와 무관하게 반드시 완료/실패 메일 발송."""
    _run_single_platform("--food",       "foodspring", "_food",       timeout_sec=5400)  # 1.5시간
    _run_single_platform("--todaysales", "todaysales", "_todaysales", timeout_sec=1800)   # 30분


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

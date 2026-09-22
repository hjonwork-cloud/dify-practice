"""
사내 SMS(direct.dongwon.com) 세션쿠키 자동 유지(keep-alive) 스케줄러
────────────────────────────────────────────────────────────────
ASP.NET 세션은 보통 일정 시간(예: 20~30분) 동안 요청이 없으면 슬라이딩 방식으로
자동 만료된다. DM 발송이 뜸하면(예: 몇 시간에 한 번) 그 사이에 세션이 끊겨
"세션 쿠키가 만료되었습니다" 오류가 발생하고, 그 시점부터는 관리자가 다시
브라우저에서 로그인 → 쿠키 복사 → 저장을 해줘야만 복구되었다.

이 모듈은 저장된 쿠키로 주기적으로(기본 10분마다) 가벼운 인증 확인 요청을
보내 세션의 활동 시각을 계속 갱신함으로써, 실제 로그인 세션이 "살아있는 한"
만료되지 않도록 유지한다. (로그인 자체를 자동화하는 것은 아니며, 이미 발급된
세션을 오래 살려두는 역할만 한다 — 브라우저를 계속 열어두는 것과 동일한 효과.)

점검 결과(성공/실패, 시각, 메시지)는 portal_db의 app_settings에
"sms_cookie_health" 키로 저장되어 관리자 화면에서 확인할 수 있다.
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone, timedelta

logger = logging.getLogger(__name__)

_KST = timezone(timedelta(hours=9))
_scheduler = None
_scheduler_lock = threading.Lock()

# 점검 주기(분) — 사내 세션 idle-timeout 보다 충분히 짧게 잡아야 함
_INTERVAL_MINUTES = 10


def _run_keepalive_check():
    """(내부용) 저장된 SMS 쿠키로 실제 발송 없이 인증 확인 요청 1회 수행,
    결과를 app_settings("sms_cookie_health")에 기록한다."""
    try:
        # 지연 import — portal_router ↔ 다른 모듈간 순환 import 방지
        import portal_router as _pr
        import portal_db as _db

        cookie = _pr._get_sms_cookie()
        now_str = datetime.now(_KST).strftime("%Y-%m-%d %H:%M:%S")
        if not cookie:
            _db.set_setting(
                "sms_cookie_health",
                json.dumps({"ts": now_str, "valid": False, "message": "저장된 쿠키 없음"}, ensure_ascii=False),
                updated_by="system",
            )
            logger.info("[sms_keepalive] 저장된 쿠키가 없어 점검을 건너뜁니다.")
            return

        result = _pr._test_sms_cookie_validity(cookie)
        _db.set_setting(
            "sms_cookie_health",
            json.dumps({
                "ts": now_str,
                "valid": bool(result.get("valid")),
                "message": result.get("message", ""),
            }, ensure_ascii=False),
            updated_by="system",
        )
        if result.get("valid"):
            logger.info("[sms_keepalive] 세션 유지 점검 성공 (%s)", now_str)
        else:
            logger.warning("[sms_keepalive] 세션 유지 점검 실패: %s — 관리자 재로그인 필요할 수 있음", result.get("message"))
    except Exception:
        logger.exception("[sms_keepalive] 점검 중 예외 발생")


def start():
    """스케줄러 시작 (앱 startup 시 호출). 이미 실행 중이면 무시."""
    global _scheduler
    with _scheduler_lock:
        if _scheduler is not None:
            return
        try:
            from apscheduler.schedulers.background import BackgroundScheduler
            from apscheduler.triggers.interval import IntervalTrigger
        except ImportError:
            logger.warning("[sms_keepalive] apscheduler 미설치 — SMS 세션 자동 유지 비활성화")
            return

        _scheduler = BackgroundScheduler(timezone="Asia/Seoul")
        _scheduler.add_job(
            _run_keepalive_check,
            trigger=IntervalTrigger(minutes=_INTERVAL_MINUTES),
            id="sms_cookie_keepalive",
            name=f"SMS 세션쿠키 자동 유지 점검 ({_INTERVAL_MINUTES}분마다)",
            replace_existing=True,
            misfire_grace_time=300,
            next_run_time=datetime.now(),  # 시작 즉시 1회 점검
        )
        _scheduler.start()
        logger.info("[sms_keepalive] 시작됨 — %d분 간격으로 세션 유지 점검", _INTERVAL_MINUTES)


def stop():
    """스케줄러 종료 (앱 shutdown 시 호출)."""
    global _scheduler
    with _scheduler_lock:
        if _scheduler is not None:
            _scheduler.shutdown(wait=False)
            _scheduler = None
            logger.info("[sms_keepalive] 종료됨")


def status() -> dict:
    """현재 스케줄러 상태 반환."""
    if _scheduler is None:
        return {"running": False}
    job = _scheduler.get_job("sms_cookie_keepalive")
    return {
        "running": True,
        "interval_minutes": _INTERVAL_MINUTES,
        "next_run": str(job.next_run_time) if job else None,
    }

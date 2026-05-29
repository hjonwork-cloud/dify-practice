"""
action_router.py — 세일즈 액션 제안 FastAPI 라우터
"""
from __future__ import annotations
import datetime
import json
import random
from pathlib import Path
from typing import Callable

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from action_db import (
    create_proposal, get_proposal, is_expired,
    mark_viewed, record_response, set_deadline,
    already_sent_today, record_daily_sent, get_today_sent_types,
)
from action_signals import run_all_signals

router = APIRouter(prefix="/action", tags=["action"])

_TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))


def _get_base_url() -> str:
    """ngrok 로컬 API → 공개 URL 자동 감지, 실패 시 환경변수 사용"""
    import urllib.request as _ur
    import json as _json
    import os as _os
    try:
        with _ur.urlopen("http://localhost:4040/api/tunnels", timeout=2) as r:
            data = _json.loads(r.read())
            for t in data.get("tunnels", []):
                if t.get("proto") == "https":
                    return t["public_url"].rstrip("/")
    except Exception:
        pass
    return _os.getenv("NGROK_URL", "http://localhost:8000").rstrip("/")

# 시그널 한국어 레이블
SIGNAL_LABELS = {
    "ITEM_CHURN":       "단품 이탈 감지",
    "STD_ITEM_MISSING": "표준 품목 미사용",
    "BRAND_UNSHIPPED":  "미출고 다발",
    "LOW_CM":           "저마진 지속",
}


# ─── 제안 생성 (카카오 콜백에서 호출) ───────────────────────────

def generate_action_proposal(
    brand: str,
    user_id: str,
    target_team: str,
    query_fn: Callable,
    base_url: str,
) -> str | None:
    """
    시그널 감지 후 제안 생성 → 카카오 전송용 URL 반환
    이미 당일 해당 브랜드에 모든 시그널을 보낸 경우 None 반환
    """
    # 당일 이미 보낸 타입 제외
    sent_today = get_today_sent_types(user_id, brand)
    signals = run_all_signals(brand, query_fn, exclude_types=sent_today)
    if not signals:
        return None

    # 우선순위 높은 것 중 랜덤 1개
    high = [s for s in signals if s.get("priority") == 1] or signals
    chosen = random.choice(high)

    pid = create_proposal(
        user_id=user_id,
        brand_name=brand,
        action_type=chosen["action_type"],
        title=chosen["title"],
        summary=chosen.get("summary", {}),
        detail=chosen.get("detail", {}),
        priority=chosen.get("priority", 2),
        target_team=target_team,
    )
    record_daily_sent(user_id, brand, chosen["action_type"])

    url = f"{base_url.rstrip('/')}/action/report/{pid}"
    return url


# ─── HTML 리포트 페이지 ─────────────────────────────────────

@router.get("/report/{proposal_id}", response_class=HTMLResponse)
async def action_report(request: Request, proposal_id: str):
    import traceback as _tb
    try:
        return await _action_report_inner(request, proposal_id)
    except Exception as e:
        err = _tb.format_exc()
        import logging
        logging.getLogger("action_router").error(f"[report] 500 오류:\n{err}")
        return HTMLResponse(f"<pre>오류:\n{err}</pre>", status_code=500)


def _render_template(name: str, **ctx) -> str:
    """Starlette TemplateResponse 우회 — 직접 Jinja2 렌더링 (캐시 버그 회피)."""
    from jinja2 import Environment, FileSystemLoader
    env = Environment(loader=FileSystemLoader(str(_TEMPLATES_DIR)), autoescape=True)
    tpl = env.get_template(name)
    return tpl.render(**ctx)


async def _action_report_inner(request: Request, proposal_id: str):
    proposal = get_proposal(proposal_id)
    base_url = str(request.base_url).rstrip("/")

    if not proposal or is_expired(proposal_id):
        html = _render_template(
            "action_report.html",
            expired=True, proposal={}, base_url=base_url,
        )
        return HTMLResponse(html)

    mark_viewed(proposal_id, proposal.get("user_id", ""))

    summary = json.loads(proposal.get("summary_json") or "{}")
    detail  = json.loads(proposal.get("detail_json")  or "{}")

    overview_rows = detail.get("overview", [])
    detail_rows   = detail.get("rows", [])
    detail_cols   = list(detail_rows[0].keys()) if detail_rows else []

    today = datetime.date.today().isoformat()
    default_deadline = (datetime.date.today() + datetime.timedelta(days=14)).isoformat()

    html = _render_template(
        "action_report.html",
        expired=False,
        proposal=proposal,
        summary=summary,
        overview_rows=overview_rows,
        detail_rows=detail_rows,
        detail_cols=detail_cols,
        signal_type_label=SIGNAL_LABELS.get(proposal.get("action_type", ""), ""),
        today=today,
        default_deadline=default_deadline,
        base_url=base_url,
    )
    return HTMLResponse(html)


# ─── 열람 기록 ──────────────────────────────────────────────

class ViewReq(BaseModel):
    proposal_id: str

@router.post("/view")
async def action_view(body: ViewReq, request: Request):
    user_ip = request.client.host if request.client else ""
    p = get_proposal(body.proposal_id)
    if p:
        mark_viewed(body.proposal_id, p.get("user_id", user_ip))
    return {"ok": True}


# ─── 1차 응답 (실행/보류/해당없음) ─────────────────────────

class RespondReq(BaseModel):
    proposal_id: str
    response: str       # approve / hold / reject
    hold_reason: str = ""

@router.post("/respond")
async def action_respond(body: RespondReq):
    p = get_proposal(body.proposal_id)
    if not p:
        return JSONResponse({"ok": False, "error": "not found"}, status_code=404)
    if is_expired(body.proposal_id):
        return JSONResponse({"ok": False, "error": "expired"}, status_code=410)
    record_response(
        body.proposal_id,
        p.get("user_id", ""),
        body.response,
        body.hold_reason,
    )
    return {"ok": True}


# ─── 데드라인 설정 ──────────────────────────────────────────

class DeadlineReq(BaseModel):
    proposal_id: str
    deadline: str   # YYYY-MM-DD

@router.post("/deadline")
async def action_deadline(body: DeadlineReq):
    p = get_proposal(body.proposal_id)
    if not p:
        return JSONResponse({"ok": False, "error": "not found"}, status_code=404)
    set_deadline(body.proposal_id, p.get("user_id", ""), body.deadline)
    return {"ok": True}


# ─── 미완료 현황 조회 (관리자용) ────────────────────────────

@router.get("/admin/pending")
async def admin_pending():
    import sqlite3
    from action_db import DB_PATH, _get_conn
    with _get_conn() as conn:
        rows = conn.execute("""
            SELECT proposal_id, brand_name, action_type, title,
                   status, step, created_at, viewed_at, responded_at, deadline
            FROM action_proposals
            ORDER BY created_at DESC
            LIMIT 100
        """).fetchall()
    return [dict(r) for r in rows]

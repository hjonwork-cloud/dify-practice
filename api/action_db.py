"""
action_db.py — 세일즈 액션 제안 SQLite 저장소
"""
import sqlite3
import uuid
import json
import datetime
import os
from pathlib import Path

DB_PATH = Path(__file__).parent / "action_store.db"


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    """테이블 초기화 (없으면 생성)"""
    with _get_conn() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS action_proposals (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            proposal_id   TEXT NOT NULL UNIQUE,   -- UUID
            created_at    TEXT NOT NULL,
            expires_at    TEXT NOT NULL,           -- 48시간 유효
            user_id       TEXT,                    -- 카카오 user_id
            target_team   TEXT,                    -- 팀명
            brand_name    TEXT,                    -- 브랜드명
            action_type   TEXT NOT NULL,           -- 시그널 코드
            title         TEXT,
            priority      INTEGER DEFAULT 2,       -- 1:높음 2:보통 3:낮음
            summary_json  TEXT,                    -- 요약 지표 JSON
            detail_json   TEXT,                    -- 세부 데이터 JSON
            status        TEXT DEFAULT 'sent',     -- sent/viewed/responded/completed
            step          INTEGER DEFAULT 0,       -- 0:미열람 1:열람 2:1차응답 3:완료
            response_1    TEXT,                    -- approve/hold/reject
            hold_reason   TEXT,                    -- 보류 사유
            deadline      TEXT,                    -- YYYY-MM-DD
            responded_at  TEXT,
            completed_at  TEXT,
            viewed_at     TEXT
        );

        CREATE TABLE IF NOT EXISTS action_logs (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            proposal_id TEXT NOT NULL,
            logged_at   TEXT NOT NULL,
            user_id     TEXT,
            action      TEXT,   -- view/approve/hold/reject/deadline_set/remind
            payload     TEXT    -- JSON
        );

        CREATE TABLE IF NOT EXISTS action_daily_sent (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            send_date   TEXT NOT NULL,   -- YYYY-MM-DD
            user_id     TEXT NOT NULL,
            brand_name  TEXT NOT NULL,
            action_type TEXT NOT NULL
        );
        """)


# ─── 제안 생성 ──────────────────────────────────────────────

def create_proposal(
    user_id: str,
    brand_name: str,
    action_type: str,
    title: str,
    summary: dict,
    detail: dict,
    priority: int = 2,
    target_team: str = "",
    ttl_hours: int = 48,
) -> str:
    """새 제안 저장 → proposal_id 반환"""
    init_db()
    pid = str(uuid.uuid4())
    now = datetime.datetime.now()
    expires = now + datetime.timedelta(hours=ttl_hours)
    with _get_conn() as conn:
        conn.execute("""
            INSERT INTO action_proposals
              (proposal_id, created_at, expires_at, user_id, target_team,
               brand_name, action_type, title, priority, summary_json, detail_json)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """, (
            pid,
            now.isoformat(timespec="seconds"),
            expires.isoformat(timespec="seconds"),
            user_id, target_team, brand_name, action_type, title, priority,
            json.dumps(summary, ensure_ascii=False),
            json.dumps(detail, ensure_ascii=False),
        ))
        _log(conn, pid, user_id, "created", {})
    return pid


# ─── 열람 기록 ──────────────────────────────────────────────

def mark_viewed(proposal_id: str, user_id: str = ""):
    init_db()
    now = datetime.datetime.now().isoformat(timespec="seconds")
    with _get_conn() as conn:
        conn.execute("""
            UPDATE action_proposals
            SET status='viewed', step=MAX(step,1), viewed_at=?
            WHERE proposal_id=? AND step=0
        """, (now, proposal_id))
        _log(conn, proposal_id, user_id, "view", {})


# ─── 1차 응답 ──────────────────────────────────────────────

def record_response(
    proposal_id: str,
    user_id: str,
    response: str,           # approve / hold / reject
    hold_reason: str = "",
):
    init_db()
    now = datetime.datetime.now().isoformat(timespec="seconds")
    status = {"approve": "approved", "hold": "hold", "reject": "rejected"}.get(response, "responded")
    with _get_conn() as conn:
        conn.execute("""
            UPDATE action_proposals
            SET response_1=?, hold_reason=?, status=?, step=MAX(step,2),
                responded_at=?
            WHERE proposal_id=?
        """, (response, hold_reason, status, now, proposal_id))
        _log(conn, proposal_id, user_id, response, {"hold_reason": hold_reason})


# ─── 데드라인 설정 ───────────────────────────────────────────

def set_deadline(proposal_id: str, user_id: str, deadline: str):
    """deadline: YYYY-MM-DD"""
    init_db()
    now = datetime.datetime.now().isoformat(timespec="seconds")
    with _get_conn() as conn:
        conn.execute("""
            UPDATE action_proposals
            SET deadline=?, status='completed', step=3, completed_at=?
            WHERE proposal_id=?
        """, (deadline, now, proposal_id))
        _log(conn, proposal_id, user_id, "deadline_set", {"deadline": deadline})


# ─── 조회 ──────────────────────────────────────────────────

def get_proposal(proposal_id: str) -> dict | None:
    init_db()
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM action_proposals WHERE proposal_id=?", (proposal_id,)
        ).fetchone()
    return dict(row) if row else None


def is_expired(proposal_id: str) -> bool:
    p = get_proposal(proposal_id)
    if not p:
        return True
    return datetime.datetime.now().isoformat() > p["expires_at"]


# ─── 당일 발송 중복 체크 ────────────────────────────────────

def already_sent_today(user_id: str, brand_name: str, action_type: str) -> bool:
    """당일 동일 브랜드+동일 시그널 중복 방지"""
    init_db()
    today = datetime.date.today().isoformat()
    with _get_conn() as conn:
        row = conn.execute("""
            SELECT 1 FROM action_daily_sent
            WHERE send_date=? AND user_id=? AND brand_name=? AND action_type=?
        """, (today, user_id, brand_name, action_type)).fetchone()
    return row is not None


def record_daily_sent(user_id: str, brand_name: str, action_type: str):
    init_db()
    today = datetime.date.today().isoformat()
    with _get_conn() as conn:
        conn.execute("""
            INSERT INTO action_daily_sent (send_date, user_id, brand_name, action_type)
            VALUES (?,?,?,?)
        """, (today, user_id, brand_name, action_type))


def get_today_sent_types(user_id: str, brand_name: str) -> list[str]:
    """오늘 해당 브랜드에 이미 보낸 시그널 타입 목록"""
    init_db()
    today = datetime.date.today().isoformat()
    with _get_conn() as conn:
        rows = conn.execute("""
            SELECT action_type FROM action_daily_sent
            WHERE send_date=? AND user_id=? AND brand_name=?
        """, (today, user_id, brand_name)).fetchall()
    return [r["action_type"] for r in rows]


# ─── 미응답 목록 (리마인드용) ────────────────────────────────

def get_unresponded(hours_since: int = 24) -> list[dict]:
    """열람 후 N시간 이상 미응답 제안 목록"""
    init_db()
    cutoff = (datetime.datetime.now() - datetime.timedelta(hours=hours_since)).isoformat()
    with _get_conn() as conn:
        rows = conn.execute("""
            SELECT * FROM action_proposals
            WHERE step=1 AND viewed_at < ? AND status='viewed'
        """, (cutoff,)).fetchall()
    return [dict(r) for r in rows]


def get_unviewed(hours_since: int = 24) -> list[dict]:
    """발송 후 N시간 이상 미열람 제안 목록"""
    init_db()
    cutoff = (datetime.datetime.now() - datetime.timedelta(hours=hours_since)).isoformat()
    with _get_conn() as conn:
        rows = conn.execute("""
            SELECT * FROM action_proposals
            WHERE step=0 AND created_at < ? AND status='sent'
        """, (cutoff,)).fetchall()
    return [dict(r) for r in rows]


# ─── 내부 로그 헬퍼 ─────────────────────────────────────────

def _log(conn, proposal_id: str, user_id: str, action: str, payload: dict):
    conn.execute("""
        INSERT INTO action_logs (proposal_id, logged_at, user_id, action, payload)
        VALUES (?,?,?,?,?)
    """, (
        proposal_id,
        datetime.datetime.now().isoformat(timespec="seconds"),
        user_id, action,
        json.dumps(payload, ensure_ascii=False),
    ))


# ─── 초기화 실행 ────────────────────────────────────────────
init_db()

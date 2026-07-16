"""관리자 웹 콘솔의 VOC·FAQ·감사 저장소."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import secrets
import sqlite3
import threading
import unicodedata
from contextlib import contextmanager
from pathlib import Path

DATA_DIR = Path(os.getenv("CHATBOT_DATA_DIR", r"E:\data\chatbot"))
DB_PATH = DATA_DIR / "admin_console.db"
_db_lock = threading.Lock()

VOC_STATUSES = ("new", "reviewing", "waiting_data", "answered", "published", "closed")
VOC_CATEGORIES = ("미분류", "매출", "수익성", "미출고", "오류", "기능요청", "정책안내", "기타")
ADMIN_ROLES = ("system_admin", "voc_operator", "action_operator", "viewer")


def _now() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def _get_conn() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def _connection():
    """SQLite 연결을 트랜잭션 종료 후 명시적으로 닫아 Windows 파일 잠금을 방지한다."""
    conn = _get_conn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def hash_password(password: str, salt: str | None = None) -> str:
    """외부 패키지 없이 scrypt 기반 비밀번호 해시를 생성한다."""
    raw_salt = bytes.fromhex(salt) if salt else secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=raw_salt, n=16384, r=8, p=1)
    return f"scrypt$16384$8$1${raw_salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, n, r, p, salt, digest = stored.split("$")
        actual = hashlib.scrypt(
            password.encode("utf-8"), salt=bytes.fromhex(salt),
            n=int(n), r=int(r), p=int(p),
        ).hex()
        return secrets.compare_digest(actual, digest)
    except (ValueError, AttributeError):
        return False


def normalize_question(question: str) -> str:
    """초기 VOC 중복 집계용 보수적인 정규화: 공백·대소문자·문장부호만 정리."""
    value = unicodedata.normalize("NFKC", question).strip().lower()
    value = "".join(ch for ch in value if ch.isalnum() or ch.isspace())
    return " ".join(value.split())


def init_db() -> None:
    with _db_lock, _connection() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS admin_accounts (
            admin_id TEXT PRIMARY KEY,
            username TEXT NOT NULL UNIQUE,
            display_name TEXT NOT NULL,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'viewer',
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            last_login_at TEXT
        );

        CREATE TABLE IF NOT EXISTS voc_cases (
            case_id TEXT PRIMARY KEY,
            normalized_question TEXT NOT NULL UNIQUE,
            representative_question TEXT NOT NULL,
            category TEXT NOT NULL DEFAULT '미분류',
            status TEXT NOT NULL DEFAULT 'new',
            priority TEXT NOT NULL DEFAULT 'normal',
            occurrence_count INTEGER NOT NULL DEFAULT 1,
            owner_admin_id TEXT,
            public_answer TEXT,
            internal_note TEXT,
            answer_type TEXT,
            faq_id TEXT,
            first_received_at TEXT NOT NULL,
            last_received_at TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS voc_occurrences (
            occurrence_id TEXT PRIMARY KEY,
            case_id TEXT NOT NULL,
            user_id TEXT,
            user_name TEXT,
            team TEXT,
            original_question TEXT NOT NULL,
            received_at TEXT NOT NULL,
            channel TEXT NOT NULL DEFAULT 'kakao',
            request_context TEXT,
            FOREIGN KEY(case_id) REFERENCES voc_cases(case_id)
        );

        CREATE TABLE IF NOT EXISTS faq_entries (
            faq_id TEXT PRIMARY KEY,
            case_id TEXT,
            title TEXT NOT NULL,
            category TEXT NOT NULL DEFAULT '미분류',
            answer TEXT NOT NULL,
            is_published INTEGER NOT NULL DEFAULT 0,
            published_at TEXT,
            published_by TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(case_id) REFERENCES voc_cases(case_id)
        );

        CREATE TABLE IF NOT EXISTS faq_patterns (
            pattern_id TEXT PRIMARY KEY,
            faq_id TEXT NOT NULL,
            normalized_pattern TEXT NOT NULL,
            raw_pattern TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(faq_id, normalized_pattern),
            FOREIGN KEY(faq_id) REFERENCES faq_entries(faq_id)
        );

        CREATE TABLE IF NOT EXISTS audit_logs (
            audit_id TEXT PRIMARY KEY,
            actor_admin_id TEXT,
            action TEXT NOT NULL,
            entity_type TEXT NOT NULL,
            entity_id TEXT NOT NULL,
            before_json TEXT,
            after_json TEXT,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_voc_status ON voc_cases(status, last_received_at DESC);
        CREATE INDEX IF NOT EXISTS idx_occurrences_case ON voc_occurrences(case_id, received_at DESC);
        CREATE INDEX IF NOT EXISTS idx_faq_patterns_lookup ON faq_patterns(normalized_pattern);
        """)
    _bootstrap_admin_from_environment()


def _bootstrap_admin_from_environment() -> None:
    """환경변수로만 최초 시스템 관리자를 만든다. 기본 계정은 제공하지 않는다."""
    username = os.getenv("ADMIN_CONSOLE_USERNAME", "").strip()
    password = os.getenv("ADMIN_CONSOLE_PASSWORD", "")
    if not username or not password:
        return
    with _connection() as conn:
        exists = conn.execute("SELECT 1 FROM admin_accounts WHERE username=?", (username,)).fetchone()
        if exists:
            return
        now = _now()
        conn.execute(
            """INSERT INTO admin_accounts
            (admin_id, username, display_name, password_hash, role, created_at, updated_at)
            VALUES (?, ?, ?, ?, 'system_admin', ?, ?)""",
            (secrets.token_hex(16), username, os.getenv("ADMIN_CONSOLE_DISPLAY_NAME", username), hash_password(password), now, now),
        )


def _audit(conn: sqlite3.Connection, actor: str | None, action: str, entity_type: str, entity_id: str, before=None, after=None) -> None:
    conn.execute(
        """INSERT INTO audit_logs
        (audit_id, actor_admin_id, action, entity_type, entity_id, before_json, after_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (secrets.token_hex(16), actor, action, entity_type, entity_id,
         json.dumps(before, ensure_ascii=False) if before is not None else None,
         json.dumps(after, ensure_ascii=False) if after is not None else None, _now()),
    )


def authenticate(username: str, password: str) -> dict | None:
    init_db()
    with _connection() as conn:
        row = conn.execute("SELECT * FROM admin_accounts WHERE username=? AND active=1", (username,)).fetchone()
        if not row or not verify_password(password, row["password_hash"]):
            return None
        conn.execute("UPDATE admin_accounts SET last_login_at=?, updated_at=? WHERE admin_id=?", (_now(), _now(), row["admin_id"]))
        return dict(row)


def get_admin(admin_id: str) -> dict | None:
    init_db()
    with _connection() as conn:
        row = conn.execute("SELECT * FROM admin_accounts WHERE admin_id=? AND active=1", (admin_id,)).fetchone()
    return dict(row) if row else None


def list_admins() -> list[dict]:
    init_db()
    with _connection() as conn:
        rows = conn.execute("SELECT admin_id, username, display_name, role, active, created_at, last_login_at FROM admin_accounts ORDER BY created_at").fetchall()
    return [dict(row) for row in rows]


def record_unanswered_question(question: str, user_id: str, user_name: str = "", team: str = "", context: dict | None = None) -> str:
    """미인식 질문을 VOC로 저장하거나 기존 정규화 질문에 발생 이력을 추가한다."""
    init_db()
    normalized = normalize_question(question)
    if not normalized:
        raise ValueError("빈 질문은 VOC로 저장할 수 없습니다.")
    now = _now()
    with _db_lock, _connection() as conn:
        case = conn.execute("SELECT * FROM voc_cases WHERE normalized_question=?", (normalized,)).fetchone()
        if case:
            case_id = case["case_id"]
            conn.execute("UPDATE voc_cases SET occurrence_count=occurrence_count+1, last_received_at=?, updated_at=? WHERE case_id=?", (now, now, case_id))
        else:
            case_id = f"VOC-{dt.datetime.now():%Y%m%d}-{secrets.token_hex(4).upper()}"
            conn.execute(
                """INSERT INTO voc_cases
                (case_id, normalized_question, representative_question, first_received_at, last_received_at, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (case_id, normalized, question.strip(), now, now, now, now),
            )
        conn.execute(
            """INSERT INTO voc_occurrences
            (occurrence_id, case_id, user_id, user_name, team, original_question, received_at, request_context)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (secrets.token_hex(16), case_id, user_id, user_name, team, question.strip(), now,
             json.dumps(context or {}, ensure_ascii=False)),
        )
        return case_id


def find_published_faq(question: str) -> dict | None:
    init_db()
    normalized = normalize_question(question)
    if not normalized:
        return None
    with _connection() as conn:
        row = conn.execute(
            """SELECT f.* FROM faq_patterns p
            JOIN faq_entries f ON f.faq_id=p.faq_id
            WHERE p.normalized_pattern=? AND f.is_published=1
            ORDER BY f.updated_at DESC LIMIT 1""", (normalized,),
        ).fetchone()
    return dict(row) if row else None


def dashboard_metrics() -> dict:
    init_db()
    with _connection() as conn:
        status_rows = conn.execute("SELECT status, COUNT(*) AS count FROM voc_cases GROUP BY status").fetchall()
        recent = conn.execute("SELECT COUNT(*) AS count FROM voc_occurrences WHERE received_at >= datetime('now', '-7 days')").fetchone()["count"]
        faq = conn.execute("SELECT COUNT(*) AS count FROM faq_entries WHERE is_published=1").fetchone()["count"]
        admins = conn.execute("SELECT COUNT(*) AS count FROM admin_accounts WHERE active=1").fetchone()["count"]
        top_cases = conn.execute("SELECT case_id, representative_question, occurrence_count, status FROM voc_cases ORDER BY occurrence_count DESC, last_received_at DESC LIMIT 5").fetchall()
    statuses = {row["status"]: row["count"] for row in status_rows}
    return {"new": statuses.get("new", 0), "pending": sum(statuses.get(s, 0) for s in ("reviewing", "waiting_data", "answered")), "published": faq, "recent": recent, "admins": admins, "top_cases": [dict(row) for row in top_cases]}


def list_voc_cases(status: str = "", keyword: str = "", category: str = "") -> list[dict]:
    init_db()
    clauses, params = [], []
    if status:
        clauses.append("c.status=?")
        params.append(status)
    if category:
        clauses.append("c.category=?")
        params.append(category)
    if keyword:
        clauses.append("(c.representative_question LIKE ? OR c.public_answer LIKE ?)")
        params.extend([f"%{keyword}%", f"%{keyword}%"])
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    with _connection() as conn:
        rows = conn.execute(
            f"""SELECT c.*, a.display_name AS owner_name
            FROM voc_cases c LEFT JOIN admin_accounts a ON c.owner_admin_id=a.admin_id
            {where} ORDER BY CASE c.status WHEN 'new' THEN 0 ELSE 1 END, c.last_received_at DESC""", params,
        ).fetchall()
    return [dict(row) for row in rows]


def get_voc_case(case_id: str) -> dict | None:
    init_db()
    with _connection() as conn:
        case = conn.execute("SELECT * FROM voc_cases WHERE case_id=?", (case_id,)).fetchone()
        if not case:
            return None
        occurrences = conn.execute("SELECT * FROM voc_occurrences WHERE case_id=? ORDER BY received_at DESC LIMIT 100", (case_id,)).fetchall()
        data = dict(case)
        data["occurrences"] = [dict(row) for row in occurrences]
        return data


def update_voc_case(case_id: str, actor_id: str, *, category: str, status: str, priority: str, owner_admin_id: str | None, public_answer: str, internal_note: str, answer_type: str) -> None:
    if status not in VOC_STATUSES or category not in VOC_CATEGORIES:
        raise ValueError("유효하지 않은 상태 또는 분류입니다.")
    with _connection() as conn:
        before = conn.execute("SELECT * FROM voc_cases WHERE case_id=?", (case_id,)).fetchone()
        if not before:
            raise LookupError("VOC를 찾을 수 없습니다.")
        now = _now()
        conn.execute(
            """UPDATE voc_cases SET category=?, status=?, priority=?, owner_admin_id=?, public_answer=?, internal_note=?, answer_type=?, updated_at=? WHERE case_id=?""",
            (category, status, priority, owner_admin_id or None, public_answer.strip() or None, internal_note.strip() or None, answer_type.strip() or None, now, case_id),
        )
        after = conn.execute("SELECT * FROM voc_cases WHERE case_id=?", (case_id,)).fetchone()
        _audit(conn, actor_id, "voc_updated", "voc_case", case_id, dict(before), dict(after))


def publish_voc_as_faq(case_id: str, actor_id: str, patterns: list[str]) -> str:
    with _connection() as conn:
        case = conn.execute("SELECT * FROM voc_cases WHERE case_id=?", (case_id,)).fetchone()
        if not case:
            raise LookupError("VOC를 찾을 수 없습니다.")
        if not (case["public_answer"] or "").strip():
            raise ValueError("공개할 사용자 답변을 먼저 작성해주세요.")
        faq_id = case["faq_id"] or f"FAQ-{secrets.token_hex(8).upper()}"
        now = _now()
        conn.execute(
            """INSERT INTO faq_entries (faq_id, case_id, title, category, answer, is_published, published_at, published_by, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, ?)
            ON CONFLICT(faq_id) DO UPDATE SET title=excluded.title, category=excluded.category, answer=excluded.answer,
              is_published=1, published_at=excluded.published_at, published_by=excluded.published_by, updated_at=excluded.updated_at""",
            (faq_id, case_id, case["representative_question"], case["category"], case["public_answer"], now, actor_id, now, now),
        )
        conn.execute("DELETE FROM faq_patterns WHERE faq_id=?", (faq_id,))
        raw_patterns = [case["representative_question"], *patterns]
        for raw in dict.fromkeys(p.strip() for p in raw_patterns if p and p.strip()):
            normalized = normalize_question(raw)
            if normalized:
                conn.execute("INSERT OR IGNORE INTO faq_patterns (pattern_id, faq_id, normalized_pattern, raw_pattern, created_at) VALUES (?, ?, ?, ?, ?)", (secrets.token_hex(16), faq_id, normalized, raw, now))
        conn.execute("UPDATE voc_cases SET status='published', faq_id=?, updated_at=? WHERE case_id=?", (faq_id, now, case_id))
        _audit(conn, actor_id, "faq_published", "voc_case", case_id, None, {"faq_id": faq_id, "patterns": raw_patterns})
        return faq_id


def list_faqs() -> list[dict]:
    init_db()
    with _connection() as conn:
        rows = conn.execute("SELECT * FROM faq_entries ORDER BY is_published DESC, updated_at DESC").fetchall()
    return [dict(row) for row in rows]


def recent_audit_logs(limit: int = 100) -> list[dict]:
    init_db()
    with _connection() as conn:
        rows = conn.execute("""SELECT l.*, a.display_name AS actor_name FROM audit_logs l
        LEFT JOIN admin_accounts a ON l.actor_admin_id=a.admin_id ORDER BY l.created_at DESC LIMIT ?""", (limit,)).fetchall()
    return [dict(row) for row in rows]


def record_audit(actor_admin_id: str | None, action: str, entity_type: str, entity_id: str, before=None, after=None) -> None:
    """외부 모듈(사용자 관리 등)에서 감사 이력을 남긴다."""
    init_db()
    with _connection() as conn:
        _audit(conn, actor_admin_id, action, entity_type, entity_id, before, after)


init_db()

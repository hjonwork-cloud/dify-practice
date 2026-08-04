"""영업사원 포털 활동 로그 저장소."""
from __future__ import annotations

import hashlib
import os
import sys
import secrets
import sqlite3
from pathlib import Path
from datetime import datetime
try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo  # type: ignore

def _default_data_dir() -> str:
    """환경변수 없을 때 OS별 기본 경로 반환. Azure App Service는 /home 이 영구 스토리지."""
    if os.getenv("CHATBOT_DATA_DIR"):
        return os.getenv("CHATBOT_DATA_DIR")
    if os.getenv("DATA_DIR"):
        return os.getenv("DATA_DIR")
    # Azure App Service (Linux) 판별: /home 존재 여부
    if sys.platform != "win32" and Path("/home").exists():
        return "/home/data/chatbot"
    return r"E:\data\chatbot"

DATA_DIR = Path(_default_data_dir())
try:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
except Exception:
    # 쓰기 불가 시 /tmp fallback
    DATA_DIR = Path("/tmp/chatbot")
    DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "portal_activity.db"

_KST = ZoneInfo("Asia/Seoul")


def _now() -> str:
    """현재 한국 시간 (KST) 문자열 반환."""
    return datetime.now(_KST).strftime("%Y-%m-%d %H:%M:%S")


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=3000")
    return conn


def init_db() -> None:
    with _connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS portal_login_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                emp_code TEXT NOT NULL,
                emp_name TEXT,
                team TEXT,
                ip TEXT,
                user_agent TEXT,
                success INTEGER NOT NULL DEFAULT 1,
                reason TEXT,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_portal_login_created ON portal_login_logs(created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_portal_login_emp ON portal_login_logs(emp_code, created_at DESC);

            CREATE TABLE IF NOT EXISTS dm_send_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                emp_code TEXT NOT NULL,
                emp_name TEXT,
                team TEXT,
                brand_code TEXT,
                brand_name TEXT,
                customer_code TEXT,
                customer_name TEXT,
                action_type TEXT,
                product_names TEXT,
                message TEXT,
                price_items_json TEXT,
                sap_saved_count INTEGER DEFAULT 0,
                sap_result_json TEXT,
                status TEXT NOT NULL DEFAULT 'test_logged',
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_dm_created ON dm_send_logs(created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_dm_emp ON dm_send_logs(emp_code, created_at DESC);

            CREATE TABLE IF NOT EXISTS promotion_action_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                emp_code TEXT NOT NULL,
                emp_name TEXT,
                brand_name TEXT,
                customer_code TEXT,
                customer_name TEXT,
                action TEXT NOT NULL,
                detail TEXT,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_action_created ON promotion_action_logs(created_at DESC);
            """
        )
        # 하위호환 마이그레이션
        for col, typedef in [
            ("price_items_json", "TEXT"),
            ("sap_saved_count",  "INTEGER DEFAULT 0"),
            ("sap_result_json",  "TEXT"),
            ("action_ym",        "TEXT"),
        ]:
            try:
                conn.execute(f"ALTER TABLE dm_send_logs ADD COLUMN {col} {typedef}")
            except Exception:
                pass
        # 비밀번호 테이블 마이그레이션
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS portal_user_passwords (
                emp_code      TEXT PRIMARY KEY,
                password_hash TEXT NOT NULL,
                must_change   INTEGER DEFAULT 0,
                failed_count  INTEGER DEFAULT 0,
                locked_until  TEXT,
                created_at    TEXT NOT NULL,
                updated_at    TEXT NOT NULL
            );
        """)
        # VOC 게시판 마이그레이션
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS voc_posts (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                emp_code   TEXT NOT NULL,
                emp_name   TEXT,
                team       TEXT,
                category   TEXT NOT NULL DEFAULT '플랫폼',
                title      TEXT NOT NULL,
                content    TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_voc_posts_created ON voc_posts(created_at DESC);
            CREATE TABLE IF NOT EXISTS voc_comments (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                post_id    INTEGER NOT NULL,
                emp_code   TEXT NOT NULL,
                emp_name   TEXT,
                team       TEXT,
                content    TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_voc_comments_post ON voc_comments(post_id, created_at);
        """)


def record_login(emp_code: str, emp_name: str = "", team: str = "", ip: str = "", user_agent: str = "", success: bool = True, reason: str = "") -> None:
    init_db()
    with _connect() as conn:
        conn.execute(
            """INSERT INTO portal_login_logs
            (emp_code, emp_name, team, ip, user_agent, success, reason, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (emp_code, emp_name, team, ip, user_agent, 1 if success else 0, reason, _now()),
        )


def record_dm_log(*, emp_code: str, emp_name: str, team: str, brand_code: str, brand_name: str,
                  customer_code: str, customer_name: str, action_type: str, product_names: str,
                  message: str, status: str = "test_logged") -> None:
    init_db()
    with _connect() as conn:
        conn.execute(
            """INSERT INTO dm_send_logs
            (emp_code, emp_name, team, brand_code, brand_name, customer_code, customer_name,
             action_type, product_names, message, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (emp_code, emp_name, team, brand_code, brand_name, customer_code, customer_name,
             action_type, product_names, message, status, _now()),
        )
        conn.execute(
            """INSERT INTO promotion_action_logs
            (emp_code, emp_name, brand_name, customer_code, customer_name, action, detail, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (emp_code, emp_name, brand_name, customer_code, customer_name, "dm_test_logged", product_names, _now()),
        )


def record_dm_log_v2(
    *, emp_code: str, emp_name: str, team: str,
    brand_code: str, brand_name: str,
    customer_code: str, customer_name: str,
    action_type: str, product_names: str, message: str,
    price_items_json: str = "",
    sap_saved_count: int = 0,
    sap_result_json: str = "",
    status: str = "dm_only_sent",
) -> None:
    """판가 설정 정보 포함 DM 로그 저장 (v2)."""
    init_db()
    with _connect() as conn:
        conn.execute(
            """INSERT INTO dm_send_logs
            (emp_code, emp_name, team, brand_code, brand_name, customer_code, customer_name,
             action_type, product_names, message,
             price_items_json, sap_saved_count, sap_result_json,
             status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (emp_code, emp_name, team, brand_code, brand_name, customer_code, customer_name,
             action_type, product_names, message,
             price_items_json, sap_saved_count, sap_result_json,
             status, _now()),
        )
        conn.execute(
            """INSERT INTO promotion_action_logs
            (emp_code, emp_name, brand_name, customer_code, customer_name, action, detail, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (emp_code, emp_name, brand_name, customer_code, customer_name, action_type, product_names, _now()),
        )


def list_login_logs(limit: int = 200) -> list[dict]:
    init_db()
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM portal_login_logs ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]


def list_dm_logs(limit: int = 200, emp_code: str | None = None,
                 brand_code: str | None = None, action_ym: str | None = None) -> list[dict]:
    init_db()
    conds, params = [], []
    if emp_code:
        conds.append("emp_code = ?"); params.append(emp_code)
    if brand_code:
        conds.append("brand_code = ?"); params.append(brand_code)
    if action_ym:
        conds.append("action_ym = ?"); params.append(action_ym)
    where = ("WHERE " + " AND ".join(conds)) if conds else ""
    params.append(limit)
    with _connect() as conn:
        rows = conn.execute(
            f"SELECT * FROM dm_send_logs {where} ORDER BY created_at DESC LIMIT ?",
            params,
        ).fetchall()
        return [dict(row) for row in rows]


def list_action_logs(limit: int = 200) -> list[dict]:
    init_db()
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM promotion_action_logs ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]


# ── 비밀번호 관리 ──────────────────────────────────────────────────────

_HASH_ITER = 260_000

def _hash_pw(password: str, salt: str) -> str:
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), _HASH_ITER)
    return dk.hex()


def set_password(emp_code: str, password: str, must_change: bool = False) -> None:
    """비밀번호 설정 (신규/변경 공통)."""
    init_db()
    salt = secrets.token_hex(16)
    pw_hash = f"pbkdf2:{salt}:{_hash_pw(password, salt)}"
    now = _now()
    with _connect() as conn:
        conn.execute("""
            INSERT INTO portal_user_passwords (emp_code, password_hash, must_change, failed_count, created_at, updated_at)
            VALUES (?, ?, ?, 0, ?, ?)
            ON CONFLICT(emp_code) DO UPDATE SET
                password_hash=excluded.password_hash,
                must_change=excluded.must_change,
                failed_count=0,
                locked_until=NULL,
                updated_at=excluded.updated_at
        """, (emp_code, pw_hash, 1 if must_change else 0, now, now))


def check_password(emp_code: str, password: str) -> dict:
    """비밀번호 검증. 반환: {ok, locked, must_change, reason}"""
    init_db()
    now = _now()
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM portal_user_passwords WHERE emp_code = ?", (emp_code,)
        ).fetchone()
        if not row:
            return {"ok": False, "locked": False, "must_change": False, "reason": "no_password"}

        row = dict(row)
        # 잠금 확인
        locked_until = row.get("locked_until")
        if locked_until and locked_until > now:
            return {"ok": False, "locked": True, "must_change": False,
                    "reason": f"계정이 잠겼습니다. {locked_until[:16]} 이후 재시도 가능합니다."}

        # 비밀번호 검증
        stored = row["password_hash"]
        try:
            _, salt, hx = stored.split(":", 2)
            ok = secrets.compare_digest(_hash_pw(password, salt), hx)
        except Exception:
            ok = False

        if ok:
            # 성공: 실패 카운트 초기화
            conn.execute(
                "UPDATE portal_user_passwords SET failed_count=0, locked_until=NULL WHERE emp_code=?",
                (emp_code,)
            )
            return {"ok": True, "locked": False, "must_change": bool(row.get("must_change"))}
        else:
            # 실패: 카운트 증가, 5회 이상이면 30분 잠금
            new_count = (row.get("failed_count") or 0) + 1
            lock_until = None
            if new_count >= 5:
                from datetime import timedelta
                lock_until = (datetime.now(_KST) + timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M:%S")
            conn.execute(
                "UPDATE portal_user_passwords SET failed_count=?, locked_until=? WHERE emp_code=?",
                (new_count, lock_until, emp_code)
            )
            remain = max(0, 5 - new_count)
            if lock_until:
                return {"ok": False, "locked": True, "must_change": False,
                        "reason": f"비밀번호 5회 오류. 30분간 잠금됩니다."}
            return {"ok": False, "locked": False, "must_change": False,
                    "reason": f"비밀번호가 올바르지 않습니다. ({remain}회 남음)"}


def has_password(emp_code: str) -> bool:
    """비밀번호 설정 여부 확인."""
    init_db()
    with _connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM portal_user_passwords WHERE emp_code = ?", (emp_code,)
        ).fetchone()
        return row is not None


def reset_password(emp_code: str) -> str:
    """관리자용 비밀번호 초기화. 임시 비밀번호(사번 뒤 4자리) 반환."""
    temp_pw = emp_code[-4:]
    set_password(emp_code, temp_pw, must_change=True)
    return temp_pw


def unlock_account(emp_code: str) -> None:
    """계정 잠금 해제."""
    init_db()
    with _connect() as conn:
        conn.execute(
            "UPDATE portal_user_passwords SET failed_count=0, locked_until=NULL WHERE emp_code=?",
            (emp_code,)
        )


def list_password_status(emp_codes: list[str]) -> dict[str, dict]:
    """emp_code 목록의 비밀번호 상태 반환."""
    init_db()
    with _connect() as conn:
        rows = conn.execute(
            f"SELECT * FROM portal_user_passwords WHERE emp_code IN ({','.join('?'*len(emp_codes))})",
            emp_codes
        ).fetchall()
    result = {}
    now = _now()
    for r in rows:
        r = dict(r)
        r["is_locked"] = bool(r.get("locked_until") and r["locked_until"] > now)
        result[r["emp_code"]] = r
    return result


def list_login_users() -> list[dict]:
    """로그인 성공 기록이 있는 고유 사용자 목록 반환."""
    init_db()
    with _connect() as conn:
        rows = conn.execute(
            """SELECT emp_code, emp_name, team,
               MAX(created_at) AS last_login, COUNT(*) AS login_count
               FROM portal_login_logs WHERE success=1
               GROUP BY emp_code ORDER BY last_login DESC"""
        ).fetchall()
        return [dict(r) for r in rows]


def list_login_logs(limit: int = 200) -> list[dict]:
    """최근 로그인 기록 반환."""
    init_db()
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM portal_login_logs ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]


# ── VOC 게시판 ──────────────────────────────────────────────────────────────

def create_voc_post(*, emp_code: str, emp_name: str, team: str,
                    category: str, title: str, content: str) -> int:
    """VOC 글 작성. 생성된 id 반환."""
    init_db()
    with _connect() as conn:
        cur = conn.execute(
            """INSERT INTO voc_posts (emp_code, emp_name, team, category, title, content, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (emp_code, emp_name, team, category, title, content, _now()),
        )
        return cur.lastrowid


def list_voc_posts(category: str = "", page: int = 1, per_page: int = 20) -> tuple[list[dict], int]:
    """VOC 글 목록 + 댓글수. (rows, total) 반환."""
    init_db()
    conds, params = [], []
    if category:
        conds.append("p.category = ?"); params.append(category)
    where = ("WHERE " + " AND ".join(conds)) if conds else ""
    with _connect() as conn:
        total = conn.execute(
            f"SELECT COUNT(*) FROM voc_posts p {where}", params
        ).fetchone()[0]
        offset = (page - 1) * per_page
        rows = conn.execute(
            f"""SELECT p.*, COUNT(c.id) AS comment_count
                FROM voc_posts p
                LEFT JOIN voc_comments c ON c.post_id = p.id
                {where}
                GROUP BY p.id
                ORDER BY p.created_at DESC
                LIMIT ? OFFSET ?""",
            params + [per_page, offset],
        ).fetchall()
    return [dict(r) for r in rows], total


def get_voc_post(post_id: int) -> dict | None:
    """단건 조회."""
    init_db()
    with _connect() as conn:
        row = conn.execute("SELECT * FROM voc_posts WHERE id = ?", (post_id,)).fetchone()
        return dict(row) if row else None


def delete_voc_post(post_id: int, emp_code: str, is_admin: bool = False) -> bool:
    """본인 또는 관리자만 삭제. 성공 시 True."""
    init_db()
    with _connect() as conn:
        if is_admin:
            conn.execute("DELETE FROM voc_comments WHERE post_id = ?", (post_id,))
            conn.execute("DELETE FROM voc_posts WHERE id = ?", (post_id,))
        else:
            row = conn.execute(
                "SELECT emp_code FROM voc_posts WHERE id = ?", (post_id,)
            ).fetchone()
            if not row or row["emp_code"] != emp_code:
                return False
            conn.execute("DELETE FROM voc_comments WHERE post_id = ?", (post_id,))
            conn.execute("DELETE FROM voc_posts WHERE id = ?", (post_id,))
    return True


def list_voc_comments(post_id: int) -> list[dict]:
    init_db()
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM voc_comments WHERE post_id = ? ORDER BY created_at",
            (post_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def create_voc_comment(*, post_id: int, emp_code: str, emp_name: str,
                        team: str, content: str) -> int:
    init_db()
    with _connect() as conn:
        cur = conn.execute(
            """INSERT INTO voc_comments (post_id, emp_code, emp_name, team, content, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (post_id, emp_code, emp_name, team, content, _now()),
        )
        return cur.lastrowid


def delete_voc_comment(comment_id: int, emp_code: str, is_admin: bool = False) -> bool:
    init_db()
    with _connect() as conn:
        if is_admin:
            conn.execute("DELETE FROM voc_comments WHERE id = ?", (comment_id,))
        else:
            row = conn.execute(
                "SELECT emp_code FROM voc_comments WHERE id = ?", (comment_id,)
            ).fetchone()
            if not row or row["emp_code"] != emp_code:
                return False
            conn.execute("DELETE FROM voc_comments WHERE id = ?", (comment_id,))
    return True


# ── 모듈 로드 시 DB 초기화 ──────────────────────────────────────────────────
init_db()

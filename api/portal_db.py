"""영업사원 포털 활동 로그 저장소."""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from datetime import datetime

DATA_DIR = Path(os.getenv("CHATBOT_DATA_DIR", r"E:\data\chatbot"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "portal_activity.db"


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


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


def list_login_logs(limit: int = 200) -> list[dict]:
    init_db()
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM portal_login_logs ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]


def list_dm_logs(limit: int = 200) -> list[dict]:
    init_db()
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM dm_send_logs ORDER BY created_at DESC LIMIT ?",
            (limit,),
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


init_db()

"""SQLite 공통 설정 · 일일 백업.

배경 (2026-10-21)
  Azure App Service 의 /home 은 SMB 네트워크 공유 스토리지다.
  SQLite WAL 모드는 공유 메모리(-shm)를 쓰므로 네트워크 파일시스템에서 안전하지 않고,
  배포·재시작 시 신·구 컨테이너가 약 20초 겹치며 같은 DB 에 붙는다.
  → portal_activity.db 의 BT 인덱스가 손상("database disk image is malformed")된 사례 발생.

대응
  1) 저널 모드를 DELETE(롤백 저널, 파일 잠금 기반)로 전환 — SQLITE_JOURNAL_MODE 로 변경 가능
  2) busy_timeout 10초 — 잠금 대기
  3) 일일 백업(SQLite online backup API) + 백업본 quick_check, 7일 보관
"""
from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

JOURNAL_MODE = os.getenv("SQLITE_JOURNAL_MODE", "DELETE").upper()
BUSY_TIMEOUT_MS = int(os.getenv("SQLITE_BUSY_TIMEOUT_MS", "10000"))
BACKUP_KEEP_DAYS = int(os.getenv("SQLITE_BACKUP_KEEP_DAYS", "7"))

_mode_ok: set[str] = set()
_mode_lock = threading.Lock()


def configure(conn: sqlite3.Connection, path: str | os.PathLike) -> sqlite3.Connection:
    """연결 직후 호출. 저널 모드 전환은 경로별로 성공할 때까지만 시도한다.
    (WAL → DELETE 전환은 다른 연결이 열려 있으면 실패할 수 있으므로 다음 연결에서 재시도)"""
    conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    key = str(path)
    if key in _mode_ok:
        return conn
    with _mode_lock:
        if key in _mode_ok:
            return conn
        try:
            mode = str(conn.execute(f"PRAGMA journal_mode={JOURNAL_MODE}").fetchone()[0]).upper()
            if mode == JOURNAL_MODE:
                _mode_ok.add(key)
                logger.info(f"[sqlite] {Path(key).name} journal_mode={mode}")
            else:
                logger.warning(f"[sqlite] {Path(key).name} journal_mode 전환 보류 (현재 {mode})")
        except sqlite3.DatabaseError as e:
            logger.warning(f"[sqlite] {Path(key).name} journal_mode 전환 실패: {e}")
    return conn


# ── 일일 백업 ───────────────────────────────────────────────────────────────

def backup_once(db_path: Path, backup_dir: Path, tag: str | None = None) -> dict:
    """온라인 백업 → 임시파일 → quick_check → 원자적 교체. 같은 날짜 백업이 있으면 건너뜀."""
    db_path, backup_dir = Path(db_path), Path(backup_dir)
    if not db_path.exists():
        return {"db": db_path.name, "skipped": "missing"}
    backup_dir.mkdir(parents=True, exist_ok=True)
    tag = tag or datetime.now().strftime("%Y%m%d")
    dst = backup_dir / f"{db_path.stem}_{tag}.db"
    if dst.exists():
        return {"db": db_path.name, "skipped": "exists", "file": dst.name}
    tmp = backup_dir / f".{dst.name}.{os.getpid()}.tmp"
    t0 = time.time()
    src = sqlite3.connect(str(db_path), timeout=BUSY_TIMEOUT_MS / 1000)
    try:
        out = sqlite3.connect(str(tmp))
        try:
            src.backup(out, pages=256, sleep=0.05)
            check = [r[0] for r in out.execute("PRAGMA quick_check(10)").fetchall()]
        finally:
            out.close()
    finally:
        src.close()
    os.replace(tmp, dst)
    ok = check == ["ok"]
    res = {"db": db_path.name, "file": dst.name, "ok": ok, "check": check[:5],
           "size": dst.stat().st_size, "sec": round(time.time() - t0, 1)}
    (logger.info if ok else logger.error)(f"[sqlite-backup] {res}")
    return res


def prune(backup_dir: Path, keep_days: int = BACKUP_KEEP_DAYS) -> int:
    cutoff = time.time() - keep_days * 86400
    n = 0
    for f in Path(backup_dir).glob("*.db"):
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink()
                n += 1
        except OSError:
            pass
    for f in Path(backup_dir).glob(".*.tmp"):
        try:
            if f.stat().st_mtime < time.time() - 3600:
                f.unlink()
        except OSError:
            pass
    return n


_started = False


def start_backup_scheduler(db_paths: list[Path], backup_dir: Path,
                           hour: int = 4, first_delay: int = 300) -> None:
    """매일 hour 시(서버 로컬)에 백업. 기동 후 first_delay 초 뒤 당일 백업이 없으면 1회 수행."""
    global _started
    if _started or os.getenv("SQLITE_BACKUP", "1") != "1":
        return
    _started = True

    def _run():
        for p in db_paths:
            try:
                backup_once(p, backup_dir)
            except Exception as e:  # noqa: BLE001
                logger.error(f"[sqlite-backup] {Path(p).name} 실패: {e}")
        try:
            prune(backup_dir)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[sqlite-backup] 정리 실패: {e}")

    def _loop():
        time.sleep(first_delay)
        while True:
            _run()
            now = datetime.now()
            nxt = now.replace(hour=hour, minute=0, second=0, microsecond=0)
            if nxt <= now:
                nxt += timedelta(days=1)
            time.sleep(max(60, (nxt - now).total_seconds()))

    threading.Thread(target=_loop, daemon=True, name="sqlite-backup").start()
    logger.info(f"[sqlite-backup] 스케줄러 시작 → {backup_dir} (매일 {hour:02d}시, {BACKUP_KEEP_DAYS}일 보관)")

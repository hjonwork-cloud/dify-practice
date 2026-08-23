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
            CREATE TABLE IF NOT EXISTS voc_likes (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                target_type TEXT NOT NULL,
                target_id   INTEGER NOT NULL,
                emp_code    TEXT NOT NULL,
                created_at  TEXT NOT NULL,
                UNIQUE(target_type, target_id, emp_code)
            );
            CREATE INDEX IF NOT EXISTS idx_voc_likes ON voc_likes(target_type, target_id);
        """)
        # 공지 테이블 마이그레이션
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS announcements (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                badge       TEXT NOT NULL DEFAULT '공지',
                title       TEXT NOT NULL,
                content     TEXT NOT NULL,
                is_active   INTEGER NOT NULL DEFAULT 1,
                pinned      INTEGER NOT NULL DEFAULT 0,
                created_by  TEXT,
                created_at  TEXT NOT NULL,
                updated_at  TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_ann_active ON announcements(is_active, pinned DESC, created_at DESC);
        """)
        # 공지사항 게시판 (notice_posts) 마이그레이션
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS notice_posts (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                badge        TEXT NOT NULL DEFAULT '공지',
                title        TEXT NOT NULL,
                content      TEXT NOT NULL,
                image_data   TEXT,
                image_mime   TEXT,
                popup_start  TEXT,
                popup_end    TEXT,
                is_active    INTEGER NOT NULL DEFAULT 1,
                pinned       INTEGER NOT NULL DEFAULT 0,
                created_by   TEXT,
                created_at   TEXT NOT NULL,
                updated_at   TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_notice_active ON notice_posts(is_active, pinned DESC, created_at DESC);
            CREATE TABLE IF NOT EXISTS notice_comments (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                post_id    INTEGER NOT NULL,
                emp_code   TEXT NOT NULL,
                emp_name   TEXT,
                team       TEXT,
                content    TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_notice_comments_post ON notice_comments(post_id, created_at);
        """)
        # ── 가격 모니터링 테이블 ────────────────────────────────────────────
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS price_map_product_link (
                mapping_id          INTEGER PRIMARY KEY AUTOINCREMENT,
                our_product_code    TEXT NOT NULL,
                plant               TEXT NOT NULL DEFAULT '4120',
                product_key         TEXT NOT NULL,
                platform            TEXT NOT NULL,
                platform_seller_id  TEXT,
                platform_product_id TEXT,
                product_name        TEXT,
                seller_name         TEXT,
                tag                 TEXT NOT NULL DEFAULT 'normal',
                multiplier          REAL NOT NULL DEFAULT 1.0,
                created_by          TEXT,
                created_at          TEXT DEFAULT (datetime('now','localtime')),
                is_active           INTEGER DEFAULT 1
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_price_map_unique
                ON price_map_product_link(our_product_code, plant, product_key) WHERE is_active=1;
            CREATE INDEX IF NOT EXISTS idx_price_map_code
                ON price_map_product_link(our_product_code, plant);

            CREATE TABLE IF NOT EXISTS price_map_change_request (
                request_id          INTEGER PRIMARY KEY AUTOINCREMENT,
                our_product_code    TEXT NOT NULL,
                plant               TEXT NOT NULL DEFAULT '4120',
                request_type        TEXT NOT NULL,
                delete_product_keys TEXT,
                add_product_keys    TEXT,
                reason              TEXT NOT NULL,
                requested_by        TEXT,
                requested_by_name   TEXT,
                requested_at        TEXT DEFAULT (datetime('now','localtime')),
                status              TEXT DEFAULT 'PENDING',
                reviewed_by         TEXT,
                reviewed_at         TEXT,
                admin_memo          TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_price_cr_status
                ON price_map_change_request(status, requested_at DESC);

            CREATE TABLE IF NOT EXISTS price_baemin_sellers (
                seller_id    INTEGER PRIMARY KEY,
                seller_name  TEXT,
                region       TEXT,
                is_active    INTEGER DEFAULT 1,
                updated_at   TEXT DEFAULT (datetime('now','localtime'))
            );
            INSERT OR IGNORE INTO price_baemin_sellers(seller_id,seller_name,region,is_active) VALUES
                (907,  '이너피스',      '서울/경기/인천', 1),
                (2090, '그로우식자재',  '서울/경기/인천', 1),
                (2089, '스마일푸드',    '서울/경기/인천', 1),
                (1384, '다봄푸드',      '서울/경기/인천', 1),
                (1774, '온국민신선몰',  '서울/경기/인천', 1),
                (2057, '세현F&B',       '서울/경기일부',  1),
                (2006, '파라도',        '서울/경기일부',  1),
                (2039, '현대그린푸드',  '전국',           1),
                (2005, '얌피쉬',        '서울/경기/인천', 1);

            -- 식봄(foodspring) 셀러 관리 테이블
            -- delivery_type: 'direct'(직배송) | 'singsing'(싱싱배송) | ''(미확인)
            CREATE TABLE IF NOT EXISTS price_foodspring_sellers (
                seller_id    INTEGER PRIMARY KEY,
                seller_name  TEXT,
                delivery_type TEXT DEFAULT '',
                is_active    INTEGER DEFAULT 1,
                updated_at   TEXT DEFAULT (datetime('now','localtime'))
            );
            -- 식봄.md 기준 113개 셀러 초기 데이터 (배송타입은 크롤 시 자동 감지)
            INSERT OR IGNORE INTO price_foodspring_sellers(seller_id,seller_name,delivery_type,is_active) VALUES
                -- 싱싱배송 (AggregateDelivery)
                (1517,'CJ프레시웨이','singsing',1),
                (3105,'우주식품 디씨오피','singsing',1),
                (4709,'에드벨류','singsing',1),
                (4777,'이너피스(싱싱배송)','singsing',1),
                (3738,'사조푸디스트','singsing',1),
                (3825,'푸딩팩토리(안주)','singsing',1),
                (4861,'대상주식회사(싱싱배송)','singsing',1),
                (4921,'쉐프스푸드','singsing',1),
                (4956,'식품팜','singsing',1),
                (3638,'우진식품','singsing',1),
                (2575,'팜파스','singsing',1),
                (3215,'쉐프의정원','singsing',1),
                (4141,'예주나라','singsing',1),
                (4568,'에이치제이팩토리','singsing',1),
                (3672,'면사랑','singsing',1),
                (4646,'더바모스','singsing',1),
                (4744,'푸드올마켓','singsing',1),
                (4375,'지이디통상','singsing',1),
                (4484,'영인코퍼레이션','singsing',1),
                (4603,'감성팩','singsing',1),
                (3241,'야채는 농부네','singsing',1),
                (4572,'영남코프레이션','singsing',1),
                (4883,'더고기','singsing',1),
                (4787,'효성푸드','singsing',1),
                (3994,'푸딩팩토리(튀김)','singsing',1),
                (4926,'싱싱 플러스','singsing',1),
                (4673,'대성푸드시스템','singsing',1),
                (4782,'에이치푸드서플라이','singsing',1),
                (3899,'주흥상사','singsing',1),
                (3643,'서진그룹','singsing',1),
                (4677,'와이즈온','singsing',1),
                (4448,'엠제이푸드','singsing',1),
                (4123,'누리로지스','singsing',1),
                (5044,'사조대림','singsing',1),
                (4423,'코우인터내셔널','singsing',1),
                (4907,'소반푸드','singsing',1),
                (4212,'푸드렐라','singsing',1),
                (3102,'우성물산','singsing',1),
                (3603,'아시안푸드스타','singsing',1),
                (4645,'코주부어묵','singsing',1),
                (4636,'다들림푸드','singsing',1),
                (4785,'치즈트리','singsing',1),
                (4025,'돌우물','singsing',1),
                (4853,'수플린','singsing',1),
                (4833,'계식이푸드','singsing',1),
                (4281,'토자연','singsing',1),
                (4776,'푸르온','singsing',1),
                (4946,'두보식품','singsing',1),
                (4422,'더블제이푸드','singsing',1),
                (4305,'오름(싱싱배송)','singsing',1),
                (5070,'벌크푸드','singsing',1),
                (4802,'조인','singsing',1),
                (4791,'청계원','singsing',1),
                -- 직배송 (DirectDelivery)
                (5081,'푸드팡-수도권','direct',1),
                (2716,'현대그린푸드','direct',1),
                (2626,'식자재119','direct',1),
                (867,'온국민 신선몰','direct',1),
                (4069,'디어푸드','direct',1),
                (3038,'푸드레인','direct',1),
                (2455,'세현F&B','direct',1),
                (1388,'다봄푸드','direct',1),
                (3828,'케이에프피(강남)','direct',1),
                (3359,'베이킹몬','direct',1),
                (3680,'내가그린푸드','direct',1),
                (2019,'대상(수도권)','direct',1),
                (1702,'써니플러스','direct',1),
                (560,'푸드코리아시스템','direct',1),
                (3183,'두리식자재','direct',1),
                (3610,'얌피쉬홀세일','direct',1),
                (1862,'서일코퍼레이션','direct',1),
                (693,'이너피스(직배송)','direct',1),
                (4533,'그로우식자재','direct',1),
                (4473,'윤진유통','direct',1),
                (3452,'굿모닝식자재','direct',1),
                (2711,'메종베르','direct',1),
                (4431,'착한푸드','direct',1),
                (4195,'마당몰','direct',1),
                (1104,'신선한식탁','direct',1),
                (3554,'스마일푸드','direct',1),
                (1838,'토박이유통','direct',1),
                (3451,'싱싱채소 그린팜','direct',1),
                (4067,'푸드베테랑','direct',1),
                (4034,'진프레시','direct',1),
                (4328,'푸르메 씨앤푸드','direct',1),
                (2203,'무진물산','direct',1),
                (2587,'주설유통','direct',1),
                (3646,'미트프렌즈','direct',1),
                (2897,'미트클럽','direct',1),
                (4312,'한양유통','direct',1),
                (3455,'공주유통','direct',1),
                (3827,'봄봄상회','direct',1),
                (3841,'바름푸드','direct',1),
                (2481,'블루푸드','direct',1),
                (4071,'오름푸드시스템','direct',1),
                (3172,'원흥축산','direct',1),
                (2984,'한올미트','direct',1),
                (2488,'디보트코리아','direct',1),
                (920,'서울식자재','direct',1),
                (3652,'더고기(신선직배송)','direct',1),
                (1186,'OTTO 영흥식품','direct',1),
                (2901,'도깨비가게','direct',1),
                (3314,'진우푸드','direct',1),
                (2198,'엠에이치컴퍼니','direct',1),
                (2729,'에이젯유통','direct',1),
                (4656,'세현F&B_정육','direct',1),
                (3984,'BEST RICE 31','direct',1),
                (3271,'제이엘유지','direct',1),
                (4004,'현대비즈','direct',1),
                (2380,'혜성프로비젼','direct',1),
                (1556,'고구마켓','direct',1),
                (1913,'가온 신선팜','direct',1),
                (1725,'에그랑에프엔비','direct',1),
                (3384,'하늘농원','direct',1);
        """)
        # price_map_product_link: tag/multiplier 컬럼 마이그레이션
        for col, typedef in [
            ("tag",        "TEXT NOT NULL DEFAULT 'normal'"),
            ("multiplier", "REAL NOT NULL DEFAULT 1.0"),
        ]:
            try:
                conn.execute(f"ALTER TABLE price_map_product_link ADD COLUMN {col} {typedef}")
            except Exception:
                pass

        # announcements 데이터 → notice_posts 자동 이관 (중복 방지)
        try:
            migrated = conn.execute("SELECT COUNT(*) FROM notice_posts").fetchone()[0]
            if migrated == 0:
                conn.execute("""
                    INSERT INTO notice_posts
                        (badge,title,content,image_data,image_mime,popup_start,popup_end,
                         is_active,pinned,created_by,created_at,updated_at)
                    SELECT badge,title,content,NULL,NULL,NULL,NULL,
                           is_active,pinned,created_by,created_at,updated_at
                    FROM announcements
                """)
        except Exception:
            pass


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
    action_ym: str = "",
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
             action_ym, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (emp_code, emp_name, team, brand_code, brand_name, customer_code, customer_name,
             action_type, product_names, message,
             price_items_json, sap_saved_count, sap_result_json,
             action_ym or None, status, _now()),
        )
        conn.execute(
            """INSERT INTO promotion_action_logs
            (emp_code, emp_name, brand_name, customer_code, customer_name, action, detail, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (emp_code, emp_name, brand_name, customer_code, customer_name, action_type, product_names, _now()),
        )


def list_login_logs(limit: int = 200):
    init_db()
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM portal_login_logs ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]


def list_dm_logs(limit: int = 200, emp_code: str | None = None,
                 brand_code: str | None = None, action_ym: str | None = None):
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


def list_action_logs(limit: int = 200):
    init_db()
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM promotion_action_logs ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]


def delete_dm_logs_by_action(emp_code: str, customer_code: str, brand_code: str, action_ym: str) -> int:
    """액션 단위(customer+brand) DM 발송이력 일괄 삭제.
    action_ym이 NULL인 레거시 레코드도 함께 삭제하기 위해
    `action_ym IS NULL OR action_ym = ?` 조건 사용. 삭제된 행 수 반환."""
    init_db()
    with _connect() as conn:
        if emp_code:
            cur = conn.execute(
                "DELETE FROM dm_send_logs WHERE emp_code=? AND customer_code=? AND brand_code=?"
                " AND (action_ym IS NULL OR action_ym=?)",
                (emp_code, customer_code, brand_code, action_ym),
            )
        else:
            cur = conn.execute(
                "DELETE FROM dm_send_logs WHERE customer_code=? AND brand_code=?"
                " AND (action_ym IS NULL OR action_ym=?)",
                (customer_code, brand_code, action_ym),
            )
        return cur.rowcount


def delete_dm_log_by_id(log_id: int) -> int:
    """id 기반 DM 발송이력 단건 삭제. 삭제된 행 수 반환."""
    init_db()
    with _connect() as conn:
        cur = conn.execute("DELETE FROM dm_send_logs WHERE id=?", (log_id,))
        return cur.rowcount


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


def list_password_status(emp_codes):
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


def list_login_users():
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


def list_login_logs(limit: int = 200):
    """최근 로그인 기록 반환."""
    init_db()
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM portal_login_logs ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]


# ── 공지사항 게시판 (notice_posts) ──────────────────────────────────────────

def list_notice_posts(active_only: bool = False, page: int = 1, per_page: int = 20) -> tuple[list[dict], int]:
    init_db()
    where = "WHERE is_active = 1" if active_only else ""
    with _connect() as conn:
        total = conn.execute(f"SELECT COUNT(*) FROM notice_posts {where}").fetchone()[0]
        offset = (page - 1) * per_page
        rows = conn.execute(
            f"""SELECT p.*, COUNT(c.id) AS comment_count
                FROM notice_posts p
                LEFT JOIN notice_comments c ON c.post_id = p.id
                {where}
                GROUP BY p.id
                ORDER BY p.pinned DESC, p.created_at DESC
                LIMIT ? OFFSET ?""",
            (per_page, offset),
        ).fetchall()
    return [dict(r) for r in rows], total


def get_notice_post(post_id: int) -> dict | None:
    init_db()
    with _connect() as conn:
        row = conn.execute("SELECT * FROM notice_posts WHERE id=?", (post_id,)).fetchone()
        return dict(row) if row else None


def list_notice_active_for_popup():
    """팝업 기간이 명시적으로 설정된 공지 중 현재 날짜가 기간 내인 목록."""
    init_db()
    today = _now()[:10]
    with _connect() as conn:
        rows = conn.execute(
            """SELECT * FROM notice_posts
               WHERE is_active = 1
                 AND popup_start IS NOT NULL AND popup_start != ''
                 AND popup_end   IS NOT NULL AND popup_end   != ''
                 AND popup_start <= ?
                 AND popup_end   >= ?
               ORDER BY pinned DESC, created_at DESC""",
            (today, today),
        ).fetchall()
    return [dict(r) for r in rows]


def create_notice_post(*, badge: str, title: str, content: str,
                        image_data: str = "", image_mime: str = "",
                        popup_start: str = "", popup_end: str = "",
                        pinned: bool = False, created_by: str = "") -> int:
    init_db()
    now = _now()
    with _connect() as conn:
        cur = conn.execute(
            """INSERT INTO notice_posts
               (badge,title,content,image_data,image_mime,popup_start,popup_end,
                is_active,pinned,created_by,created_at,updated_at)
               VALUES (?,?,?,?,?,?,?,1,?,?,?,?)""",
            (badge, title, content,
             image_data or None, image_mime or None,
             popup_start or None, popup_end or None,
             1 if pinned else 0, created_by, now, now),
        )
        return cur.lastrowid


def update_notice_post(post_id: int, *, badge: str, title: str, content: str,
                        image_data: str = "", image_mime: str = "",
                        popup_start: str = "", popup_end: str = "",
                        pinned: bool = False) -> None:
    init_db()
    with _connect() as conn:
        conn.execute(
            """UPDATE notice_posts
               SET badge=?,title=?,content=?,image_data=?,image_mime=?,
                   popup_start=?,popup_end=?,pinned=?,updated_at=?
               WHERE id=?""",
            (badge, title, content,
             image_data or None, image_mime or None,
             popup_start or None, popup_end or None,
             1 if pinned else 0, _now(), post_id),
        )


def toggle_notice_post(post_id: int) -> bool:
    init_db()
    with _connect() as conn:
        row = conn.execute("SELECT is_active FROM notice_posts WHERE id=?", (post_id,)).fetchone()
        if not row:
            return False
        new_state = 0 if row["is_active"] else 1
        conn.execute("UPDATE notice_posts SET is_active=?,updated_at=? WHERE id=?",
                     (new_state, _now(), post_id))
        return bool(new_state)


def delete_notice_post(post_id: int) -> None:
    init_db()
    with _connect() as conn:
        conn.execute("DELETE FROM notice_comments WHERE post_id=?", (post_id,))
        conn.execute("DELETE FROM notice_posts WHERE id=?", (post_id,))


def list_notice_comments(post_id: int):
    init_db()
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM notice_comments WHERE post_id=? ORDER BY created_at",
            (post_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def create_notice_comment(*, post_id: int, emp_code: str,
                           emp_name: str, team: str, content: str) -> int:
    init_db()
    with _connect() as conn:
        cur = conn.execute(
            """INSERT INTO notice_comments (post_id,emp_code,emp_name,team,content,created_at)
               VALUES (?,?,?,?,?,?)""",
            (post_id, emp_code, emp_name, team, content, _now()),
        )
        return cur.lastrowid


def delete_notice_comment(comment_id: int, emp_code: str, is_admin: bool = False) -> bool:
    init_db()
    with _connect() as conn:
        if is_admin:
            conn.execute("DELETE FROM notice_comments WHERE id=?", (comment_id,))
        else:
            row = conn.execute("SELECT emp_code FROM notice_comments WHERE id=?", (comment_id,)).fetchone()
            if not row or row["emp_code"] != emp_code:
                return False
            conn.execute("DELETE FROM notice_comments WHERE id=?", (comment_id,))
    return True


# ── 공지 관리 (announcements — 하위호환 유지) ──────────────────────────────

def list_announcements(active_only: bool = False):
    init_db()
    where = "WHERE is_active = 1" if active_only else ""
    with _connect() as conn:
        rows = conn.execute(
            f"SELECT * FROM announcements {where} ORDER BY pinned DESC, created_at DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def create_announcement(*, badge: str, title: str, content: str,
                         pinned: bool = False, created_by: str = "") -> int:
    init_db()
    now = _now()
    with _connect() as conn:
        cur = conn.execute(
            """INSERT INTO announcements (badge, title, content, is_active, pinned, created_by, created_at, updated_at)
               VALUES (?, ?, ?, 1, ?, ?, ?, ?)""",
            (badge, title, content, 1 if pinned else 0, created_by, now, now),
        )
        return cur.lastrowid


def update_announcement(ann_id: int, *, badge: str, title: str, content: str, pinned: bool) -> None:
    init_db()
    with _connect() as conn:
        conn.execute(
            """UPDATE announcements SET badge=?, title=?, content=?, pinned=?, updated_at=?
               WHERE id=?""",
            (badge, title, content, 1 if pinned else 0, _now(), ann_id),
        )


def toggle_announcement(ann_id: int) -> bool:
    """활성/비활성 토글. 현재 상태 반환."""
    init_db()
    with _connect() as conn:
        row = conn.execute("SELECT is_active FROM announcements WHERE id=?", (ann_id,)).fetchone()
        if not row:
            return False
        new_state = 0 if row["is_active"] else 1
        conn.execute("UPDATE announcements SET is_active=?, updated_at=? WHERE id=?",
                     (new_state, _now(), ann_id))
        return bool(new_state)


def delete_announcement(ann_id: int) -> None:
    init_db()
    with _connect() as conn:
        conn.execute("DELETE FROM announcements WHERE id=?", (ann_id,))


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


def list_voc_comments(post_id: int):
    init_db()
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM voc_comments WHERE post_id = ? ORDER BY created_at",
            (post_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def toggle_voc_like(target_type: str, target_id: int, emp_code: str):
    """좋아요 토글. (is_liked_now, total_count) 반환."""
    init_db()
    with _connect() as conn:
        existing = conn.execute(
            "SELECT id FROM voc_likes WHERE target_type=? AND target_id=? AND emp_code=?",
            (target_type, target_id, emp_code),
        ).fetchone()
        if existing:
            conn.execute(
                "DELETE FROM voc_likes WHERE target_type=? AND target_id=? AND emp_code=?",
                (target_type, target_id, emp_code),
            )
            liked = False
        else:
            conn.execute(
                "INSERT INTO voc_likes(target_type,target_id,emp_code,created_at) VALUES(?,?,?,?)",
                (target_type, target_id, emp_code, _now()),
            )
            liked = True
        count = conn.execute(
            "SELECT COUNT(*) FROM voc_likes WHERE target_type=? AND target_id=?",
            (target_type, target_id),
        ).fetchone()[0]
    return liked, count


def get_voc_likes(target_type: str, target_ids, emp_code: str) -> dict:
    """다수 target의 (count, my_like) 정보 반환. {id: {count, liked}} 형태."""
    if not target_ids:
        return {}
    init_db()
    placeholders = ",".join(["?"] * len(target_ids))
    with _connect() as conn:
        rows = conn.execute(
            f"SELECT target_id, COUNT(*) as cnt FROM voc_likes"
            f" WHERE target_type=? AND target_id IN ({placeholders}) GROUP BY target_id",
            [target_type] + target_ids,
        ).fetchall()
        my_rows = conn.execute(
            f"SELECT target_id FROM voc_likes"
            f" WHERE target_type=? AND target_id IN ({placeholders}) AND emp_code=?",
            [target_type] + target_ids + [emp_code],
        ).fetchall() if emp_code else []
    counts = {r["target_id"]: r["cnt"] for r in rows}
    my_liked = {r["target_id"] for r in my_rows}
    return {tid: {"count": counts.get(tid, 0), "liked": tid in my_liked} for tid in target_ids}


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


# ── 가격 모니터링 DB 함수 ──────────────────────────────────────────────────

def pm_list_mappings(our_product_code: str, plant: str):
    """특정 상품+플랜트의 활성 매핑 목록.
    plant='ALL'로 등록된 공통 매핑도 함께 반환.
    """
    init_db()
    with _connect() as conn:
        rows = conn.execute(
            """SELECT * FROM price_map_product_link
               WHERE our_product_code=? AND (plant=? OR plant='ALL') AND is_active=1
               ORDER BY created_at DESC""",
            (our_product_code, plant),
        ).fetchall()
    return [dict(r) for r in rows]


def pm_list_all_mappings(plant: str):
    """플랜트 기준 전체 활성 매핑 목록.
    plant='ALL'(전체센터)이면 모든 활성 매핑 반환.
    plant='ALL'로 등록된 공통 매핑도 함께 포함.
    """
    init_db()
    with _connect() as conn:
        if plant == "ALL":
            rows = conn.execute(
                """SELECT * FROM price_map_product_link
                   WHERE is_active=1
                   ORDER BY our_product_code, platform""",
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT * FROM price_map_product_link
                   WHERE (plant=? OR plant='ALL') AND is_active=1
                   ORDER BY our_product_code, platform""",
                (plant,),
            ).fetchall()
    return [dict(r) for r in rows]


def pm_add_mapping(our_product_code: str, plant: str, product_key: str,
                   platform: str, platform_seller_id: str, platform_product_id: str,
                   product_name: str, seller_name: str, created_by: str,
                   tag: str = 'normal', multiplier: float = 1.0) -> int:
    """매핑 즉시 추가 (ADD — 승인 불필요). 이미 존재하면 재활성화."""
    tag = tag if tag in ('normal', 'substitute', 'multiple') else 'normal'
    try:
        multiplier = float(multiplier)
        if multiplier <= 0:
            multiplier = 1.0
    except (TypeError, ValueError):
        multiplier = 1.0
    init_db()
    with _connect() as conn:
        existing = conn.execute(
            """SELECT mapping_id FROM price_map_product_link
               WHERE our_product_code=? AND plant=? AND product_key=?""",
            (our_product_code, plant, product_key),
        ).fetchone()
        if existing:
            conn.execute(
                """UPDATE price_map_product_link
                   SET is_active=1, created_by=?, created_at=datetime('now','localtime'),
                       tag=?, multiplier=?
                   WHERE mapping_id=?""",
                (created_by, tag, multiplier, existing["mapping_id"]),
            )
            return existing["mapping_id"]
        cur = conn.execute(
            """INSERT INTO price_map_product_link
               (our_product_code, plant, product_key, platform,
                platform_seller_id, platform_product_id,
                product_name, seller_name, tag, multiplier, created_by)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (our_product_code, plant, product_key, platform,
             platform_seller_id, platform_product_id,
             product_name, seller_name, tag, multiplier, created_by),
        )
        return cur.lastrowid


def pm_deactivate_mapping(mapping_id: int) -> bool:
    """매핑 비활성화 (soft delete)"""
    init_db()
    with _connect() as conn:
        conn.execute(
            "UPDATE price_map_product_link SET is_active=0 WHERE mapping_id=?",
            (mapping_id,),
        )
    return True


def pm_create_change_request(our_product_code: str, plant: str, request_type: str,
                              delete_product_keys: list, add_product_keys: list,
                              reason: str, requested_by: str, requested_by_name: str) -> int:
    """삭제/교체 수정요청 생성"""
    import json
    init_db()
    with _connect() as conn:
        cur = conn.execute(
            """INSERT INTO price_map_change_request
               (our_product_code, plant, request_type,
                delete_product_keys, add_product_keys,
                reason, requested_by, requested_by_name)
               VALUES (?,?,?,?,?,?,?,?)""",
            (our_product_code, plant, request_type,
             json.dumps(delete_product_keys or [], ensure_ascii=False),
             json.dumps(add_product_keys or [], ensure_ascii=False),
             reason, requested_by, requested_by_name),
        )
        return cur.lastrowid


def pm_list_change_requests(status: str | None = None):
    """수정요청 목록"""
    import json
    init_db()
    where = "WHERE status=?" if status else ""
    params = (status,) if status else ()
    with _connect() as conn:
        rows = conn.execute(
            f"SELECT * FROM price_map_change_request {where} ORDER BY requested_at DESC",
            params,
        ).fetchall()
    result = []
    for r in rows:
        d = dict(r)
        try: d["delete_product_keys"] = json.loads(d.get("delete_product_keys") or "[]")
        except Exception: d["delete_product_keys"] = []
        try: d["add_product_keys"] = json.loads(d.get("add_product_keys") or "[]")
        except Exception: d["add_product_keys"] = []
        result.append(d)
    return result


def pm_review_change_request(request_id: int, action: str,
                              reviewed_by: str, admin_memo: str) -> dict | None:
    """수정요청 승인/반려. 승인 시 매핑 테이블 자동 반영."""
    import json
    init_db()
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM price_map_change_request WHERE request_id=? AND status='PENDING'",
            (request_id,),
        ).fetchone()
        if not row:
            return None
        req = dict(row)
        now = _now()
        if action == "APPROVE":
            # 삭제 대상 비활성화 (plant='ALL' 매핑도 함께 처리)
            del_keys = json.loads(req.get("delete_product_keys") or "[]")
            for pk in del_keys:
                conn.execute(
                    """UPDATE price_map_product_link SET is_active=0
                       WHERE our_product_code=? AND product_key=?
                         AND (plant=? OR plant='ALL')""",
                    (req["our_product_code"], pk, req["plant"]),
                )
            # 추가 대상 UPSERT (이미 존재하면 재활성화)
            add_keys_raw = json.loads(req.get("add_product_keys") or "[]")
            for item in add_keys_raw:
                if isinstance(item, dict):
                    pk = item.get("product_key", "")
                    # 기존 레코드 여부
                    existing = conn.execute(
                        "SELECT mapping_id FROM price_map_product_link WHERE our_product_code=? AND plant=? AND product_key=?",
                        (req["our_product_code"], req["plant"], pk),
                    ).fetchone()
                    if existing:
                        conn.execute(
                            "UPDATE price_map_product_link SET is_active=1, created_by=?, created_at=datetime('now','localtime') WHERE mapping_id=?",
                            (reviewed_by, existing["mapping_id"]),
                        )
                    else:
                        conn.execute(
                            """INSERT INTO price_map_product_link
                               (our_product_code, plant, product_key, platform,
                                platform_seller_id, platform_product_id,
                                product_name, seller_name, created_by)
                               VALUES (?,?,?,?,?,?,?,?,?)""",
                            (req["our_product_code"], req["plant"], pk,
                             item.get("platform", ""), item.get("platform_seller_id", ""),
                             item.get("platform_product_id", ""),
                             item.get("product_name", ""), item.get("seller_name", ""),
                             reviewed_by),
                        )
            conn.execute(
                "UPDATE price_map_change_request SET status='APPROVED', reviewed_by=?, reviewed_at=?, admin_memo=? WHERE request_id=?",
                (reviewed_by, now, admin_memo, request_id),
            )
        else:
            conn.execute(
                "UPDATE price_map_change_request SET status='REJECTED', reviewed_by=?, reviewed_at=?, admin_memo=? WHERE request_id=?",
                (reviewed_by, now, admin_memo, request_id),
            )
    return pm_list_change_requests()[0] if False else {"request_id": request_id, "status": action}


def pm_list_baemin_sellers():
    init_db()
    with _connect() as conn:
        rows = conn.execute("SELECT * FROM price_baemin_sellers ORDER BY seller_id").fetchall()
    return [dict(r) for r in rows]


def pm_toggle_seller(seller_id: int, is_active: int) -> None:
    init_db()
    with _connect() as conn:
        conn.execute(
            "UPDATE price_baemin_sellers SET is_active=?, updated_at=datetime('now','localtime') WHERE seller_id=?",
            (is_active, seller_id),
        )


def pm_list_foodspring_sellers(active_only: bool = True):
    """식봄 셀러 목록 반환. delivery_type: 'direct'|'singsing'|''"""
    init_db()
    with _connect() as conn:
        if active_only:
            rows = conn.execute(
                "SELECT * FROM price_foodspring_sellers WHERE is_active=1 ORDER BY seller_id"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM price_foodspring_sellers ORDER BY seller_id"
            ).fetchall()
    return [dict(r) for r in rows]


def pm_update_foodspring_delivery_type(seller_id: int, delivery_type: str, seller_name: str = "") -> None:
    """크롤 시 감지한 배송타입을 업데이트"""
    init_db()
    with _connect() as conn:
        if seller_name:
            conn.execute(
                "UPDATE price_foodspring_sellers SET delivery_type=?, seller_name=?, updated_at=datetime('now','localtime') WHERE seller_id=?",
                (delivery_type, seller_name, seller_id),
            )
        else:
            conn.execute(
                "UPDATE price_foodspring_sellers SET delivery_type=?, updated_at=datetime('now','localtime') WHERE seller_id=?",
                (delivery_type, seller_id),
            )


def pm_toggle_foodspring_seller(seller_id: int, is_active: int) -> None:
    init_db()
    with _connect() as conn:
        conn.execute(
            "UPDATE price_foodspring_sellers SET is_active=?, updated_at=datetime('now','localtime') WHERE seller_id=?",
            (is_active, seller_id),
        )


# ── 모듈 로드 시 DB 초기화 ──────────────────────────────────────────────────
init_db()

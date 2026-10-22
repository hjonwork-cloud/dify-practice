"""이체가관리조회 엑셀 → Databricks 테이블 업로드 (전체 교체).

사용:
  python upload_transfer_price.py "E:\\...\\이체가관리조회1022.XLSX"

테이블: h_hmfo_fsi_dm.gd_rst_ing.pm_transfer_price
  자재·이체가(원) 중심. 사업영역(이체 받는 쪽)·공급사업영역(이체 주는 쪽)·효력기간 원본 보존.
  같은 자재가 여러 행이면 price_monitor에서 사업영역 3000(식재사업) → 효력시작일 최신 순으로 1건 선택.
"""
import os
import sys
import datetime

import pandas as pd

os.environ.setdefault("BT_SCHEDULER", "0")
import main as M  # noqa: E402

T = "h_hmfo_fsi_dm.gd_rst_ing.pm_transfer_price"

DDL = f"""
CREATE OR REPLACE TABLE {T} (
  `자재`            STRING NOT NULL,
  `자재명`          STRING,
  `사업영역`        STRING,
  `사업영역명`      STRING,
  `사용고객`        STRING,
  `사용고객명`      STRING,
  `이체가`          DOUBLE,
  `이체가율`        DOUBLE,
  `공급사업영역`    STRING,
  `공급사업영역명`  STRING,
  `공급고객`        STRING,
  `공급고객명`      STRING,
  `효력시작일`      DATE,
  `효력종료일`      DATE,
  `원본파일`        STRING,
  `업로드일시`      TIMESTAMP
) USING DELTA
COMMENT '이체가관리조회(SAP) 엑셀 업로드본 — price-monitor 구매가 보완(DIPS 실단가 없을 때)'
"""

COLS = {
    "자재": "자재", "자재 명": "자재명", "사업영역": "사업영역", "사업영역명": "사업영역명",
    "사용고객": "사용고객", "사용고객 명": "사용고객명", "이체가": "이체가", "이체가율": "이체가율",
    "사업영역.1": "공급사업영역", "사업영역명.1": "공급사업영역명",
    "공급고객": "공급고객", "공급고객 명": "공급고객명",
    "효력시작일": "효력시작일", "효력종료일": "효력종료일",
}


def _s(v) -> str:
    if v is None or (isinstance(v, float) and pd.isna(v)) or (isinstance(v, str) and not v.strip()):
        return "NULL"
    return "'" + str(v).strip().replace("\\", "\\\\").replace("'", "\\'") + "'"


def _n(v) -> str:
    try:
        f = float(str(v).replace(",", ""))
        return "NULL" if pd.isna(f) else repr(f)
    except Exception:
        return "NULL"


def _d(v) -> str:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return "NULL"
    try:
        return f"DATE'{pd.to_datetime(v).date().isoformat()}'"
    except Exception:
        s = str(v)[:10]
        return f"DATE'{s}'" if s[:4].isdigit() else "NULL"


def _exec(sql: str) -> None:
    conn = M._CONN_POOL.acquire()
    ok = True
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
    except Exception:
        ok = False
        raise
    finally:
        M._CONN_POOL.release(conn, healthy=ok)


def main(path: str) -> None:
    df = pd.read_excel(path, dtype=str)
    miss = [c for c in COLS if c not in df.columns]
    if miss:
        raise SystemExit(f"엑셀 컬럼 누락: {miss}")
    df = df[list(COLS)].rename(columns=COLS)
    df = df[df["자재"].notna() & (df["자재"].str.strip() != "")]
    df["자재"] = df["자재"].str.strip().str.lstrip("0")
    fname = os.path.basename(path)
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    _exec(DDL)
    vals = []
    for r in df.to_dict("records"):
        vals.append("(" + ", ".join([
            _s(r["자재"]), _s(r["자재명"]), _s(r["사업영역"]), _s(r["사업영역명"]),
            _s(r["사용고객"]), _s(r["사용고객명"]), _n(r["이체가"]), _n(r["이체가율"]),
            _s(r["공급사업영역"]), _s(r["공급사업영역명"]), _s(r["공급고객"]), _s(r["공급고객명"]),
            _d(r["효력시작일"]), _d(r["효력종료일"]), _s(fname), f"TIMESTAMP'{now}'",
        ]) + ")")
    for i in range(0, len(vals), 500):
        _exec(f"INSERT INTO {T} VALUES " + ",\n".join(vals[i:i + 500]))
    cnt = M.run_query(f"SELECT COUNT(*) n, COUNT(DISTINCT `자재`) m FROM {T}")[0]
    print(f"업로드 완료: {T} rows={cnt['n']} 자재={cnt['m']} (엑셀 {len(df)}행)")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit("사용: python upload_transfer_price.py <엑셀경로>")
    main(sys.argv[1])

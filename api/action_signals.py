"""
action_signals.py — 세일즈 액션 시그널 감지 엔진
각 함수는 query_fn(sql, raw=True) 콜러블을 받아 독립적으로 동작
"""
from __future__ import annotations
import datetime
from typing import Callable

# ─── 테이블 상수 ────────────────────────────────────────────
T_MAIN    = "h_hmfo_fsi_dm.gd_rst_ing.sales_custmasters_compat_v"
T_MISULGO = "h_hmfo_fsi_dm.gd_rst_ing.unshipped_compat_v"
T_PROFIT  = "h_hmfo.gd_dcube.`00_customers_cm`"

QueryFn = Callable[..., list[dict]]


def _ym_months_ago(n: int) -> str:
    """n개월 전 년월 (YYYYMM)"""
    d = datetime.date.today().replace(day=1)
    for _ in range(n):
        d = (d - datetime.timedelta(days=1)).replace(day=1)
    return d.strftime("%Y%m")


def _this_ym() -> str:
    return datetime.date.today().strftime("%Y%m")


# ────────────────────────────────────────────────────────────
# SIG-01: 단품 이탈 (3개월 이상 주문하다 2개월 연속 0)
# ────────────────────────────────────────────────────────────
def detect_item_churn(brand: str, qfn: QueryFn) -> dict | None:
    """브랜드 내 단품 이탈 감지 (ZB본지점명 단위)"""
    ym3 = _ym_months_ago(3)
    ym2 = _ym_months_ago(2)
    ym1 = _ym_months_ago(1)

    # ── 브랜드 개요: 수익성 테이블 최근 3개월 월별 ──
    overview_rows = qfn(f"""
        SELECT
            DATE_FORMAT(`날짜`, '%Y%m') AS 년월,
            FORMAT(ROUND(SUM(`FI매출액`) * 100 / 1000), 0) AS 매출액_천원,
            CONCAT(ROUND(SUM(`매출총이익`) / NULLIF(SUM(`FI매출액`), 0) * 100, 1), '%') AS 매출총이익율,
            FORMAT(ROUND(SUM(`공헌이익`) * 100 / 1000), 0) AS 공헌이익_천원,
            COUNT(DISTINCT `거래처명`) AS 가맹점수
        FROM {T_PROFIT}
        WHERE `Zc본부명` LIKE '%{brand}%'
          AND DATE_FORMAT(`날짜`, '%Y%m') IN ('{ym3}', '{ym2}', '{ym1}')
        GROUP BY DATE_FORMAT(`날짜`, '%Y%m')
        ORDER BY 년월
    """, raw=True)

    # ── 전체 본지점 수 (기준: ym3~ym1 출현한 고유 ZB본지점명) ──
    total_rows = qfn(f"""
        SELECT COUNT(DISTINCT `ZB본지점명`) AS total
        FROM {T_MAIN}
        WHERE `사업부명`='외식식재사업부'
          AND `ZC본부명` LIKE '%{brand}%'
          AND `년월` IN ('{ym3}','{ym2}','{ym1}')
    """, raw=True)
    total_stores = int(total_rows[0]["total"]) if total_rows else 0

    # ── 이탈 감지 (ZB본지점명 × 품목 단위) ──
    rows = qfn(f"""
        WITH base AS (
            SELECT `ZB본지점명`, `거래처명`, `자재명` AS item,
                   SUM(CASE WHEN `년월`='{ym3}' THEN `매출액`   ELSE 0 END) AS m3,
                   SUM(CASE WHEN `년월`='{ym2}' THEN `매출액`   ELSE 0 END) AS m2,
                   SUM(CASE WHEN `년월`='{ym1}' THEN `매출액`   ELSE 0 END) AS m1,
                   SUM(CASE WHEN `년월`='{ym3}' THEN `매출수량` ELSE 0 END) AS q3
            FROM {T_MAIN}
            WHERE `사업부명`='외식식재사업부'
              AND `ZC본부명` LIKE '%{brand}%'
              AND `년월` IN ('{ym3}','{ym2}','{ym1}')
            GROUP BY `ZB본지점명`, `거래처명`, `자재명`
        )
        SELECT
            `ZB본지점명` AS 본지점명,
            `거래처명`,
            item AS 품목,
            FORMAT(ROUND(m3 * 100 / 1000), 0) AS `매출액(천원)`,
            ROUND(q3) AS 수량
        FROM base
        WHERE m3 > 0 AND m1 = 0
        ORDER BY m3 DESC
        LIMIT 20
    """, raw=True)

    if not rows:
        return None

    # ── 10% 필터: 이탈 관련 본지점이 전체의 10% 미만이면 무시 (TODO: 개발 완료 후 0.10으로 복원)
    churn_stores = len(set(r["본지점명"] for r in rows))
    if total_stores > 0 and churn_stores / total_stores < 0.0:  # 임시: 필터 비활성화
        return None

    items = list({r["품목"] for r in rows})[:5]
    churn_pct = round(churn_stores / total_stores * 100, 1) if total_stores else 0

    return {
        "action_type": "ITEM_CHURN",
        "title": f"단품 이탈 감지 — {len(rows)}건 ({churn_stores}개 본지점)",
        "priority": 1,
        "summary": {
            "기준_기간": f"{ym3} ~ {ym1}",
            "이탈_건수": len(rows),
            "이탈_관련_본지점수": f"{churn_stores}개 ({churn_pct}%)",
            "전체_본지점수": total_stores,
            "주요_이탈_품목": items,
        },
        "detail": {
            "overview": overview_rows,
            "rows": rows,
        },
    }


# ────────────────────────────────────────────────────────────
# SIG-02: 표준 품목 미사용 (브랜드 80%↑이 쓰는데 미주문 가맹 존재)
# ────────────────────────────────────────────────────────────
def detect_standard_item_missing(brand: str, qfn: QueryFn) -> dict | None:
    ym1 = _ym_months_ago(1)
    rows = qfn(f"""
        WITH total_cnt AS (
            SELECT COUNT(DISTINCT `ZA거래처`) AS total
            FROM {T_MAIN}
            WHERE `사업부명`='외식식재사업부'
              AND `ZC본부명` LIKE '%{brand}%'
              AND `년월`='{ym1}'
        ),
        item_usage AS (
            SELECT `자재명`, COUNT(DISTINCT `ZA거래처`) AS user_cnt
            FROM {T_MAIN}
            WHERE `사업부명`='외식식재사업부'
              AND `ZC본부명` LIKE '%{brand}%'
              AND `년월`='{ym1}'
            GROUP BY `자재명`
        ),
        standard_items AS (
            SELECT i.`자재명`, i.user_cnt,
                   t.total,
                   ROUND(i.user_cnt * 100.0 / t.total, 1) AS usage_pct
            FROM item_usage i CROSS JOIN total_cnt t
            WHERE i.user_cnt * 1.0 / t.total >= 0.8
        ),
        non_users AS (
            SELECT si.`자재명`, si.usage_pct,
                   m.`ZA거래처`, m.`거래처명`
            FROM standard_items si
            JOIN {T_MAIN} m ON m.`사업부명`='외식식재사업부'
                AND m.`ZC본부명` LIKE '%{brand}%'
                AND m.`년월`='{ym1}'
            WHERE m.`ZA거래처` NOT IN (
                SELECT DISTINCT `ZA거래처`
                FROM {T_MAIN}
                WHERE `자재명`=si.`자재명`
                  AND `ZC본부명` LIKE '%{brand}%'
                  AND `년월`='{ym1}'
            )
        )
        SELECT `자재명`, usage_pct, COUNT(DISTINCT `ZA거래처`) AS missing_cnt
        FROM non_users
        GROUP BY `자재명`, usage_pct
        ORDER BY missing_cnt DESC
        LIMIT 5
    """, raw=True)
    if not rows:
        return None
    return {
        "action_type": "STD_ITEM_MISSING",
        "title": f"표준 품목 미사용 가맹 존재 — {rows[0]['자재명']} 외 {len(rows)-1}건",
        "priority": 2,
        "summary": {
            "기준_기간": ym1,
            "미사용_품목": [r["자재명"] for r in rows],
            "최대_미사용_가맹수": rows[0]["missing_cnt"] if rows else 0,
        },
        "detail": {"rows": rows},
    }


# ────────────────────────────────────────────────────────────
# SIG-05: 브랜드 미출고 다발
# ────────────────────────────────────────────────────────────
def detect_brand_unshipped(brand: str, qfn: QueryFn) -> dict | None:
    rows = qfn(f"""
        SELECT COUNT(*) AS cnt,
               COUNT(DISTINCT `거래처명`) AS shop_cnt,
               SUM(`미출고수량`) AS total_qty
        FROM {T_MISULGO}
        WHERE `ZC본부명` LIKE '%{brand}%'
    """, raw=True)
    if not rows or not rows[0].get("cnt") or rows[0]["cnt"] == 0:
        return None
    r = rows[0]
    detail = qfn(f"""
        SELECT `거래처명`, `자재명`, `미출고수량`, `주문일자`
        FROM {T_MISULGO}
        WHERE `ZC본부명` LIKE '%{brand}%'
        ORDER BY `주문일자` DESC
        LIMIT 10
    """, raw=True)
    return {
        "action_type": "BRAND_UNSHIPPED",
        "title": f"미출고 다발 — {r['cnt']}건 / {r['shop_cnt']}개 가맹",
        "priority": 1,
        "summary": {
            "미출고_건수": r["cnt"],
            "관련_가맹수": r["shop_cnt"],
            "총_미출고수량": r["total_qty"],
        },
        "detail": {"rows": detail},
    }


# ────────────────────────────────────────────────────────────
# SIG-07: 저마진 지속 (CM률 1% 미만 + 매출 1억↑ + 최근 월)
# ────────────────────────────────────────────────────────────
def detect_low_cm(brand: str, qfn: QueryFn) -> dict | None:
    rows = qfn(f"""
        SELECT `거래처명`, `Zc본부명`,
               SUM(`FI매출액`) AS fi, SUM(`공헌이익`) AS cm
        FROM {T_PROFIT}
        WHERE `Zc본부명` LIKE '%{brand}%'
          AND YEAR(`날짜`) = YEAR(CURRENT_DATE)
          AND MONTH(`날짜`) = MONTH(CURRENT_DATE) - 1
        GROUP BY `거래처명`, `Zc본부명`
        HAVING fi > 100000000 AND cm / fi < 0.01
        ORDER BY fi DESC
        LIMIT 10
    """, raw=True)
    if not rows:
        return None
    return {
        "action_type": "LOW_CM",
        "title": f"저마진 가맹 감지 — {len(rows)}개 거래처 CM률 1% 미만",
        "priority": 1,
        "summary": {
            "해당_가맹수": len(rows),
            "최대_매출_가맹": rows[0]["거래처명"] if rows else "-",
            "평균_CM률": f"{sum(float(r['cm'] or 0)/float(r['fi'] or 1)*100 for r in rows)/len(rows):.1f}%",
        },
        "detail": {"rows": [{
            "거래처명": r["거래처명"],
            "매출_백만": int((float(r["fi"]) if r["fi"] else 0) // 1_000_000),
            "CM률": f"{(float(r['cm']) if r['cm'] else 0)/(float(r['fi']) if r['fi'] else 1)*100:.1f}%",
        } for r in rows]},
    }


# ────────────────────────────────────────────────────────────
# 시그널 실행 레지스트리
# ────────────────────────────────────────────────────────────
SIGNAL_REGISTRY: dict[str, Callable] = {
    "ITEM_CHURN":        detect_item_churn,
    "STD_ITEM_MISSING":  detect_standard_item_missing,
    "BRAND_UNSHIPPED":   detect_brand_unshipped,
    "LOW_CM":            detect_low_cm,
}


def run_all_signals(brand: str, qfn: QueryFn, exclude_types: list[str] = None) -> list[dict]:
    """
    브랜드 대상으로 모든 시그널 감지 실행
    exclude_types: 당일 이미 발송된 타입 제외
    Returns: 감지된 시그널 목록 (priority 오름차순)
    """
    import logging as _logging
    _log = _logging.getLogger("action_signals")
    exclude_types = exclude_types or []
    results = []
    for sig_type, fn in SIGNAL_REGISTRY.items():
        if sig_type in exclude_types:
            _log.info(f"[시그널] {sig_type} 제외(당일전송)")
            continue
        try:
            result = fn(brand, qfn)
            if result:
                result["action_type"] = sig_type
                results.append(result)
                _log.info(f"[시그널] {sig_type} 감지됨: {result.get('title')}")
            else:
                _log.info(f"[시그널] {sig_type} 해당없음")
        except Exception as e:
            _log.warning(f"[시그널] {sig_type} 오류: {e}")
    results.sort(key=lambda x: x.get("priority", 9))
    return results

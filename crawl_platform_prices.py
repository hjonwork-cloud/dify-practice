"""
플랫폼 가격 통합 크롤러 - 배민상회 + 식봄
수집 결과를 Databricks silver.dim_platform_products 테이블에 저장.

실행:
  python crawl_platform_prices.py            # 전체 수집
  python crawl_platform_prices.py --test     # 셀러 1개, 1페이지만 (검증용)
  python crawl_platform_prices.py --baemin   # 배민상회만
  python crawl_platform_prices.py --food     # 식봄만
"""

import sys, os, time, datetime, json, argparse, re
import requests

sys.stdout.reconfigure(encoding="utf-8")

# ── Databricks 연결 설정 ──────────────────────────────────────────────────
_THIS_DIR  = os.path.dirname(os.path.abspath(__file__))
_API_DIR   = os.path.join(_THIS_DIR, "api")
TOKEN_FILE = os.path.join(_API_DIR, ".token_cache")

HOST      = "adb-707807361397497.17.azuredatabricks.net"
HTTP_PATH = "/sql/1.0/warehouses/acc2ec933ffef2d0"

T_SILVER = "h_hmfo_fsi_dm.gd_rst_ing.dim_platform_products"

DDL_CREATE = f"""
CREATE TABLE IF NOT EXISTS {T_SILVER} (
  platform             STRING  NOT NULL,
  platform_seller_id   STRING  NOT NULL,
  platform_seller_name STRING,
  product_key          STRING  NOT NULL,
  product_name         STRING,
  spec                 STRING,
  price_original       DOUBLE,
  price_sale           DOUBLE,
  discount_rate        DOUBLE,
  unit_price_desc      STRING,
  delivery_type        STRING,
  is_free_delivery     BOOLEAN,
  crawl_date           DATE    NOT NULL
)
USING DELTA
PARTITIONED BY (crawl_date)
"""


def _get_token() -> str:
    env_pat = os.getenv("DATABRICKS_TOKEN", "").strip()
    if env_pat:
        return env_pat
    if os.path.exists(TOKEN_FILE):
        with open(TOKEN_FILE) as f:
            t = f.read().strip()
        if t:
            return t
    raise RuntimeError(
        "Databricks 토큰을 찾을 수 없습니다.\n"
        "환경변수 DATABRICKS_TOKEN 을 설정하거나\n"
        f"포털 서버를 먼저 실행해 {TOKEN_FILE} 을 생성하세요."
    )


def _get_conn():
    import databricks.sql as dbsql
    token = os.getenv("DATABRICKS_TOKEN", "").strip()

    # 환경변수에 없으면 토큰 파일에서 시도
    if not token and os.path.exists(TOKEN_FILE):
        with open(TOKEN_FILE) as f:
            token = f.read().strip()

    # 토큰이 있으면 바로 연결 시도
    if token:
        try:
            conn = dbsql.connect(
                server_hostname=HOST,
                http_path=HTTP_PATH,
                access_token=token,
            )
            # 연결 테스트
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
            print("  ✓ PAT 토큰으로 연결 성공")
            return conn
        except Exception as e:
            print(f"  ⚠ PAT 토큰 연결 실패 ({e}), 브라우저 인증 시도...")

    # 브라우저 OAuth 폴백
    print("  브라우저 로그인 팝업이 열립니다...")
    from databricks.sdk import WorkspaceClient
    from databricks.sdk.service.sql import StatementState
    wc = WorkspaceClient(host=f"https://{HOST}", auth_type="external-browser")
    me = wc.current_user.me()
    print(f"  ✓ 로그인: {me.user_name}")
    # SDK에서 토큰 추출
    new_token = None
    try:
        creds = wc.config.authenticate()
        new_token = creds.get("Authorization", "").replace("Bearer ", "")
    except Exception:
        pass
    if not new_token:
        raise RuntimeError("브라우저 인증 후 토큰 추출 실패. DATABRICKS_TOKEN 환경변수를 직접 설정해주세요.")
    # 토큰 파일에 저장
    os.makedirs(os.path.dirname(TOKEN_FILE), exist_ok=True)
    with open(TOKEN_FILE, "w") as f:
        f.write(new_token)
    return dbsql.connect(
        server_hostname=HOST,
        http_path=HTTP_PATH,
        access_token=new_token,
    )


def _exec(conn, sql: str):
    with conn.cursor() as cur:
        cur.execute(sql)


def _esc(v) -> str:
    """SQL 문자열 이스케이프: None→NULL, 그 외 작은따옴표 '' 처리 (SQL 표준)
    Databricks 파서 오동작 방지: 단따옴표(') → 유니코드 오른쪽 따옴표(')로 정규화"""
    if v is None:
        return "NULL"
    # 단따옴표를 유니코드 RIGHT SINGLE QUOTATION MARK(\u2019)로 교체
    # → SQL 내부 이스케이프 불필요, Databricks 파서 오동작 없음
    cleaned = str(v).replace("'", "\u2019")
    return "'" + cleaned + "'"


def _num(v) -> str:
    if v is None:
        return "NULL"
    try:
        return str(float(v))
    except Exception:
        return "NULL"


def _batch_insert(conn, records: list[dict], today: str):
    """100건씩 bulk VALUES INSERT (Databricks SQL 크기 한도 대응)
    batch 실패 시 파라미터 바인딩(%s)으로 1건씩 재시도 → 이스케이프 문제 완전 우회."""
    if not records:
        return
    BATCH = 100
    total = 0
    skip_count = 0

    INSERT_PREFIX = (
        f"INSERT INTO {T_SILVER} "
        "(platform,platform_seller_id,platform_seller_name,product_key,"
        "product_name,spec,price_original,price_sale,discount_rate,"
        "unit_price_desc,delivery_type,is_free_delivery,crawl_date) VALUES "
    )

    def _insert_bulk(rows):
        """rows: list[dict] → bulk VALUES INSERT (문자열 보간)"""
        vals = []
        for r in rows:
            vals.append(
                f"({_esc(r['platform'])},{_esc(r['platform_seller_id'])},"
                f"{_esc(r['platform_seller_name'])},{_esc(r['product_key'])},"
                f"{_esc(r['product_name'])},{_esc(r['spec'])},"
                f"{_num(r['price_original'])},{_num(r['price_sale'])},"
                f"{_num(r['discount_rate'])},{_esc(r['unit_price_desc'])},"
                f"{_esc(r['delivery_type'])},{'TRUE' if r['is_free_delivery'] else 'FALSE'},"
                f"'{today}')"
            )
        with conn.cursor() as cur:
            cur.execute(INSERT_PREFIX + ",\n".join(vals))

    def _insert_one_safe(r):
        """단건 INSERT - _esc에서 따옴표 정규화됐으므로 bulk와 동일 방식"""
        _insert_bulk([r])

    for i in range(0, len(records), BATCH):
        chunk = records[i:i + BATCH]
        try:
            _insert_bulk(chunk)
            total += len(chunk)
            print(f"  INSERT {total}/{len(records)} 건 완료")
        except Exception as e:
            print(f"  ⚠ 배치 INSERT 실패 ({len(chunk)}건), 파라미터 바인딩으로 1건씩 재시도: {e}")
            for r in chunk:
                try:
                    _insert_one_safe(r)
                    total += 1
                except Exception as e2:
                    skip_count += 1
                    print(f"    ✗ 건너뜀: {(r.get('product_name') or '')[:40]} / {e2}")
            print(f"  INSERT {total}/{len(records)} 건 완료 (건너뜀 {skip_count}건)")
    if skip_count:
        print(f"  ⚠ 총 {skip_count}건 INSERT 실패로 건너뜀")


# ── portal_db import (셀러 목록 DB 관리) ──────────────────────────────────
_THIS_DIR_ROOT = os.path.dirname(os.path.abspath(__file__))
_API_DIR_PATH  = os.path.join(_THIS_DIR_ROOT, "api")
if _API_DIR_PATH not in sys.path:
    sys.path.insert(0, _API_DIR_PATH)
try:
    import portal_db as _portal_db
    _DB_AVAILABLE = True
except ImportError:
    _DB_AVAILABLE = False
    print("⚠ portal_db 로드 실패: 셀러 목록을 DB에서 읽을 수 없습니다.")


# ══════════════════════════════════════════════════════════════════════════
# 배민상회 크롤러
# ══════════════════════════════════════════════════════════════════════════
BAEMIN_API     = "https://gw-api-mart.baemin.com/front-api/v1/sellers"
BAEMIN_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json",
    "Referer": "https://mart.baemin.com/",
    "Origin": "https://mart.baemin.com",
}
# 배민상회 배송타입 매핑 (상품별 goodsDeliveryType)
# DIRECT_DELIVERY → 직배송 (수집 대상)
# NORMAL_DELIVERY → 택배배송 (수집 제외)
BAEMIN_DELIVERY_MAP = {
    "DIRECT_DELIVERY": "직배송",
    "NORMAL_DELIVERY": "택배배송",   # 수집 제외
    "PARCEL":          "택배배송",   # 수집 제외
    "FRESH":           "새벽배송",
    "MARKET_DAY":      "장날배송",
}
# 수집 대상 배송유형 (택배 제외)
BAEMIN_COLLECT_TYPES = {"직배송", "새벽배송", "장날배송"}


def _get_baemin_sellers() -> list[dict]:
    """DB에서 활성 배민 셀러 목록 반환. DB 없으면 기본값 사용."""
    if _DB_AVAILABLE:
        try:
            rows = _portal_db.pm_list_baemin_sellers()
            sellers = [{"id": str(r["seller_id"]), "name": r["seller_name"]}
                       for r in rows if r.get("is_active", 1)]
            if sellers:
                return sellers
        except Exception as e:
            print(f"  ⚠ DB 셀러 조회 실패: {e}")
    # 폴백: 기본 셀러 목록
    return [
        {"id": "907",  "name": "이너피스"},
        {"id": "2090", "name": "그로우식자재"},
        {"id": "2089", "name": "스마일푸드"},
        {"id": "1384", "name": "다봄푸드"},
        {"id": "1774", "name": "온국민신선몰"},
        {"id": "2057", "name": "세현F&B"},
        {"id": "2006", "name": "파라도"},
        {"id": "2039", "name": "현대그린푸드"},
        {"id": "2005", "name": "얌피쉬"},
    ]


def _baemin_fetch_page(seller_id: str, page: int) -> dict | None:
    url = f"{BAEMIN_API}/{seller_id}/goods/paging"
    try:
        resp = requests.get(
            url, headers=BAEMIN_HEADERS,
            params={"page": page, "size": 40, "sortType": "RECOMMEND"},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        return data["data"]["goodsList"] if data.get("success") else None
    except Exception as e:
        print(f"  ⚠ 배민 요청 실패 (seller={seller_id} page={page}): {e}")
        return None


def crawl_baemin(test_mode=False) -> list[dict]:
    """배민상회 크롤링. DIRECT_DELIVERY(직배송)만 수집. 택배(NORMAL_DELIVERY) 제외."""
    records = []
    sellers = _get_baemin_sellers()
    if test_mode:
        sellers = sellers[:1]
    print(f"배민상회 셀러 {len(sellers)}개 수집 시작")

    for s in sellers:
        print(f"\n[배민상회] {s['name']} (id={s['id']})")
        first = _baemin_fetch_page(s["id"], 0)
        if not first:
            print("  ✗ 첫 페이지 실패, 건너뜀")
            continue
        total_pages = first.get("totalPages", 1) if not test_mode else 1
        seller_direct = 0
        seller_skip   = 0
        print(f"  총 {first.get('totalElements',0)}개 / {total_pages}페이지")

        for page_no in range(total_pages):
            if page_no > 0:
                time.sleep(0.8)
                data = _baemin_fetch_page(s["id"], page_no)
                if not data:
                    break
                items = data.get("content", [])
            else:
                items = first.get("content", [])

            for item in items:
                delivery_raw  = item.get("goodsDeliveryType", "")
                delivery_text = BAEMIN_DELIVERY_MAP.get(delivery_raw, delivery_raw)
                # 택배배송 제외
                if delivery_text not in BAEMIN_COLLECT_TYPES:
                    seller_skip += 1
                    continue
                product_id = str(item.get("id", ""))
                records.append({
                    "platform":             "baemin",
                    "platform_seller_id":   s["id"],
                    "platform_seller_name": s["name"],
                    "product_key":          f"baemin_{s['id']}_{product_id}",
                    "product_name":         item.get("name", ""),
                    "spec":                 item.get("sizeDesc", ""),
                    "price_original":       item.get("customerPrice") or None,
                    "price_sale":           item.get("goodsPrice") or None,
                    "discount_rate":        item.get("discountRate") or None,
                    "unit_price_desc":      item.get("unitPriceDesc", ""),
                    "delivery_type":        delivery_text,
                    "is_free_delivery":     bool(item.get("freeShipping")),
                })
                seller_direct += 1

            print(f"  page {page_no}: 수집 {seller_direct}건 / 제외(택배) {seller_skip}건 (누적 {len(records)}건)")

    print(f"\n✓ 배민상회 수집 완료: {len(records)}건")
    return records


# ══════════════════════════════════════════════════════════════════════════
# 식봄 크롤러
# ══════════════════════════════════════════════════════════════════════════
FOOD_GQL_URL = "https://api.foodspring.co.kr/v2/graphql"
FOOD_HEADERS = {
    "Content-Type": "application/json",
    "Origin": "https://www.foodspring.co.kr",
    "Referer": "https://www.foodspring.co.kr/",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
}

# 식봄 GraphQL delivery __typename → 배송유형 매핑
FOOD_DELIVERY_TYPENAME_MAP = {
    "DirectDelivery":    "직배송",
    "AggregateDelivery": "싱싱배송",
}
# DB에서 읽어온 delivery_type 값 → 표시 이름
FOOD_DB_DELIVERY_MAP = {
    "direct":   "직배송",
    "singsing": "싱싱배송",
}

# 상품 + 배송타입 동시 조회 쿼리
FOOD_QUERY = """
query SellerGoods($id: ID!, $input: GoodsListInput!, $after: String, $areaId: ID) {
  goodsListPC: goodsList(first: 80, after: $after, input: $input, areaId: $areaId) {
    totalCount
    pageInfo { hasNextPage endCursor }
    edges {
      node {
        nid
        name
        standard
        unit
        price { salePrice originalPrice discountRate }
        isFreeDelivery
      }
    }
  }
  node(id: $id) {
    ... on Vendor {
      nid
      name
      delivery { __typename }
    }
  }
}
"""


def _get_foodspring_sellers() -> list[dict]:
    """DB에서 활성 식봄 셀러 목록 반환 (113개). DB 없으면 기존 10개 사용."""
    if _DB_AVAILABLE:
        try:
            rows = _portal_db.pm_list_foodspring_sellers(active_only=True)
            sellers = [{"id": str(r["seller_id"]), "name": r["seller_name"],
                        "delivery_type": r.get("delivery_type", "")}
                       for r in rows]
            if sellers:
                return sellers
        except Exception as e:
            print(f"  ⚠ DB 식봄 셀러 조회 실패: {e}")
    # 폴백: 기존 10개
    return [
        {"id": "1517", "name": "CJ프레시웨이",    "delivery_type": "singsing"},
        {"id": "5081", "name": "푸드팡-수도권",   "delivery_type": "direct"},
        {"id": "2716", "name": "현대그린푸드",    "delivery_type": "direct"},
        {"id": "2626", "name": "식자재119",       "delivery_type": "direct"},
        {"id": "867",  "name": "온국민 신선몰",   "delivery_type": "direct"},
        {"id": "4069", "name": "디어푸드",        "delivery_type": "direct"},
        {"id": "3038", "name": "푸드레인",        "delivery_type": "direct"},
        {"id": "2455", "name": "세현F&B",         "delivery_type": "direct"},
        {"id": "1388", "name": "다봄푸드",        "delivery_type": "direct"},
        {"id": "3828", "name": "케이에프피(강남)", "delivery_type": "direct"},
    ]


def _food_fetch_page(seller_id: str, after: str | None) -> dict | None:
    vendor_id = f"vendor_{seller_id}"
    variables = {
        "id": vendor_id,
        "input": {
            "sort": "POPULAR_DESC",
            "vendorId": vendor_id,
            "categoryId": "goodsCategoryNode_0",
            "terms": "",
            "delivery": "ALL",
        },
        "after": after,
        "areaId": None,
    }
    try:
        resp = requests.post(
            FOOD_GQL_URL,
            json={"query": FOOD_QUERY, "variables": variables},
            headers=FOOD_HEADERS,
            timeout=20,
        )
        return resp.json().get("data")
    except Exception as e:
        print(f"  ⚠ 식봄 요청 실패 (seller={seller_id}): {e}")
        return None


def crawl_foodspring(test_mode=False) -> list[dict]:
    """
    식봄 크롤링.
    - 배송타입을 GraphQL __typename으로 동적 감지: DirectDelivery→직배송, AggregateDelivery→싱싱배송
    - 감지한 배송타입을 portal_db에 업데이트 (신규 셀러 자동 반영)
    - 직배송/싱싱배송만 수집 (기타 배송유형 제외)
    """
    records = []
    sellers = _get_foodspring_sellers()
    if test_mode:
        sellers = sellers[:1]
    print(f"식봄 셀러 {len(sellers)}개 수집 시작")

    for s in sellers:
        seller_id = s["id"]
        print(f"\n[식봄] seller_id={seller_id}")
        after = None
        seller_name   = s.get("name", seller_id)
        delivery_type = ""   # GraphQL로 감지 후 결정
        page_no       = 0
        seller_count  = 0

        while True:
            data = _food_fetch_page(seller_id, after)
            if not data:
                break

            # 첫 페이지: 셀러명 + 배송타입 __typename 감지
            if page_no == 0:
                node = data.get("node") or {}
                seller_name = node.get("name", seller_id)
                total = (data.get("goodsListPC") or {}).get("totalCount", 0)

                # 배송타입 동적 감지
                typename = (node.get("delivery") or {}).get("__typename", "")
                delivery_type = FOOD_DELIVERY_TYPENAME_MAP.get(typename, "")
                if not delivery_type:
                    # DB에 저장된 값 폴백
                    delivery_type = FOOD_DB_DELIVERY_MAP.get(s.get("delivery_type", ""), "직배송")

                print(f"  셀러명: {seller_name}, 총 {total}개, 배송: {delivery_type} (typename={typename})")

                # 비대상 배송유형이면 스킵 (현재는 모두 대상이므로 예방적 처리)
                if delivery_type not in ("직배송", "싱싱배송"):
                    print(f"  ⚠ 수집 제외 배송유형: {delivery_type}")
                    break

                # DB 배송타입 업데이트 (크롤 시 자동 갱신)
                if _DB_AVAILABLE and typename:
                    db_val = "singsing" if delivery_type == "싱싱배송" else "direct"
                    try:
                        _portal_db.pm_update_foodspring_delivery_type(
                            int(seller_id), db_val, seller_name
                        )
                    except Exception:
                        pass

            goods_list = data.get("goodsListPC") or {}
            edges      = goods_list.get("edges", [])
            page_info  = goods_list.get("pageInfo", {})

            for edge in edges:
                node       = edge.get("node", {})
                price      = node.get("price") or {}
                product_id = str(node.get("nid", ""))
                records.append({
                    "platform":             "foodspring",
                    "platform_seller_id":   seller_id,
                    "platform_seller_name": seller_name,
                    "product_key":          f"foodspring_{seller_id}_{product_id}",
                    "product_name":         node.get("name", ""),
                    "spec":                 node.get("standard", ""),
                    "price_original":       price.get("originalPrice") or None,
                    "price_sale":           price.get("salePrice") or None,
                    "discount_rate":        price.get("discountRate") or None,
                    "unit_price_desc":      node.get("unit", ""),
                    "delivery_type":        delivery_type,
                    "is_free_delivery":     bool(node.get("isFreeDelivery")),
                })
                seller_count += 1

            print(f"  page {page_no}: {len(edges)}개 (누적 {seller_count}개)")

            if test_mode or not page_info.get("hasNextPage"):
                break
            after = page_info.get("endCursor")
            page_no += 1
            time.sleep(0.3)

    print(f"\n✓ 식봄 수집 완료: {len(records)}건")
    return records


# ══════════════════════════════════════════════════════════════════════════
# 메인
# ══════════════════════════════════════════════════════════════════════════
def _crawl_baemin_per_seller(test_mode: bool):
    """배민상회 셀러별 (seller_id, records) 제너레이터."""
    sellers = _get_baemin_sellers()
    if test_mode:
        sellers = sellers[:1]
    print(f"배민상회 셀러 {len(sellers)}개 수집 시작")
    for s in sellers:
        print(f"\n[배민상회] {s['name']} (id={s['id']})")
        first = _baemin_fetch_page(s["id"], 0)
        if not first:
            print("  ✗ 첫 페이지 실패, 건너뜀")
            continue
        total_pages = first.get("totalPages", 1) if not test_mode else 1
        seller_records = []
        seller_direct = 0
        seller_skip   = 0
        print(f"  총 {first.get('totalElements',0)}개 / {total_pages}페이지")
        for page_no in range(total_pages):
            if page_no > 0:
                time.sleep(0.8)
                data = _baemin_fetch_page(s["id"], page_no)
                if not data:
                    break
                items = data.get("content", [])
            else:
                items = first.get("content", [])
            for item in items:
                delivery_raw  = item.get("goodsDeliveryType", "")
                delivery_text = BAEMIN_DELIVERY_MAP.get(delivery_raw, delivery_raw)
                if delivery_text not in BAEMIN_COLLECT_TYPES:
                    seller_skip += 1
                    continue
                product_id = str(item.get("id", ""))
                seller_records.append({
                    "platform":             "baemin",
                    "platform_seller_id":   s["id"],
                    "platform_seller_name": s["name"],
                    "product_key":          f"baemin_{s['id']}_{product_id}",
                    "product_name":         item.get("name", ""),
                    "spec":                 item.get("sizeDesc", ""),
                    "price_original":       item.get("customerPrice") or None,
                    "price_sale":           item.get("goodsPrice") or None,
                    "discount_rate":        item.get("discountRate") or None,
                    "unit_price_desc":      item.get("unitPriceDesc", ""),
                    "delivery_type":        delivery_text,
                    "is_free_delivery":     bool(item.get("freeShipping")),
                })
                seller_direct += 1
            print(f"  page {page_no}: 수집 {seller_direct}건 / 제외(택배) {seller_skip}건")
        yield s["id"], seller_records


def _crawl_food_per_seller(test_mode: bool, only_ids: set | None = None):
    """식봄 셀러별 (seller_id, records) 제너레이터.
    only_ids: 수집할 seller_id 집합 (None이면 전체)"""
    sellers = _get_foodspring_sellers()
    if test_mode:
        sellers = sellers[:1]
    if only_ids:
        sellers = [s for s in sellers if s["id"] in only_ids]
    print(f"식봄 셀러 {len(sellers)}개 수집 시작")
    for s in sellers:
        seller_id = s["id"]
        print(f"\n[식봄] seller_id={seller_id}")
        after = None
        seller_name   = s.get("name", seller_id)
        delivery_type = ""
        page_no       = 0
        seller_records = []
        while True:
            data = _food_fetch_page(seller_id, after)
            if not data:
                break
            if page_no == 0:
                node = data.get("node") or {}
                seller_name = node.get("name", seller_id)
                total = (data.get("goodsListPC") or {}).get("totalCount", 0)
                typename = (node.get("delivery") or {}).get("__typename", "")
                delivery_type = FOOD_DELIVERY_TYPENAME_MAP.get(typename, "")
                if not delivery_type:
                    delivery_type = FOOD_DB_DELIVERY_MAP.get(s.get("delivery_type", ""), "직배송")
                print(f"  셀러명: {seller_name}, 총 {total}개, 배송: {delivery_type}")
                if delivery_type not in ("직배송", "싱싱배송"):
                    print(f"  ⚠ 수집 제외 배송유형: {delivery_type}")
                    break
                if _DB_AVAILABLE and typename:
                    db_val = "singsing" if delivery_type == "싱싱배송" else "direct"
                    try:
                        _portal_db.pm_update_foodspring_delivery_type(int(seller_id), db_val, seller_name)
                    except Exception:
                        pass
            goods_list = data.get("goodsListPC") or {}
            edges      = goods_list.get("edges", [])
            page_info  = goods_list.get("pageInfo", {})
            for edge in edges:
                node2      = edge.get("node", {})
                price      = node2.get("price") or {}
                product_id = str(node2.get("nid", ""))
                seller_records.append({
                    "platform":             "foodspring",
                    "platform_seller_id":   seller_id,
                    "platform_seller_name": seller_name,
                    "product_key":          f"foodspring_{seller_id}_{product_id}",
                    "product_name":         node2.get("name", ""),
                    "spec":                 node2.get("standard", ""),
                    "price_original":       price.get("originalPrice") or None,
                    "price_sale":           price.get("salePrice") or None,
                    "discount_rate":        price.get("discountRate") or None,
                    "unit_price_desc":      node2.get("unit", ""),
                    "delivery_type":        delivery_type,
                    "is_free_delivery":     bool(node2.get("isFreeDelivery")),
                })
            print(f"  page {page_no}: {len(edges)}개 (누적 {len(seller_records)}개)")
            if test_mode or not page_info.get("hasNextPage"):
                break
            after = page_info.get("endCursor")
            page_no += 1
            time.sleep(0.3)
        yield seller_id, seller_records


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--test",    action="store_true", help="셀러 1개, 1페이지만")
    parser.add_argument("--baemin",  action="store_true", help="배민상회만")
    parser.add_argument("--food",    action="store_true", help="식봄만")
    parser.add_argument("--seller",  type=str, default="",
                        help="특정 셀러만 재수집 (쉼표구분, 예: foodspring/1388,foodspring/5081)")
    parser.add_argument("--cleanup", action="store_true",
                        help="기존 배민 택배(NORMAL_DELIVERY/택배배송) 데이터 삭제만 실행")
    args = parser.parse_args()

    # --seller 파싱: {"baemin": {"1234",...}, "foodspring": {"1388","5081"}}
    seller_filter: dict[str, set] = {}
    if args.seller:
        for item in args.seller.split(","):
            item = item.strip()
            if "/" in item:
                pf, sid = item.split("/", 1)
                seller_filter.setdefault(pf.strip(), set()).add(sid.strip())
            else:
                # 플랫폼 없이 ID만 입력하면 식봄으로 간주
                seller_filter.setdefault("foodspring", set()).add(item)
        print(f"[셀러 필터] {seller_filter}")

    today = datetime.date.today().isoformat()
    print(f"{'='*60}")
    print(f"플랫폼 가격 크롤러 시작 (crawl_date={today})")
    if args.test:
        print("TEST MODE: 셀러 1개 / 1페이지만")
    print(f"{'='*60}")

    # Databricks 연결 (크롤링 전에 먼저 확인)
    print(f"\nDatabricks 연결 중...")
    try:
        conn = _get_conn()
        print("  ✓ 연결 성공")
    except Exception as e:
        print(f"  ✗ 연결 실패: {e}")
        return

    # 테이블 생성 확인
    _exec(conn, DDL_CREATE)

    # --cleanup: 기존 NORMAL_DELIVERY/택배배송 데이터 정리 후 종료
    if args.cleanup:
        print("\n[정리] 기존 배민 택배배송 데이터 삭제...")
        try:
            with conn.cursor() as cur:
                cur.execute(f"DELETE FROM {T_SILVER} WHERE platform='baemin' AND delivery_type='NORMAL_DELIVERY'")
                print("  ✓ NORMAL_DELIVERY raw값 삭제 완료")
                cur.execute(f"DELETE FROM {T_SILVER} WHERE platform='baemin' AND delivery_type='택배배송'")
                print("  ✓ 택배배송 삭제 완료")
        except Exception as e:
            print(f"  ✗ 정리 실패: {e}")
        conn.close()
        return

    # 오늘 날짜 기존 데이터 삭제 (--seller 옵션 시 해당 셀러만 삭제)
    print(f"\n기존 {today} 데이터 삭제...")
    if seller_filter:
        for pf, sids in seller_filter.items():
            ids_str = ",".join(f"'{s}'" for s in sids)
            _exec(conn, f"DELETE FROM {T_SILVER} WHERE crawl_date='{today}' AND platform='{pf}' AND platform_seller_id IN ({ids_str})")
            print(f"  ✓ {pf} 셀러 {sids} 기존 데이터 삭제")
    else:
        if not args.food:
            _exec(conn, f"DELETE FROM {T_SILVER} WHERE crawl_date='{today}' AND platform='baemin'")
        if not args.baemin:
            _exec(conn, f"DELETE FROM {T_SILVER} WHERE crawl_date='{today}' AND platform='foodspring'")
    print("  ✓ 삭제 완료")

    # 셀러별 크롤링 + 즉시 저장 (메모리에 쌓지 않음)
    total_saved = 0
    failed_sellers = []

    run_baemin = (not args.food) and (not seller_filter or "baemin" in seller_filter)
    run_food   = (not args.baemin) and (not seller_filter or "foodspring" in seller_filter)

    if run_baemin:
        baemin_ids = seller_filter.get("baemin")
        for seller_id, records in _crawl_baemin_per_seller(test_mode=args.test):
            if baemin_ids and seller_id not in baemin_ids:
                continue
            if not records:
                continue
            try:
                _batch_insert(conn, records, today)
                total_saved += len(records)
                print(f"  ✓ 배민 {seller_id}: {len(records)}건 저장 (누적 {total_saved}건)")
            except Exception as e:
                print(f"  ✗ 배민 {seller_id} 저장 실패: {e}")
                failed_sellers.append(f"baemin/{seller_id}")

    if run_food:
        food_ids = seller_filter.get("foodspring")
        for seller_id, records in _crawl_food_per_seller(test_mode=args.test, only_ids=food_ids):
            if food_ids and seller_id not in food_ids:
                continue
            if not records:
                continue
            try:
                _batch_insert(conn, records, today)
                total_saved += len(records)
                print(f"  ✓ 식봄 {seller_id}: {len(records)}건 저장 (누적 {total_saved}건)")
            except Exception as e:
                print(f"  ✗ 식봄 {seller_id} 저장 실패: {e}")
                failed_sellers.append(f"foodspring/{seller_id}")

    conn.close()

    print(f"\n{'='*60}")
    print(f"✓ 완료! 총 {total_saved:,}건 저장 ({today})")
    if failed_sellers:
        print(f"⚠ 실패 셀러: {', '.join(failed_sellers)}")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()

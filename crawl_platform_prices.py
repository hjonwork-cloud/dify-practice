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


def _batch_insert(conn, records: list[dict], today: str):
    """500건씩 나눠서 INSERT INTO"""
    if not records:
        return
    BATCH = 500
    total = 0
    for i in range(0, len(records), BATCH):
        chunk = records[i:i + BATCH]
        vals = []
        for r in chunk:
            def _s(v):
                if v is None:
                    return "NULL"
                return "'" + str(v).replace("'", "\\'") + "'"
            def _n(v):
                if v is None:
                    return "NULL"
                try:
                    return str(float(v))
                except Exception:
                    return "NULL"
            def _b(v):
                return "TRUE" if v else "FALSE"
            vals.append(
                f"({_s(r['platform'])},{_s(r['platform_seller_id'])},"
                f"{_s(r['platform_seller_name'])},{_s(r['product_key'])},"
                f"{_s(r['product_name'])},{_s(r['spec'])},"
                f"{_n(r['price_original'])},{_n(r['price_sale'])},"
                f"{_n(r['discount_rate'])},{_s(r['unit_price_desc'])},"
                f"{_s(r['delivery_type'])},{_b(r['is_free_delivery'])},"
                f"'{today}')"
            )
        sql = f"INSERT INTO {T_SILVER} VALUES " + ",\n".join(vals)
        with conn.cursor() as cur:
            cur.execute(sql)
        total += len(chunk)
        print(f"  INSERT {total}/{len(records)} 건 완료")


# ══════════════════════════════════════════════════════════════════════════
# 배민상회 크롤러
# ══════════════════════════════════════════════════════════════════════════
BAEMIN_SELLERS = [
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

BAEMIN_API    = "https://gw-api-mart.baemin.com/front-api/v1/sellers"
BAEMIN_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json",
    "Referer": "https://mart.baemin.com/",
    "Origin": "https://mart.baemin.com",
}
DELIVERY_MAP = {
    "DIRECT_DELIVERY": "직배송",
    "PARCEL": "택배배송",
    "FRESH": "새벽배송",
    "MARKET_DAY": "장날배송",
}


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
    records = []
    sellers = BAEMIN_SELLERS[:1] if test_mode else BAEMIN_SELLERS
    for s in sellers:
        print(f"\n[배민상회] {s['name']} (id={s['id']})")
        first = _baemin_fetch_page(s["id"], 0)
        if not first:
            print("  ✗ 첫 페이지 실패, 건너뜀")
            continue
        total_pages = first.get("totalPages", 1) if not test_mode else 1
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
                delivery_raw = item.get("goodsDeliveryType", "")
                delivery_text = DELIVERY_MAP.get(delivery_raw, delivery_raw)
                if item.get("freshShipping"):
                    delivery_text = "새벽배송"
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
            print(f"  page {page_no}: {len(items)}개 (누적 {len(records)}개)")

    print(f"\n✓ 배민상회 수집 완료: {len(records)}건")
    return records


# ══════════════════════════════════════════════════════════════════════════
# 식봄 크롤러
# ══════════════════════════════════════════════════════════════════════════
FOOD_SELLERS = [
    "1517", "5081", "2716", "2626", "867",
    "4069", "3038", "2455", "1388", "3828",
]

FOOD_GQL_URL = "https://api.foodspring.co.kr/v2/graphql"
FOOD_HEADERS = {
    "Content-Type": "application/json",
    "Origin": "https://www.foodspring.co.kr",
    "Referer": "https://www.foodspring.co.kr/",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
}
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
      delivery {
        ... on DirectDelivery {
          arrivalTag
          deliveryFee { condition price }
        }
      }
    }
  }
}
"""


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
    records = []
    sellers = FOOD_SELLERS[:1] if test_mode else FOOD_SELLERS
    for seller_id in sellers:
        print(f"\n[식봄] seller_id={seller_id}")
        after = None
        seller_name = seller_id
        page_no = 0
        seller_count = 0

        while True:
            data = _food_fetch_page(seller_id, after)
            if not data:
                break

            # 셀러명 추출 (첫 페이지)
            if page_no == 0:
                node = data.get("node") or {}
                seller_name = node.get("name", seller_id)
                total = (data.get("goodsListPC") or {}).get("totalCount", 0)
                print(f"  셀러명: {seller_name}, 총 {total}개")

            goods_list = data.get("goodsListPC") or {}
            edges = goods_list.get("edges", [])
            page_info = goods_list.get("pageInfo", {})

            for edge in edges:
                node = edge.get("node", {})
                price = node.get("price") or {}
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
                    "delivery_type":        "직배송",
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
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--test",   action="store_true", help="셀러 1개, 1페이지만")
    parser.add_argument("--baemin", action="store_true", help="배민상회만")
    parser.add_argument("--food",   action="store_true", help="식봄만")
    args = parser.parse_args()

    today = datetime.date.today().isoformat()  # "2026-08-18"
    print(f"{'='*60}")
    print(f"플랫폼 가격 크롤러 시작 (crawl_date={today})")
    if args.test:
        print("TEST MODE: 셀러 1개 / 1페이지만")
    print(f"{'='*60}")

    # 1. 크롤링
    records = []
    if not args.food:
        records += crawl_baemin(test_mode=args.test)
    if not args.baemin:
        records += crawl_foodspring(test_mode=args.test)

    print(f"\n총 수집: {len(records)}건")
    if not records:
        print("수집 결과 없음. 종료합니다.")
        return

    # 2. Databricks 저장
    print(f"\nDatabricks 저장 시작 ({T_SILVER})...")
    try:
        conn = _get_conn()
        print("  ✓ Databricks 연결 성공")
    except Exception as e:
        print(f"  ✗ 연결 실패: {e}")
        # 로컬 JSON으로 백업 저장
        out = f"crawl_result_{today}.json"
        with open(out, "w", encoding="utf-8") as f:
            json.dump(records, f, ensure_ascii=False, indent=2)
        print(f"  → 로컬 백업 저장: {out}")
        return

    with conn:
        # 테이블 생성 (없으면)
        print("  테이블 생성 확인...")
        _exec(conn, DDL_CREATE)
        print("  ✓ 테이블 준비 완료")

        # 오늘 날짜 데이터 삭제 후 재적재
        print(f"  기존 {today} 데이터 삭제...")
        _exec(conn, f"DELETE FROM {T_SILVER} WHERE crawl_date = '{today}'")
        print("  ✓ 삭제 완료")

        # INSERT
        print(f"  INSERT 시작 ({len(records)}건)...")
        _batch_insert(conn, records, today)

    print(f"\n{'='*60}")
    print(f"✓ 완료! {len(records)}건 저장 ({today})")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()

"""
?뚮옯??媛寃??듯빀 ?щ·??- 諛곕??곹쉶 + ?앸큵
?섏쭛 寃곌낵瑜?Databricks silver.dim_platform_products ?뚯씠釉붿뿉 ???

?ㅽ뻾:
  python crawl_platform_prices.py            # ?꾩껜 ?섏쭛
  python crawl_platform_prices.py --test     # ???1媛? 1?섏씠吏留?(寃利앹슜)
  python crawl_platform_prices.py --baemin   # 諛곕??곹쉶留?
  python crawl_platform_prices.py --food     # ?앸큵留?
"""

import sys, os, time, datetime, json, argparse, re
import requests

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass  # Azure pipe ?섍꼍?먯꽌 reconfigure ?ㅽ뙣 臾댁떆

# ?? Databricks ?곌껐 ?ㅼ젙 ??????????????????????????????????????????????????
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
        "Databricks ?좏겙??李얠쓣 ???놁뒿?덈떎.\n"
        "?섍꼍蹂??DATABRICKS_TOKEN ???ㅼ젙?섍굅??n"
        f"?ы꽭 ?쒕쾭瑜?癒쇱? ?ㅽ뻾??{TOKEN_FILE} ???앹꽦?섏꽭??"
    )


def _get_conn():
    import databricks.sql as dbsql
    token = os.getenv("DATABRICKS_TOKEN", "").strip()

    # ?섍꼍蹂?섏뿉 ?놁쑝硫??좏겙 ?뚯씪?먯꽌 ?쒕룄
    if not token and os.path.exists(TOKEN_FILE):
        with open(TOKEN_FILE) as f:
            token = f.read().strip()

    # ?좏겙???덉쑝硫?諛붾줈 ?곌껐 ?쒕룄
    if token:
        try:
            conn = dbsql.connect(
                server_hostname=HOST,
                http_path=HTTP_PATH,
                access_token=token,
            )
            # ?곌껐 ?뚯뒪??
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
            print("  ??PAT ?좏겙?쇰줈 ?곌껐 ?깃났")
            return conn
        except Exception as e:
            print(f"  ??PAT ?좏겙 ?곌껐 ?ㅽ뙣 ({e}), 釉뚮씪?곗? ?몄쬆 ?쒕룄...")

    # 釉뚮씪?곗? OAuth ?대갚
    print("  釉뚮씪?곗? 濡쒓렇???앹뾽???대┰?덈떎...")
    from databricks.sdk import WorkspaceClient
    from databricks.sdk.service.sql import StatementState
    wc = WorkspaceClient(host=f"https://{HOST}", auth_type="external-browser")
    me = wc.current_user.me()
    print(f"  ??濡쒓렇?? {me.user_name}")
    # SDK?먯꽌 ?좏겙 異붿텧
    new_token = None
    try:
        creds = wc.config.authenticate()
        new_token = creds.get("Authorization", "").replace("Bearer ", "")
    except Exception:
        pass
    if not new_token:
        raise RuntimeError("釉뚮씪?곗? ?몄쬆 ???좏겙 異붿텧 ?ㅽ뙣. DATABRICKS_TOKEN ?섍꼍蹂?섎? 吏곸젒 ?ㅼ젙?댁＜?몄슂.")
    # ?좏겙 ?뚯씪?????
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
    """SQL 臾몄옄???댁뒪耳?댄봽: None?묿ULL, 洹????묒??곗샂??'' 泥섎━ (SQL ?쒖?)
    Databricks ?뚯꽌 ?ㅻ룞??諛⑹?: ?⑤뵲?댄몴(') ???좊땲肄붾뱶 ?ㅻⅨ履??곗샂??')濡??뺢퇋??""
    if v is None:
        return "NULL"
    # ?⑤뵲?댄몴瑜??좊땲肄붾뱶 RIGHT SINGLE QUOTATION MARK(\u2019)濡?援먯껜
    # ??SQL ?대? ?댁뒪耳?댄봽 遺덊븘?? Databricks ?뚯꽌 ?ㅻ룞???놁쓬
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
    """100嫄댁뵫 bulk VALUES INSERT (Databricks SQL ?ш린 ?쒕룄 ???
    batch ?ㅽ뙣 ???뚮씪誘명꽣 諛붿씤??%s)?쇰줈 1嫄댁뵫 ?ъ떆?????댁뒪耳?댄봽 臾몄젣 ?꾩쟾 ?고쉶."""
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
        """rows: list[dict] ??bulk VALUES INSERT (臾몄옄??蹂닿컙)"""
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
        """?④굔 INSERT - _esc?먯꽌 ?곗샂???뺢퇋?붾릱?쇰?濡?bulk? ?숈씪 諛⑹떇"""
        _insert_bulk([r])

    for i in range(0, len(records), BATCH):
        chunk = records[i:i + BATCH]
        try:
            _insert_bulk(chunk)
            total += len(chunk)
            print(f"  INSERT {total}/{len(records)} 嫄??꾨즺")
        except Exception as e:
            print(f"  ??諛곗튂 INSERT ?ㅽ뙣 ({len(chunk)}嫄?, ?뚮씪誘명꽣 諛붿씤?⑹쑝濡?1嫄댁뵫 ?ъ떆?? {e}")
            for r in chunk:
                try:
                    _insert_one_safe(r)
                    total += 1
                except Exception as e2:
                    skip_count += 1
                    print(f"    ??嫄대꼫?: {(r.get('product_name') or '')[:40]} / {e2}")
            print(f"  INSERT {total}/{len(records)} 嫄??꾨즺 (嫄대꼫? {skip_count}嫄?")
    if skip_count:
        print(f"  ??珥?{skip_count}嫄?INSERT ?ㅽ뙣濡?嫄대꼫?")


# ?? portal_db import (???紐⑸줉 DB 愿由? ??????????????????????????????????
_THIS_DIR_ROOT = os.path.dirname(os.path.abspath(__file__))
_API_DIR_PATH  = os.path.join(_THIS_DIR_ROOT, "api")
if _API_DIR_PATH not in sys.path:
    sys.path.insert(0, _API_DIR_PATH)
try:
    import portal_db as _portal_db
    _DB_AVAILABLE = True
except ImportError:
    _DB_AVAILABLE = False
    print("??portal_db 濡쒕뱶 ?ㅽ뙣: ???紐⑸줉??DB?먯꽌 ?쎌쓣 ???놁뒿?덈떎.")


# ?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧
# 諛곕??곹쉶 ?щ·??
# ?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧
BAEMIN_API     = "https://gw-api-mart.baemin.com/front-api/v1/sellers"
BAEMIN_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json",
    "Referer": "https://mart.baemin.com/",
    "Origin": "https://mart.baemin.com",
}
# 諛곕??곹쉶 諛곗넚???留ㅽ븨 (?곹뭹蹂?goodsDeliveryType)
# DIRECT_DELIVERY ??吏곷같??(?섏쭛 ???
# NORMAL_DELIVERY ???앸같諛곗넚 (?섏쭛 ?쒖쇅)
BAEMIN_DELIVERY_MAP = {
    "DIRECT_DELIVERY": "吏곷같??,
    "NORMAL_DELIVERY": "?앸같諛곗넚",   # ?섏쭛 ?쒖쇅
    "PARCEL":          "?앸같諛곗넚",   # ?섏쭛 ?쒖쇅
    "FRESH":           "?덈꼍諛곗넚",
    "MARKET_DAY":      "?λ궇諛곗넚",
}
# ?섏쭛 ???諛곗넚?좏삎 (?앸같 ?쒖쇅)
BAEMIN_COLLECT_TYPES = {"吏곷같??, "?덈꼍諛곗넚", "?λ궇諛곗넚"}


def _get_baemin_sellers() -> list[dict]:
    """DB?먯꽌 ?쒖꽦 諛곕? ???紐⑸줉 諛섑솚. DB ?놁쑝硫?湲곕낯媛??ъ슜."""
    if _DB_AVAILABLE:
        try:
            rows = _portal_db.pm_list_baemin_sellers()
            sellers = [{"id": str(r["seller_id"]), "name": r["seller_name"]}
                       for r in rows if r.get("is_active", 1)]
            if sellers:
                return sellers
        except Exception as e:
            print(f"  ??DB ???議고쉶 ?ㅽ뙣: {e}")
    # ?대갚: 湲곕낯 ???紐⑸줉
    return [
        {"id": "907",  "name": "?대꼫?쇱뒪"},
        {"id": "2090", "name": "洹몃줈?곗떇?먯옱"},
        {"id": "2089", "name": "?ㅻ쭏?쇳뫖??},
        {"id": "1384", "name": "?ㅻ큵?몃뱶"},
        {"id": "1774", "name": "?④뎅誘쇱떊?좊ぐ"},
        {"id": "2057", "name": "?명쁽F&B"},
        {"id": "2006", "name": "?뚮씪??},
        {"id": "2039", "name": "?꾨?洹몃┛?몃뱶"},
        {"id": "2005", "name": "?뚰뵾??},
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
        print(f"  ??諛곕? ?붿껌 ?ㅽ뙣 (seller={seller_id} page={page}): {e}")
        return None


def crawl_baemin(test_mode=False) -> list[dict]:
    """諛곕??곹쉶 ?щ·留? DIRECT_DELIVERY(吏곷같??留??섏쭛. ?앸같(NORMAL_DELIVERY) ?쒖쇅."""
    records = []
    sellers = _get_baemin_sellers()
    if test_mode:
        sellers = sellers[:1]
    print(f"諛곕??곹쉶 ???{len(sellers)}媛??섏쭛 ?쒖옉")

    for s in sellers:
        print(f"\n[諛곕??곹쉶] {s['name']} (id={s['id']})")
        first = _baemin_fetch_page(s["id"], 0)
        if not first:
            print("  ??泥??섏씠吏 ?ㅽ뙣, 嫄대꼫?")
            continue
        total_pages = first.get("totalPages", 1) if not test_mode else 1
        seller_direct = 0
        seller_skip   = 0
        print(f"  珥?{first.get('totalElements',0)}媛?/ {total_pages}?섏씠吏")

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
                # ?앸같諛곗넚 ?쒖쇅
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

            print(f"  page {page_no}: ?섏쭛 {seller_direct}嫄?/ ?쒖쇅(?앸같) {seller_skip}嫄?(?꾩쟻 {len(records)}嫄?")

    print(f"\n??諛곕??곹쉶 ?섏쭛 ?꾨즺: {len(records)}嫄?)
    return records


# ?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧
# ?앸큵 ?щ·??
# ?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧
FOOD_GQL_URL = "https://api.foodspring.co.kr/v2/graphql"
FOOD_HEADERS = {
    "Content-Type": "application/json",
    "Origin": "https://www.foodspring.co.kr",
    "Referer": "https://www.foodspring.co.kr/",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
}

# ?앸큵 GraphQL delivery __typename ??諛곗넚?좏삎 留ㅽ븨
FOOD_DELIVERY_TYPENAME_MAP = {
    "DirectDelivery":    "吏곷같??,
    "AggregateDelivery": "?깆떛諛곗넚",
}
# DB?먯꽌 ?쎌뼱??delivery_type 媛????쒖떆 ?대쫫
FOOD_DB_DELIVERY_MAP = {
    "direct":   "吏곷같??,
    "singsing": "?깆떛諛곗넚",
}

# ?곹뭹 + 諛곗넚????숈떆 議고쉶 荑쇰━
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
    """DB?먯꽌 ?쒖꽦 ?앸큵 ???紐⑸줉 諛섑솚 (113媛?. DB ?놁쑝硫?湲곗〈 10媛??ъ슜."""
    if _DB_AVAILABLE:
        try:
            rows = _portal_db.pm_list_foodspring_sellers(active_only=True)
            sellers = [{"id": str(r["seller_id"]), "name": r["seller_name"],
                        "delivery_type": r.get("delivery_type", "")}
                       for r in rows]
            if sellers:
                return sellers
        except Exception as e:
            print(f"  ??DB ?앸큵 ???議고쉶 ?ㅽ뙣: {e}")
    # ?대갚: 湲곗〈 10媛?
    return [
        {"id": "1517", "name": "CJ?꾨젅?쒖썾??,    "delivery_type": "singsing"},
        {"id": "5081", "name": "?몃뱶???섎룄沅?,   "delivery_type": "direct"},
        {"id": "2716", "name": "?꾨?洹몃┛?몃뱶",    "delivery_type": "direct"},
        {"id": "2626", "name": "?앹옄??19",       "delivery_type": "direct"},
        {"id": "867",  "name": "?④뎅誘??좎꽑紐?,   "delivery_type": "direct"},
        {"id": "4069", "name": "?붿뼱?몃뱶",        "delivery_type": "direct"},
        {"id": "3038", "name": "?몃뱶?덉씤",        "delivery_type": "direct"},
        {"id": "2455", "name": "?명쁽F&B",         "delivery_type": "direct"},
        {"id": "1388", "name": "?ㅻ큵?몃뱶",        "delivery_type": "direct"},
        {"id": "3828", "name": "耳?댁뿉?꾪뵾(媛뺣궓)", "delivery_type": "direct"},
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
        print(f"  ???앸큵 ?붿껌 ?ㅽ뙣 (seller={seller_id}): {e}")
        return None


def crawl_foodspring(test_mode=False) -> list[dict]:
    """
    ?앸큵 ?щ·留?
    - 諛곗넚??낆쓣 GraphQL __typename?쇰줈 ?숈쟻 媛먯?: DirectDelivery?믪쭅諛곗넚, AggregateDelivery?믪떛?깅같??
    - 媛먯???諛곗넚??낆쓣 portal_db???낅뜲?댄듃 (?좉퇋 ????먮룞 諛섏쁺)
    - 吏곷같???깆떛諛곗넚留??섏쭛 (湲고? 諛곗넚?좏삎 ?쒖쇅)
    """
    records = []
    sellers = _get_foodspring_sellers()
    if test_mode:
        sellers = sellers[:1]
    print(f"?앸큵 ???{len(sellers)}媛??섏쭛 ?쒖옉")

    for s in sellers:
        seller_id = s["id"]
        print(f"\n[?앸큵] seller_id={seller_id}")
        after = None
        seller_name   = s.get("name", seller_id)
        delivery_type = ""   # GraphQL濡?媛먯? ??寃곗젙
        page_no       = 0
        seller_count  = 0

        while True:
            data = _food_fetch_page(seller_id, after)
            if not data:
                break

            # 泥??섏씠吏: ??щ챸 + 諛곗넚???__typename 媛먯?
            if page_no == 0:
                node = data.get("node") or {}
                seller_name = node.get("name", seller_id)
                total = (data.get("goodsListPC") or {}).get("totalCount", 0)

                # 諛곗넚????숈쟻 媛먯?
                typename = (node.get("delivery") or {}).get("__typename", "")
                delivery_type = FOOD_DELIVERY_TYPENAME_MAP.get(typename, "")
                if not delivery_type:
                    # DB????λ맂 媛??대갚
                    delivery_type = FOOD_DB_DELIVERY_MAP.get(s.get("delivery_type", ""), "吏곷같??)

                print(f"  ??щ챸: {seller_name}, 珥?{total}媛? 諛곗넚: {delivery_type} (typename={typename})")

                # 鍮꾨???諛곗넚?좏삎?대㈃ ?ㅽ궢 (?꾩옱??紐⑤몢 ??곸씠誘濡??덈갑??泥섎━)
                if delivery_type not in ("吏곷같??, "?깆떛諛곗넚"):
                    print(f"  ???섏쭛 ?쒖쇅 諛곗넚?좏삎: {delivery_type}")
                    break

                # DB 諛곗넚????낅뜲?댄듃 (?щ· ???먮룞 媛깆떊)
                if _DB_AVAILABLE and typename:
                    db_val = "singsing" if delivery_type == "?깆떛諛곗넚" else "direct"
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

            print(f"  page {page_no}: {len(edges)}媛?(?꾩쟻 {seller_count}媛?")

            if test_mode or not page_info.get("hasNextPage"):
                break
            after = page_info.get("endCursor")
            page_no += 1
            time.sleep(0.3)

    print(f"\n???앸큵 ?섏쭛 ?꾨즺: {len(records)}嫄?)
    return records


# ?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧
# 硫붿씤
# ?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧
def _crawl_baemin_per_seller(test_mode: bool):
    """諛곕??곹쉶 ??щ퀎 (seller_id, records) ?쒕꼫?덉씠??"""
    sellers = _get_baemin_sellers()
    if test_mode:
        sellers = sellers[:1]
    print(f"諛곕??곹쉶 ???{len(sellers)}媛??섏쭛 ?쒖옉")
    for s in sellers:
        print(f"\n[諛곕??곹쉶] {s['name']} (id={s['id']})")
        first = _baemin_fetch_page(s["id"], 0)
        if not first:
            print("  ??泥??섏씠吏 ?ㅽ뙣, 嫄대꼫?")
            continue
        total_pages = first.get("totalPages", 1) if not test_mode else 1
        seller_records = []
        seller_direct = 0
        seller_skip   = 0
        print(f"  珥?{first.get('totalElements',0)}媛?/ {total_pages}?섏씠吏")
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
            print(f"  page {page_no}: ?섏쭛 {seller_direct}嫄?/ ?쒖쇅(?앸같) {seller_skip}嫄?)
        yield s["id"], seller_records


def _crawl_food_per_seller(test_mode: bool, only_ids: set | None = None):
    """?앸큵 ??щ퀎 (seller_id, records) ?쒕꼫?덉씠??
    only_ids: ?섏쭛??seller_id 吏묓빀 (None?대㈃ ?꾩껜)"""
    sellers = _get_foodspring_sellers()
    if test_mode:
        sellers = sellers[:1]
    if only_ids:
        sellers = [s for s in sellers if s["id"] in only_ids]
    print(f"?앸큵 ???{len(sellers)}媛??섏쭛 ?쒖옉")
    for s in sellers:
        seller_id = s["id"]
        print(f"\n[?앸큵] seller_id={seller_id}")
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
                    delivery_type = FOOD_DB_DELIVERY_MAP.get(s.get("delivery_type", ""), "吏곷같??)
                print(f"  ??щ챸: {seller_name}, 珥?{total}媛? 諛곗넚: {delivery_type}")
                if delivery_type not in ("吏곷같??, "?깆떛諛곗넚"):
                    print(f"  ???섏쭛 ?쒖쇅 諛곗넚?좏삎: {delivery_type}")
                    break
                if _DB_AVAILABLE and typename:
                    db_val = "singsing" if delivery_type == "?깆떛諛곗넚" else "direct"
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
            print(f"  page {page_no}: {len(edges)}媛?(?꾩쟻 {len(seller_records)}媛?")
            if test_mode or not page_info.get("hasNextPage"):
                break
            after = page_info.get("endCursor")
            page_no += 1
            time.sleep(0.3)
        yield seller_id, seller_records


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--test",    action="store_true", help="???1媛? 1?섏씠吏留?)
    parser.add_argument("--baemin",  action="store_true", help="諛곕??곹쉶留?)
    parser.add_argument("--food",    action="store_true", help="?앸큵留?)
    parser.add_argument("--seller",  type=str, default="",
                        help="?뱀젙 ??щ쭔 ?ъ닔吏?(?쇳몴援щ텇, ?? foodspring/1388,foodspring/5081)")
    parser.add_argument("--cleanup", action="store_true",
                        help="湲곗〈 諛곕? ?앸같(NORMAL_DELIVERY/?앸같諛곗넚) ?곗씠????젣留??ㅽ뻾")
    parser.add_argument("--delete-date", type=str, default="",
                        help="?뱀젙 ?좎쭨 ?곗씠???꾩껜 ??젣 ??醫낅즺 (?? --delete-date 2026-08-18)")
    args = parser.parse_args()

    # --seller ?뚯떛: {"baemin": {"1234",...}, "foodspring": {"1388","5081"}}
    seller_filter: dict[str, set] = {}
    if args.seller:
        for item in args.seller.split(","):
            item = item.strip()
            if "/" in item:
                pf, sid = item.split("/", 1)
                seller_filter.setdefault(pf.strip(), set()).add(sid.strip())
            else:
                # ?뚮옯???놁씠 ID留??낅젰?섎㈃ ?앸큵?쇰줈 媛꾩＜
                seller_filter.setdefault("foodspring", set()).add(item)
        print(f"[????꾪꽣] {seller_filter}")

    today = datetime.date.today().isoformat()
    print(f"{'='*60}")
    print(f"?뚮옯??媛寃??щ·???쒖옉 (crawl_date={today})")
    if args.test:
        print("TEST MODE: ???1媛?/ 1?섏씠吏留?)
    print(f"{'='*60}")

    # Databricks ?곌껐 (?щ·留??꾩뿉 癒쇱? ?뺤씤)
    print(f"\nDatabricks ?곌껐 以?..")
    try:
        conn = _get_conn()
        print("  ???곌껐 ?깃났")
    except Exception as e:
        print(f"  ???곌껐 ?ㅽ뙣: {e}")
        return

    # ?뚯씠釉??앹꽦 ?뺤씤
    _exec(conn, DDL_CREATE)

    # --cleanup: 湲곗〈 NORMAL_DELIVERY/?앸같諛곗넚 ?곗씠???뺣━ ??醫낅즺
    if args.cleanup:
        print("\n[?뺣━] 湲곗〈 諛곕? ?앸같諛곗넚 ?곗씠????젣...")
        try:
            with conn.cursor() as cur:
                cur.execute(f"DELETE FROM {T_SILVER} WHERE platform='baemin' AND delivery_type='NORMAL_DELIVERY'")
                print("  ??NORMAL_DELIVERY raw媛???젣 ?꾨즺")
                cur.execute(f"DELETE FROM {T_SILVER} WHERE platform='baemin' AND delivery_type='?앸같諛곗넚'")
                print("  ???앸같諛곗넚 ??젣 ?꾨즺")
        except Exception as e:
            print(f"  ???뺣━ ?ㅽ뙣: {e}")
        conn.close()
        return

    # --delete-date: ?뱀젙 ?좎쭨 ?곗씠???꾩껜 ??젣 ??醫낅즺
    if args.delete_date:
        d = args.delete_date.strip()
        print(f"\n[??젣] {d} ?좎쭨 ?곗씠???꾩껜 ??젣 以?..")
        try:
            _exec(conn, f"DELETE FROM {T_SILVER} WHERE crawl_date = '{d}'")
            print(f"  ??{d} ?곗씠????젣 ?꾨즺")
        except Exception as e:
            print(f"  ????젣 ?ㅽ뙣: {e}")
        conn.close()
        return
        conn.close()
        return

    # ?ㅻ뒛 ?좎쭨 湲곗〈 ?곗씠????젣 (--seller ?듭뀡 ???대떦 ??щ쭔 ??젣)
    print(f"\n湲곗〈 {today} ?곗씠????젣...")
    if seller_filter:
        for pf, sids in seller_filter.items():
            ids_str = ",".join(f"'{s}'" for s in sids)
            _exec(conn, f"DELETE FROM {T_SILVER} WHERE crawl_date='{today}' AND platform='{pf}' AND platform_seller_id IN ({ids_str})")
            print(f"  ??{pf} ???{sids} 湲곗〈 ?곗씠????젣")
    else:
        if not args.food:
            _exec(conn, f"DELETE FROM {T_SILVER} WHERE crawl_date='{today}' AND platform='baemin'")
        if not args.baemin:
            _exec(conn, f"DELETE FROM {T_SILVER} WHERE crawl_date='{today}' AND platform='foodspring'")
    print("  ????젣 ?꾨즺")

    # ??щ퀎 ?щ·留?+ 利됱떆 ???(硫붾え由ъ뿉 ?볦? ?딆쓬)
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
                print(f"  ??諛곕? {seller_id}: {len(records)}嫄????(?꾩쟻 {total_saved}嫄?")
            except Exception as e:
                print(f"  ??諛곕? {seller_id} ????ㅽ뙣: {e}")
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
                print(f"  ???앸큵 {seller_id}: {len(records)}嫄????(?꾩쟻 {total_saved}嫄?")
            except Exception as e:
                print(f"  ???앸큵 {seller_id} ????ㅽ뙣: {e}")
                failed_sellers.append(f"foodspring/{seller_id}")

    conn.close()

    print(f"\n{'='*60}")
    print(f"???꾨즺! 珥?{total_saved:,}嫄????({today})")
    if failed_sellers:
        print(f"???ㅽ뙣 ??? {', '.join(failed_sellers)}")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
#   s y n c   t e s t 
 

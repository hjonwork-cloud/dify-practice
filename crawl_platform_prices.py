"""
?åÎû´??Í∞ÄÍ≤??µÌï© ?¨Î°§??- Î∞∞Î??ÅÌöå + ?ùÎ¥Ñ
?òÏßë Í≤∞Í≥ºÎ•?Databricks silver.dim_platform_products ?åÏù¥Î∏îÏóê ?Ä??

?§Ìñâ:
  python crawl_platform_prices.py            # ?ÑÏ≤¥ ?òÏßë
  python crawl_platform_prices.py --test     # ?Ä??1Í∞? 1?òÏù¥ÏßÄÎß?(Í≤ÄÏ¶ùÏö©)
  python crawl_platform_prices.py --baemin   # Î∞∞Î??ÅÌöåÎß?
  python crawl_platform_prices.py --food     # ?ùÎ¥ÑÎß?
"""

import sys, os, time, datetime, json, argparse, re
import requests

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass  # Azure pipe ?òÍ≤Ω?êÏÑú reconfigure ?§Ìå® Î¨¥Ïãú

# ?Ä?Ä Databricks ?∞Í≤∞ ?§Ï†ï ?Ä?Ä?Ä?Ä?Ä?Ä?Ä?Ä?Ä?Ä?Ä?Ä?Ä?Ä?Ä?Ä?Ä?Ä?Ä?Ä?Ä?Ä?Ä?Ä?Ä?Ä?Ä?Ä?Ä?Ä?Ä?Ä?Ä?Ä?Ä?Ä?Ä?Ä?Ä?Ä?Ä?Ä?Ä?Ä?Ä?Ä?Ä?Ä?Ä?Ä
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
        "Databricks ?†ÌÅ∞??Ï∞æÏùÑ ???ÜÏäµ?àÎã§.\n"
        "?òÍ≤ΩÎ≥Ä??DATABRICKS_TOKEN ???§Ï†ï?òÍ±∞??n"
        f"?¨ÌÑ∏ ?úÎ≤ÑÎ•?Î®ºÏ? ?§Ìñâ??{TOKEN_FILE} ???ùÏÑ±?òÏÑ∏??"
    )


def _get_conn():
    import databricks.sql as dbsql
    token = os.getenv("DATABRICKS_TOKEN", "").strip()

    # ?òÍ≤ΩÎ≥Ä?òÏóê ?ÜÏúºÎ©??†ÌÅ∞ ?åÏùº?êÏÑú ?úÎèÑ
    if not token and os.path.exists(TOKEN_FILE):
        with open(TOKEN_FILE) as f:
            token = f.read().strip()

    # ?†ÌÅ∞???àÏúºÎ©?Î∞îÎ°ú ?∞Í≤∞ ?úÎèÑ
    if token:
        try:
            conn = dbsql.connect(
                server_hostname=HOST,
                http_path=HTTP_PATH,
                access_token=token,
            )
            # ?∞Í≤∞ ?åÏä§??
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
            print("  ??PAT ?†ÌÅ∞?ºÎ°ú ?∞Í≤∞ ?±Í≥µ")
            return conn
        except Exception as e:
            print(f"  ??PAT ?†ÌÅ∞ ?∞Í≤∞ ?§Ìå® ({e}), Î∏åÎùº?∞Ï? ?∏Ï¶ù ?úÎèÑ...")

    # Î∏åÎùº?∞Ï? OAuth ?¥Î∞±
    print("  Î∏åÎùº?∞Ï? Î°úÍ∑∏???ùÏóÖ???¥Î¶Ω?àÎã§...")
    from databricks.sdk import WorkspaceClient
    from databricks.sdk.service.sql import StatementState
    wc = WorkspaceClient(host=f"https://{HOST}", auth_type="external-browser")
    me = wc.current_user.me()
    print(f"  ??Î°úÍ∑∏?? {me.user_name}")
    # SDK?êÏÑú ?†ÌÅ∞ Ï∂îÏ∂ú
    new_token = None
    try:
        creds = wc.config.authenticate()
        new_token = creds.get("Authorization", "").replace("Bearer ", "")
    except Exception:
        pass
    if not new_token:
        raise RuntimeError("Î∏åÎùº?∞Ï? ?∏Ï¶ù ???†ÌÅ∞ Ï∂îÏ∂ú ?§Ìå®. DATABRICKS_TOKEN ?òÍ≤ΩÎ≥Ä?òÎ? ÏßÅÏ†ë ?§Ï†ï?¥Ï£º?∏Ïöî.")
    # ?†ÌÅ∞ ?åÏùº???Ä??
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
    """SQL Î¨∏Ïûê???¥Ïä§ÏºÄ?¥ÌîÑ: None?íNULL, Í∑????ëÏ??∞Ïò¥??'' Ï≤òÎ¶¨ (SQL ?úÏ?)
    Databricks ?åÏÑú ?§Îèô??Î∞©Ï?: ?®Îî∞?¥Ìëú(') ???†ÎãàÏΩîÎìú ?§Î•∏Ï™??∞Ïò¥??')Î°??ïÍ∑ú??""
    if v is None:
        return "NULL"
    # ?®Îî∞?¥ÌëúÎ•??†ÎãàÏΩîÎìú RIGHT SINGLE QUOTATION MARK(\u2019)Î°?ÍµêÏ≤¥
    # ??SQL ?¥Î? ?¥Ïä§ÏºÄ?¥ÌîÑ Î∂àÌïÑ?? Databricks ?åÏÑú ?§Îèô???ÜÏùå
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
    """100Í±¥Ïî© bulk VALUES INSERT (Databricks SQL ?¨Í∏∞ ?úÎèÑ ?Ä??
    batch ?§Ìå® ???åÎùºÎØ∏ÌÑ∞ Î∞îÏù∏??%s)?ºÎ°ú 1Í±¥Ïî© ?¨Ïãú?????¥Ïä§ÏºÄ?¥ÌîÑ Î¨∏Ï†ú ?ÑÏ†Ñ ?∞Ìöå."""
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
        """rows: list[dict] ??bulk VALUES INSERT (Î¨∏Ïûê??Î≥¥Í∞Ñ)"""
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
        """?®Í±¥ INSERT - _esc?êÏÑú ?∞Ïò¥???ïÍ∑ú?îÎêê?ºÎ?Î°?bulk?Ä ?ôÏùº Î∞©Ïãù"""
        _insert_bulk([r])

    for i in range(0, len(records), BATCH):
        chunk = records[i:i + BATCH]
        try:
            _insert_bulk(chunk)
            total += len(chunk)
            print(f"  INSERT {total}/{len(records)} Í±??ÑÎ£å")
        except Exception as e:
            print(f"  ??Î∞∞Ïπò INSERT ?§Ìå® ({len(chunk)}Í±?, ?åÎùºÎØ∏ÌÑ∞ Î∞îÏù∏?©ÏúºÎ°?1Í±¥Ïî© ?¨Ïãú?? {e}")
            for r in chunk:
                try:
                    _insert_one_safe(r)
                    total += 1
                except Exception as e2:
                    skip_count += 1
                    print(f"    ??Í±¥ÎÑà?Ä: {(r.get('product_name') or '')[:40]} / {e2}")
            print(f"  INSERT {total}/{len(records)} Í±??ÑÎ£å (Í±¥ÎÑà?Ä {skip_count}Í±?")
    if skip_count:
        print(f"  ??Ï¥?{skip_count}Í±?INSERT ?§Ìå®Î°?Í±¥ÎÑà?Ä")


# ?Ä?Ä portal_db import (?Ä??Î™©Î°ù DB Í¥ÄÎ¶? ?Ä?Ä?Ä?Ä?Ä?Ä?Ä?Ä?Ä?Ä?Ä?Ä?Ä?Ä?Ä?Ä?Ä?Ä?Ä?Ä?Ä?Ä?Ä?Ä?Ä?Ä?Ä?Ä?Ä?Ä?Ä?Ä?Ä?Ä
_THIS_DIR_ROOT = os.path.dirname(os.path.abspath(__file__))
_API_DIR_PATH  = os.path.join(_THIS_DIR_ROOT, "api")
if _API_DIR_PATH not in sys.path:
    sys.path.insert(0, _API_DIR_PATH)
try:
    import portal_db as _portal_db
    _DB_AVAILABLE = True
except ImportError:
    _DB_AVAILABLE = False
    print("??portal_db Î°úÎìú ?§Ìå®: ?Ä??Î™©Î°ù??DB?êÏÑú ?ΩÏùÑ ???ÜÏäµ?àÎã§.")


# ?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê
# Î∞∞Î??ÅÌöå ?¨Î°§??
# ?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê
BAEMIN_API     = "https://gw-api-mart.baemin.com/front-api/v1/sellers"
BAEMIN_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json",
    "Referer": "https://mart.baemin.com/",
    "Origin": "https://mart.baemin.com",
}
# Î∞∞Î??ÅÌöå Î∞∞ÏÜ°?Ä??Îß§Ìïë (?ÅÌíàÎ≥?goodsDeliveryType)
# DIRECT_DELIVERY ??ÏßÅÎ∞∞??(?òÏßë ?Ä??
# NORMAL_DELIVERY ???ùÎ∞∞Î∞∞ÏÜ° (?òÏßë ?úÏô∏)
BAEMIN_DELIVERY_MAP = {
    "DIRECT_DELIVERY": "ÏßÅÎ∞∞??,
    "NORMAL_DELIVERY": "?ùÎ∞∞Î∞∞ÏÜ°",   # ?òÏßë ?úÏô∏
    "PARCEL":          "?ùÎ∞∞Î∞∞ÏÜ°",   # ?òÏßë ?úÏô∏
    "FRESH":           "?àÎ≤ΩÎ∞∞ÏÜ°",
    "MARKET_DAY":      "?•ÎÇ†Î∞∞ÏÜ°",
}
# ?òÏßë ?Ä??Î∞∞ÏÜ°?†Ìòï (?ùÎ∞∞ ?úÏô∏)
BAEMIN_COLLECT_TYPES = {"ÏßÅÎ∞∞??, "?àÎ≤ΩÎ∞∞ÏÜ°", "?•ÎÇ†Î∞∞ÏÜ°"}


def _get_baemin_sellers() -> list[dict]:
    """DB?êÏÑú ?úÏÑ± Î∞∞Î? ?Ä??Î™©Î°ù Î∞òÌôò. DB ?ÜÏúºÎ©?Í∏∞Î≥∏Í∞??¨Ïö©."""
    if _DB_AVAILABLE:
        try:
            rows = _portal_db.pm_list_baemin_sellers()
            sellers = [{"id": str(r["seller_id"]), "name": r["seller_name"]}
                       for r in rows if r.get("is_active", 1)]
            if sellers:
                return sellers
        except Exception as e:
            print(f"  ??DB ?Ä??Ï°∞Ìöå ?§Ìå®: {e}")
    # ?¥Î∞±: Í∏∞Î≥∏ ?Ä??Î™©Î°ù
    return [
        {"id": "907",  "name": "?¥ÎÑà?ºÏä§"},
        {"id": "2090", "name": "Í∑∏Î°ú?∞Ïãù?êÏû¨"},
        {"id": "2089", "name": "?§Îßà?ºÌë∏??},
        {"id": "1384", "name": "?§Î¥Ñ?∏Îìú"},
        {"id": "1774", "name": "?®Íµ≠ÎØºÏã†?†Î™∞"},
        {"id": "2057", "name": "?∏ÌòÑF&B"},
        {"id": "2006", "name": "?åÎùº??},
        {"id": "2039", "name": "?ÑÎ?Í∑∏Î¶∞?∏Îìú"},
        {"id": "2005", "name": "?åÌîº??},
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
        print(f"  ??Î∞∞Î? ?îÏ≤≠ ?§Ìå® (seller={seller_id} page={page}): {e}")
        return None


def crawl_baemin(test_mode=False) -> list[dict]:
    """Î∞∞Î??ÅÌöå ?¨Î°§Îß? DIRECT_DELIVERY(ÏßÅÎ∞∞??Îß??òÏßë. ?ùÎ∞∞(NORMAL_DELIVERY) ?úÏô∏."""
    records = []
    sellers = _get_baemin_sellers()
    if test_mode:
        sellers = sellers[:1]
    print(f"Î∞∞Î??ÅÌöå ?Ä??{len(sellers)}Í∞??òÏßë ?úÏûë")

    for s in sellers:
        print(f"\n[Î∞∞Î??ÅÌöå] {s['name']} (id={s['id']})")
        first = _baemin_fetch_page(s["id"], 0)
        if not first:
            print("  ??Ï≤??òÏù¥ÏßÄ ?§Ìå®, Í±¥ÎÑà?Ä")
            continue
        total_pages = first.get("totalPages", 1) if not test_mode else 1
        seller_direct = 0
        seller_skip   = 0
        print(f"  Ï¥?{first.get('totalElements',0)}Í∞?/ {total_pages}?òÏù¥ÏßÄ")

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
                # ?ùÎ∞∞Î∞∞ÏÜ° ?úÏô∏
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

            print(f"  page {page_no}: ?òÏßë {seller_direct}Í±?/ ?úÏô∏(?ùÎ∞∞) {seller_skip}Í±?(?ÑÏ†Å {len(records)}Í±?")

    print(f"\n??Î∞∞Î??ÅÌöå ?òÏßë ?ÑÎ£å: {len(records)}Í±?)
    return records


# ?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê
# ?ùÎ¥Ñ ?¨Î°§??
# ?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê
FOOD_GQL_URL = "https://api.foodspring.co.kr/v2/graphql"
FOOD_HEADERS = {
    "Content-Type": "application/json",
    "Origin": "https://www.foodspring.co.kr",
    "Referer": "https://www.foodspring.co.kr/",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
}

# ?ùÎ¥Ñ GraphQL delivery __typename ??Î∞∞ÏÜ°?†Ìòï Îß§Ìïë
FOOD_DELIVERY_TYPENAME_MAP = {
    "DirectDelivery":    "ÏßÅÎ∞∞??,
    "AggregateDelivery": "?±Ïã±Î∞∞ÏÜ°",
}
# DB?êÏÑú ?ΩÏñ¥??delivery_type Í∞????úÏãú ?¥Î¶Ñ
FOOD_DB_DELIVERY_MAP = {
    "direct":   "ÏßÅÎ∞∞??,
    "singsing": "?±Ïã±Î∞∞ÏÜ°",
}

# ?ÅÌíà + Î∞∞ÏÜ°?Ä???ôÏãú Ï°∞Ìöå ÏøºÎ¶¨
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
    """DB?êÏÑú ?úÏÑ± ?ùÎ¥Ñ ?Ä??Î™©Î°ù Î∞òÌôò (113Í∞?. DB ?ÜÏúºÎ©?Í∏∞Ï°¥ 10Í∞??¨Ïö©."""
    if _DB_AVAILABLE:
        try:
            rows = _portal_db.pm_list_foodspring_sellers(active_only=True)
            sellers = [{"id": str(r["seller_id"]), "name": r["seller_name"],
                        "delivery_type": r.get("delivery_type", "")}
                       for r in rows]
            if sellers:
                return sellers
        except Exception as e:
            print(f"  ??DB ?ùÎ¥Ñ ?Ä??Ï°∞Ìöå ?§Ìå®: {e}")
    # ?¥Î∞±: Í∏∞Ï°¥ 10Í∞?
    return [
        {"id": "1517", "name": "CJ?ÑÎ†à?úÏõ®??,    "delivery_type": "singsing"},
        {"id": "5081", "name": "?∏Îìú???òÎèÑÍ∂?,   "delivery_type": "direct"},
        {"id": "2716", "name": "?ÑÎ?Í∑∏Î¶∞?∏Îìú",    "delivery_type": "direct"},
        {"id": "2626", "name": "?ùÏûê??19",       "delivery_type": "direct"},
        {"id": "867",  "name": "?®Íµ≠ÎØ??†ÏÑ†Î™?,   "delivery_type": "direct"},
        {"id": "4069", "name": "?îÏñ¥?∏Îìú",        "delivery_type": "direct"},
        {"id": "3038", "name": "?∏Îìú?àÏù∏",        "delivery_type": "direct"},
        {"id": "2455", "name": "?∏ÌòÑF&B",         "delivery_type": "direct"},
        {"id": "1388", "name": "?§Î¥Ñ?∏Îìú",        "delivery_type": "direct"},
        {"id": "3828", "name": "ÏºÄ?¥Ïóê?ÑÌîº(Í∞ïÎÇ®)", "delivery_type": "direct"},
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
        print(f"  ???ùÎ¥Ñ ?îÏ≤≠ ?§Ìå® (seller={seller_id}): {e}")
        return None


def crawl_foodspring(test_mode=False) -> list[dict]:
    """
    ?ùÎ¥Ñ ?¨Î°§Îß?
    - Î∞∞ÏÜ°?Ä?ÖÏùÑ GraphQL __typename?ºÎ°ú ?ôÏ†Å Í∞êÏ?: DirectDelivery?íÏßÅÎ∞∞ÏÜ°, AggregateDelivery?íÏã±?±Î∞∞??
    - Í∞êÏ???Î∞∞ÏÜ°?Ä?ÖÏùÑ portal_db???ÖÎç∞?¥Ìä∏ (?†Í∑ú ?Ä???êÎèô Î∞òÏòÅ)
    - ÏßÅÎ∞∞???±Ïã±Î∞∞ÏÜ°Îß??òÏßë (Í∏∞Ì? Î∞∞ÏÜ°?†Ìòï ?úÏô∏)
    """
    records = []
    sellers = _get_foodspring_sellers()
    if test_mode:
        sellers = sellers[:1]
    print(f"?ùÎ¥Ñ ?Ä??{len(sellers)}Í∞??òÏßë ?úÏûë")

    for s in sellers:
        seller_id = s["id"]
        print(f"\n[?ùÎ¥Ñ] seller_id={seller_id}")
        after = None
        seller_name   = s.get("name", seller_id)
        delivery_type = ""   # GraphQLÎ°?Í∞êÏ? ??Í≤∞Ï†ï
        page_no       = 0
        seller_count  = 0

        while True:
            data = _food_fetch_page(seller_id, after)
            if not data:
                break

            # Ï≤??òÏù¥ÏßÄ: ?Ä?¨Î™Ö + Î∞∞ÏÜ°?Ä??__typename Í∞êÏ?
            if page_no == 0:
                node = data.get("node") or {}
                seller_name = node.get("name", seller_id)
                total = (data.get("goodsListPC") or {}).get("totalCount", 0)

                # Î∞∞ÏÜ°?Ä???ôÏ†Å Í∞êÏ?
                typename = (node.get("delivery") or {}).get("__typename", "")
                delivery_type = FOOD_DELIVERY_TYPENAME_MAP.get(typename, "")
                if not delivery_type:
                    # DB???Ä?•Îêú Í∞??¥Î∞±
                    delivery_type = FOOD_DB_DELIVERY_MAP.get(s.get("delivery_type", ""), "ÏßÅÎ∞∞??)

                print(f"  ?Ä?¨Î™Ö: {seller_name}, Ï¥?{total}Í∞? Î∞∞ÏÜ°: {delivery_type} (typename={typename})")

                # ÎπÑÎ???Î∞∞ÏÜ°?†Ìòï?¥Î©¥ ?§ÌÇµ (?ÑÏû¨??Î™®Îëê ?Ä?ÅÏù¥ÎØÄÎ°??àÎ∞©??Ï≤òÎ¶¨)
                if delivery_type not in ("ÏßÅÎ∞∞??, "?±Ïã±Î∞∞ÏÜ°"):
                    print(f"  ???òÏßë ?úÏô∏ Î∞∞ÏÜ°?†Ìòï: {delivery_type}")
                    break

                # DB Î∞∞ÏÜ°?Ä???ÖÎç∞?¥Ìä∏ (?¨Î°§ ???êÎèô Í∞±Ïã†)
                if _DB_AVAILABLE and typename:
                    db_val = "singsing" if delivery_type == "?±Ïã±Î∞∞ÏÜ°" else "direct"
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

            print(f"  page {page_no}: {len(edges)}Í∞?(?ÑÏ†Å {seller_count}Í∞?")

            if test_mode or not page_info.get("hasNextPage"):
                break
            after = page_info.get("endCursor")
            page_no += 1
            time.sleep(0.3)

    print(f"\n???ùÎ¥Ñ ?òÏßë ?ÑÎ£å: {len(records)}Í±?)
    return records


# ?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê
# Î©îÏù∏
# ?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê?ê‚ïê
def _crawl_baemin_per_seller(test_mode: bool):
    """Î∞∞Î??ÅÌöå ?Ä?¨Î≥Ñ (seller_id, records) ?úÎÑà?àÏù¥??"""
    sellers = _get_baemin_sellers()
    if test_mode:
        sellers = sellers[:1]
    print(f"Î∞∞Î??ÅÌöå ?Ä??{len(sellers)}Í∞??òÏßë ?úÏûë")
    for s in sellers:
        print(f"\n[Î∞∞Î??ÅÌöå] {s['name']} (id={s['id']})")
        first = _baemin_fetch_page(s["id"], 0)
        if not first:
            print("  ??Ï≤??òÏù¥ÏßÄ ?§Ìå®, Í±¥ÎÑà?Ä")
            continue
        total_pages = first.get("totalPages", 1) if not test_mode else 1
        seller_records = []
        seller_direct = 0
        seller_skip   = 0
        print(f"  Ï¥?{first.get('totalElements',0)}Í∞?/ {total_pages}?òÏù¥ÏßÄ")
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
            print(f"  page {page_no}: ?òÏßë {seller_direct}Í±?/ ?úÏô∏(?ùÎ∞∞) {seller_skip}Í±?)
        yield s["id"], seller_records


def _crawl_food_per_seller(test_mode: bool, only_ids: set | None = None):
    """?ùÎ¥Ñ ?Ä?¨Î≥Ñ (seller_id, records) ?úÎÑà?àÏù¥??
    only_ids: ?òÏßë??seller_id ÏßëÌï© (None?¥Î©¥ ?ÑÏ≤¥)"""
    sellers = _get_foodspring_sellers()
    if test_mode:
        sellers = sellers[:1]
    if only_ids:
        sellers = [s for s in sellers if s["id"] in only_ids]
    print(f"?ùÎ¥Ñ ?Ä??{len(sellers)}Í∞??òÏßë ?úÏûë")
    for s in sellers:
        seller_id = s["id"]
        print(f"\n[?ùÎ¥Ñ] seller_id={seller_id}")
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
                    delivery_type = FOOD_DB_DELIVERY_MAP.get(s.get("delivery_type", ""), "ÏßÅÎ∞∞??)
                print(f"  ?Ä?¨Î™Ö: {seller_name}, Ï¥?{total}Í∞? Î∞∞ÏÜ°: {delivery_type}")
                if delivery_type not in ("ÏßÅÎ∞∞??, "?±Ïã±Î∞∞ÏÜ°"):
                    print(f"  ???òÏßë ?úÏô∏ Î∞∞ÏÜ°?†Ìòï: {delivery_type}")
                    break
                if _DB_AVAILABLE and typename:
                    db_val = "singsing" if delivery_type == "?±Ïã±Î∞∞ÏÜ°" else "direct"
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
            print(f"  page {page_no}: {len(edges)}Í∞?(?ÑÏ†Å {len(seller_records)}Í∞?")
            if test_mode or not page_info.get("hasNextPage"):
                break
            after = page_info.get("endCursor")
            page_no += 1
            time.sleep(0.3)
        yield seller_id, seller_records


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--test",    action="store_true", help="?Ä??1Í∞? 1?òÏù¥ÏßÄÎß?)
    parser.add_argument("--baemin",  action="store_true", help="Î∞∞Î??ÅÌöåÎß?)
    parser.add_argument("--food",    action="store_true", help="?ùÎ¥ÑÎß?)
    parser.add_argument("--seller",  type=str, default="",
                        help="?πÏ†ï ?Ä?¨Îßå ?¨ÏàòÏß?(?ºÌëúÍµ¨Î∂Ñ, ?? foodspring/1388,foodspring/5081)")
    parser.add_argument("--cleanup", action="store_true",
                        help="Í∏∞Ï°¥ Î∞∞Î? ?ùÎ∞∞(NORMAL_DELIVERY/?ùÎ∞∞Î∞∞ÏÜ°) ?∞Ïù¥????†úÎß??§Ìñâ")
    parser.add_argument("--delete-date", type=str, default="",
                        help="?πÏ†ï ?†Ïßú ?∞Ïù¥???ÑÏ≤¥ ??†ú ??Ï¢ÖÎ£å (?? --delete-date 2026-08-18)")
    args = parser.parse_args()

    # --seller ?åÏã±: {"baemin": {"1234",...}, "foodspring": {"1388","5081"}}
    seller_filter: dict[str, set] = {}
    if args.seller:
        for item in args.seller.split(","):
            item = item.strip()
            if "/" in item:
                pf, sid = item.split("/", 1)
                seller_filter.setdefault(pf.strip(), set()).add(sid.strip())
            else:
                # ?åÎû´???ÜÏù¥ IDÎß??ÖÎ†•?òÎ©¥ ?ùÎ¥Ñ?ºÎ°ú Í∞ÑÏ£º
                seller_filter.setdefault("foodspring", set()).add(item)
        print(f"[?Ä???ÑÌÑ∞] {seller_filter}")

    today = datetime.date.today().isoformat()
    print(f"{'='*60}")
    print(f"?åÎû´??Í∞ÄÍ≤??¨Î°§???úÏûë (crawl_date={today})")
    if args.test:
        print("TEST MODE: ?Ä??1Í∞?/ 1?òÏù¥ÏßÄÎß?)
    print(f"{'='*60}")

    # Databricks ?∞Í≤∞ (?¨Î°§Îß??ÑÏóê Î®ºÏ? ?ïÏù∏)
    print(f"\nDatabricks ?∞Í≤∞ Ï§?..")
    try:
        conn = _get_conn()
        print("  ???∞Í≤∞ ?±Í≥µ")
    except Exception as e:
        print(f"  ???∞Í≤∞ ?§Ìå®: {e}")
        return

    # ?åÏù¥Î∏??ùÏÑ± ?ïÏù∏
    _exec(conn, DDL_CREATE)

    # --cleanup: Í∏∞Ï°¥ NORMAL_DELIVERY/?ùÎ∞∞Î∞∞ÏÜ° ?∞Ïù¥???ïÎ¶¨ ??Ï¢ÖÎ£å
    if args.cleanup:
        print("\n[?ïÎ¶¨] Í∏∞Ï°¥ Î∞∞Î? ?ùÎ∞∞Î∞∞ÏÜ° ?∞Ïù¥????†ú...")
        try:
            with conn.cursor() as cur:
                cur.execute(f"DELETE FROM {T_SILVER} WHERE platform='baemin' AND delivery_type='NORMAL_DELIVERY'")
                print("  ??NORMAL_DELIVERY rawÍ∞???†ú ?ÑÎ£å")
                cur.execute(f"DELETE FROM {T_SILVER} WHERE platform='baemin' AND delivery_type='?ùÎ∞∞Î∞∞ÏÜ°'")
                print("  ???ùÎ∞∞Î∞∞ÏÜ° ??†ú ?ÑÎ£å")
        except Exception as e:
            print(f"  ???ïÎ¶¨ ?§Ìå®: {e}")
        conn.close()
        return

    # --delete-date: ?πÏ†ï ?†Ïßú ?∞Ïù¥???ÑÏ≤¥ ??†ú ??Ï¢ÖÎ£å
    if args.delete_date:
        d = args.delete_date.strip()
        print(f"\n[??†ú] {d} ?†Ïßú ?∞Ïù¥???ÑÏ≤¥ ??†ú Ï§?..")
        try:
            _exec(conn, f"DELETE FROM {T_SILVER} WHERE crawl_date = '{d}'")
            print(f"  ??{d} ?∞Ïù¥????†ú ?ÑÎ£å")
        except Exception as e:
            print(f"  ????†ú ?§Ìå®: {e}")
        conn.close()
        return
        conn.close()
        return

    # ?§Îäò ?†Ïßú Í∏∞Ï°¥ ?∞Ïù¥????†ú (--seller ?µÏÖò ???¥Îãπ ?Ä?¨Îßå ??†ú)
    print(f"\nÍ∏∞Ï°¥ {today} ?∞Ïù¥????†ú...")
    if seller_filter:
        for pf, sids in seller_filter.items():
            ids_str = ",".join(f"'{s}'" for s in sids)
            _exec(conn, f"DELETE FROM {T_SILVER} WHERE crawl_date='{today}' AND platform='{pf}' AND platform_seller_id IN ({ids_str})")
            print(f"  ??{pf} ?Ä??{sids} Í∏∞Ï°¥ ?∞Ïù¥????†ú")
    else:
        if not args.food:
            _exec(conn, f"DELETE FROM {T_SILVER} WHERE crawl_date='{today}' AND platform='baemin'")
        if not args.baemin:
            _exec(conn, f"DELETE FROM {T_SILVER} WHERE crawl_date='{today}' AND platform='foodspring'")
    print("  ????†ú ?ÑÎ£å")

    # ?Ä?¨Î≥Ñ ?¨Î°§Îß?+ Ï¶âÏãú ?Ä??(Î©îÎ™®Î¶¨Ïóê ?ìÏ? ?äÏùå)
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
                print(f"  ??Î∞∞Î? {seller_id}: {len(records)}Í±??Ä??(?ÑÏ†Å {total_saved}Í±?")
            except Exception as e:
                print(f"  ??Î∞∞Î? {seller_id} ?Ä???§Ìå®: {e}")
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
                print(f"  ???ùÎ¥Ñ {seller_id}: {len(records)}Í±??Ä??(?ÑÏ†Å {total_saved}Í±?")
            except Exception as e:
                print(f"  ???ùÎ¥Ñ {seller_id} ?Ä???§Ìå®: {e}")
                failed_sellers.append(f"foodspring/{seller_id}")

    conn.close()

    print(f"\n{'='*60}")
    print(f"???ÑÎ£å! Ï¥?{total_saved:,}Í±??Ä??({today})")
    if failed_sellers:
        print(f"???§Ìå® ?Ä?? {', '.join(failed_sellers)}")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
#   s y n c   t e s t 
 

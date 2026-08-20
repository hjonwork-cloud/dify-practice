"""오늘 배민 데이터 수집 여부 확인. 수집됐으면 count 출력, 없으면 0 출력."""
import sys, os, warnings
warnings.filterwarnings('ignore')

today = sys.argv[1] if len(sys.argv) > 1 else ""
if not today:
    print(0); sys.exit(0)

try:
    import databricks.sql
    conn = databricks.sql.connect(
        server_hostname=os.environ['DATABRICKS_HOST'].replace('https://',''),
        http_path=os.environ['DATABRICKS_HTTP_PATH'],
        access_token=os.environ['DATABRICKS_TOKEN']
    )
    cur = conn.cursor()
    cur.execute(
        "SELECT COUNT(1) FROM h_hmfo_fsi_dm.gd_rst_ing.dim_platform_products "
        f"WHERE platform='baemin' AND crawl_date='{today}'"
    )
    cnt = cur.fetchone()[0]
    conn.close()
    print(cnt)
except Exception as e:
    print(f"ERROR: {e}", file=sys.stderr)
    print(0)

"""식봄 크롤링 완료 메일 발송.
Usage: python _foodspring_send_mail.py <crawl_date> <log_file> <elapsed_sec>
"""
import sys, os, json, warnings
warnings.filterwarnings('ignore')

crawl_date  = sys.argv[1] if len(sys.argv) > 1 else ""
log_file    = sys.argv[2] if len(sys.argv) > 2 else ""
elapsed_sec = float(sys.argv[3]) if len(sys.argv) > 3 else 0

# .env.local 로드
env_file = os.path.join(os.path.dirname(__file__), '.env.local')
if os.path.exists(env_file):
    for line in open(env_file, encoding='utf-8'):
        line = line.strip()
        if '=' in line and not line.startswith('#'):
            k, v = line.split('=', 1)
            os.environ[k.strip()] = v.strip()

sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'api'))

log_dir      = os.path.join(os.path.dirname(__file__), 'logs')
summary_path = os.path.join(log_dir, f'summary_{crawl_date}.json')

if os.path.exists(summary_path):
    with open(summary_path, encoding='utf-8') as f:
        data = json.load(f)
    seller_summary = [s for s in data.get('seller_summary', []) if s.get('platform') == 'foodspring']
    report = {
        'crawl_date':     crawl_date,
        'total_saved':    data.get('food_count', 0),
        'baemin_count':   0,
        'food_count':     data.get('food_count', 0),
        'seller_summary': seller_summary,
        'failed_sellers': [f for f in data.get('failed_sellers', []) if 'foodspring' in f],
        'duration_sec':   elapsed_sec,
        'stderr':         '',
    }
else:
    report = {
        'crawl_date': crawl_date, 'total_saved': 0, 'baemin_count': 0,
        'food_count': 0, 'seller_summary': [], 'failed_sellers': [],
        'duration_sec': elapsed_sec,
        'stderr': f'[경고] summary JSON 없음: {summary_path}',
    }

try:
    from api.crawl_mailer import send_foodspring_report
    ok = send_foodspring_report(report)
    print('메일 발송:', '성공' if ok else '실패 (SMTP 설정 확인)')
except Exception as e:
    print(f'메일 발송 오류: {e}')

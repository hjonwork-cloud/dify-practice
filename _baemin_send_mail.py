"""배민 크롤링 완료 메일 발송.
Usage: python _baemin_send_mail.py <crawl_date> <log_file> <elapsed_sec>
"""
import sys, os, re, warnings
warnings.filterwarnings('ignore')

crawl_date   = sys.argv[1] if len(sys.argv) > 1 else ""
log_file     = sys.argv[2] if len(sys.argv) > 2 else ""
elapsed_sec  = float(sys.argv[3]) if len(sys.argv) > 3 else 0

# .env.local 로드
env_file = os.path.join(os.path.dirname(__file__), '.env.local')
if os.path.exists(env_file):
    for line in open(env_file, encoding='utf-8'):
        line = line.strip()
        if '=' in line and not line.startswith('#'):
            k, v = line.split('=', 1)
            os.environ[k.strip()] = v.strip()

sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'api'))

log_content = ""
if log_file and os.path.exists(log_file):
    log_content = open(log_file, encoding='utf-8', errors='replace').read()

# 셀러 요약 파싱
seller_summary = []
for m in re.finditer(r'✓ 배민 (\S+): (\d+)건 저장', log_content):
    seller_summary.append({
        'platform': 'baemin', 'seller_id': m.group(1),
        'seller_name': m.group(1), 'count': int(m.group(2))
    })

total_m = re.search(r'총 ([\d,]+)건 저장', log_content)
total   = int(total_m.group(1).replace(',','')) if total_m else sum(s['count'] for s in seller_summary)

report = {
    'crawl_date':     crawl_date,
    'total_saved':    total,
    'baemin_count':   total,
    'food_count':     0,
    'seller_summary': seller_summary,
    'failed_sellers': [],
    'duration_sec':   elapsed_sec,
    'stderr':         '',
}

try:
    from crawl_mailer import send_report
    ok = send_report(report)
    print('메일 발송:', '성공' if ok else '실패 (SMTP 설정 확인)')
except Exception as e:
    print(f'메일 발송 오류: {e}')

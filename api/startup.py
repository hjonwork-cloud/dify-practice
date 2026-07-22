"""
Azure App Service 시작 시 /home/data 디렉토리와 JSON 파일을 초기화합니다.
서버 재시작 후에도 /home 은 영구 스토리지로 유지됩니다.
"""
import os, json

DATA_DIR = os.getenv("DATA_DIR", "/home/data")
os.makedirs(DATA_DIR, exist_ok=True)

_defaults = {
    os.path.join(DATA_DIR, "_registered_users.json"):     {},
    os.path.join(DATA_DIR, "_admin_whitelist.json"):       {},
    os.path.join(DATA_DIR, "_admin_team_overrides.json"):  {},
    os.path.join(DATA_DIR, "_token_usage.json"):           {"logs": []},
    os.path.join(DATA_DIR, "_seed_done.json"):             {},
}
_defaults_list = {
    os.path.join(DATA_DIR, "_admin_blacklist.json"): [],
}

for path, default in _defaults.items():
    if not os.path.exists(path):
        with open(path, "w", encoding="utf-8") as f:
            json.dump(default, f, ensure_ascii=False)

for path, default in _defaults_list.items():
    if not os.path.exists(path):
        with open(path, "w", encoding="utf-8") as f:
            json.dump(default, f, ensure_ascii=False)

# 베타테스터 초기화 (파일 없을 때만)
_beta_file = os.path.join(DATA_DIR, "_beta_testers.json")
if not os.path.exists(_beta_file):
    _beta = {
        "20210054": {"name": "최희조",   "team": "외식식재사업부", "role": "admin"},
        "20065782": {"name": "권봉주",   "team": "외식3팀"},
        "20151013": {"name": "김준호",   "team": "외식3팀"},
        "20190811": {"name": "김다복솔", "team": "외식2팀"},
        "20190805": {"name": "김진한",   "team": "외식1팀"},
        "20151017": {"name": "서경일",   "team": "외식1팀"},
        "20135653": {"name": "김동영",   "team": "영남지점"},
        "20220836": {"name": "박성우",   "team": "외식1팀"},
        "20210727": {"name": "신해민",   "team": "외식2팀"},
        "20210722": {"name": "정준기",   "team": "외식3팀"},
        "20180578": {"name": "이기범",   "team": "외식3팀"},
        "20250633": {"name": "이준혁",   "team": "외식1팀"},
        "20230719": {"name": "안담경",   "team": "외식2팀"},
        "20230720": {"name": "이충규",   "team": "외식3팀"},
        "20220822": {"name": "김현진",   "team": "영남지점"},
        "20230724": {"name": "임주원",   "team": "외식1팀"},
    }
    with open(_beta_file, "w", encoding="utf-8") as f:
        json.dump(_beta, f, ensure_ascii=False, indent=2)

print(f"[startup] DATA_DIR={DATA_DIR} 초기화 완료")

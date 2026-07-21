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

print(f"[startup] DATA_DIR={DATA_DIR} 초기화 완료")

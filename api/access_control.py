"""AI 영업지원 챗봇/세일즈 액션 플랫폼 공통 접근제어."""
from __future__ import annotations

import json
import os
import time
from datetime import date, datetime
from pathlib import Path


AUTH_DEPT = "외식식재사업부"
ADMIN_EMP_CODE = "20210054"
ADMIN_EMP_NAME = "최희조"
ADMIN_TEAM = AUTH_DEPT

DATA_DIR = Path(os.getenv("CHATBOT_DATA_DIR", os.getenv("DATA_DIR", r"E:\data\chatbot")))
DATA_DIR.mkdir(parents=True, exist_ok=True)
BETA_TESTERS_FILE = DATA_DIR / "_beta_testers.json"

# 2026-08-03 00:00부터 외식식재사업부 전체 오픈.
OPEN_DATE = date.fromisoformat(os.getenv("SALES_AI_OPEN_DATE", "2026-08-03"))

# 베타테스터 10명 확정 전 기본 운영 안전장치: 관리자와 기존 대표 테스트 계정만 허용.
_SEED_BETA_TESTERS: dict[str, dict] = {
    ADMIN_EMP_CODE: {"name": ADMIN_EMP_NAME, "team": ADMIN_TEAM, "role": "admin"},
    "20230720": {"name": "이충규", "team": "외식3팀", "role": "beta"},
    "20250629": {"name": "서용산", "team": "신규개발파트", "role": "beta"},
    "20151017": {"name": "서경일", "team": "신규개발파트", "role": "beta"},
}


def _today() -> date:
    raw = os.getenv("SALES_AI_TODAY", "").strip()
    if raw:
        return date.fromisoformat(raw)
    return datetime.now().date()


def beta_gate_active() -> bool:
    """True면 베타테스터/관리자만 접근 가능."""
    return _today() < OPEN_DATE


def _read_json(path: Path, default):
    try:
        if path.exists():
            with path.open("r", encoding="utf-8") as f:
                data = json.load(f)
            return data if data else default
    except (OSError, ValueError):
        return default
    return default


def _write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def load_beta_testers() -> dict[str, dict]:
    """베타테스터 목록. {emp_code: {name, team, role}}"""
    data = _read_json(BETA_TESTERS_FILE, None)
    if data is None:
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        data = {emp: {**info, "added_at": now} for emp, info in _SEED_BETA_TESTERS.items()}
        _write_json(BETA_TESTERS_FILE, data)
    env_codes = [x.strip() for x in os.getenv("SALES_AI_BETA_EMP_CODES", "").split(",") if x.strip()]
    for code in env_codes:
        data.setdefault(code, {"name": "", "team": "", "role": "beta", "added_at": "env"})
    data.setdefault(ADMIN_EMP_CODE, {"name": ADMIN_EMP_NAME, "team": ADMIN_TEAM, "role": "admin", "added_at": "seed"})
    # 항상 seed 항목이 누락 없이 유지되도록 보장
    for _emp, _info in _SEED_BETA_TESTERS.items():
        data.setdefault(_emp, {**_info, "added_at": "seed"})
    return data


def is_admin_emp(emp_code: str) -> bool:
    return str(emp_code or "").strip() == ADMIN_EMP_CODE


def beta_access_allowed(emp_code: str) -> bool:
    """8/3 전에는 베타테스터와 관리자만, 8/3부터는 상위 화이트리스트 정책에 위임."""
    code = str(emp_code or "").strip()
    if not code:
        return False
    if not beta_gate_active():
        return True
    return is_admin_emp(code) or code in load_beta_testers()


def beta_denied_message(service_name: str = "AI 영업지원 서비스") -> str:
    return (
        f"현재 {service_name}는 베타테스트 기간으로,\n"
        "선정된 베타테스터만 이용 가능합니다.\n\n"
        "전체 오픈은 2026년 8월 3일부터 예정되어 있습니다."
    )

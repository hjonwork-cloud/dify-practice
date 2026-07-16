"""내부 관리자 웹 콘솔 라우터."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import time
from pathlib import Path
from urllib.parse import parse_qs, quote

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape

import admin_db

router = APIRouter(prefix="/admin", tags=["admin-console"])
_TEMPLATES_DIR = Path(__file__).parent / "templates"
_STATIC_DIR = Path(__file__).parent / "static"
_jinja_env = Environment(
    loader=FileSystemLoader(str(_TEMPLATES_DIR)),
    autoescape=select_autoescape(("html", "xml")),
)


def _asset_version() -> str:
    """정적 파일(admin.js/admin.css) 최신 수정시각으로 캐시 무효화 버전 생성."""
    try:
        mtimes = [
            (_STATIC_DIR / "admin.js").stat().st_mtime,
            (_STATIC_DIR / "admin.css").stat().st_mtime,
        ]
        return str(int(max(mtimes)))
    except OSError:
        return "1"
_USERS_FILE = Path(os.getenv("CHATBOT_DATA_DIR", r"E:\data\chatbot")) / "_registered_users.json"
_SESSION_COOKIE = "dongwon_admin_session"
_CSRF_COOKIE = "dongwon_admin_csrf"
_SESSION_MAX_AGE = 60 * 60 * 8
_SESSION_SECRET = os.getenv("ADMIN_SESSION_SECRET", "dongwon-admin-dev-secret-change-me")


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _sign(payload: str) -> str:
    return hmac.new(_SESSION_SECRET.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()


def _make_session(admin_id: str) -> str:
    exp = int(time.time()) + _SESSION_MAX_AGE
    nonce = secrets.token_urlsafe(12)
    payload = f"{admin_id}|{exp}|{nonce}"
    return f"{_b64(payload.encode('utf-8'))}.{_sign(payload)}"


def _read_session(token: str | None) -> str | None:
    if not token or "." not in token:
        return None
    payload_b64, sig = token.rsplit(".", 1)
    try:
        payload = base64.urlsafe_b64decode(payload_b64 + "=" * (-len(payload_b64) % 4)).decode("utf-8")
        admin_id, exp_raw, _nonce = payload.split("|", 2)
        if int(exp_raw) < int(time.time()):
            return None
        if not secrets.compare_digest(sig, _sign(payload)):
            return None
        return admin_id
    except Exception:
        return None


async def _read_form(request: Request) -> dict[str, str]:
    raw = (await request.body()).decode("utf-8", errors="replace")
    parsed = parse_qs(raw, keep_blank_values=True)
    return {key: values[-1] if values else "" for key, values in parsed.items()}


def _csrf(request: Request) -> str:
    return request.cookies.get(_CSRF_COOKIE) or secrets.token_urlsafe(32)


def _current_admin(request: Request) -> dict | None:
    admin_id = _read_session(request.cookies.get(_SESSION_COOKIE))
    return admin_db.get_admin(admin_id) if admin_id else None


def _require_admin(request: Request, allowed_roles: tuple[str, ...] | None = None) -> dict:
    admin = _current_admin(request)
    if not admin:
        raise HTTPException(status_code=401, detail="로그인이 필요합니다.")
    if allowed_roles and admin["role"] not in allowed_roles:
        raise HTTPException(status_code=403, detail="권한이 없습니다.")
    return admin


def _render(request: Request, name: str, **context) -> HTMLResponse:
    admin = _current_admin(request)
    csrf_token = _csrf(request)
    html = _jinja_env.get_template(name).render({"request": request, "admin": admin, "csrf_token": csrf_token, "asset_v": _asset_version(), **context})
    response = HTMLResponse(html)
    response.set_cookie(_CSRF_COOKIE, csrf_token, max_age=_SESSION_MAX_AGE, httponly=False, samesite="lax")
    return response


def _redirect(path: str) -> RedirectResponse:
    return RedirectResponse(path, status_code=303)


def _delete_auth_cookies(response: RedirectResponse) -> RedirectResponse:
    response.delete_cookie(_SESSION_COOKIE)
    response.delete_cookie(_CSRF_COOKIE)
    return response


def _load_users() -> dict:
    if not _USERS_FILE.exists():
        return {}
    try:
        return json.loads(_USERS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _valid_csrf(request: Request, form: dict[str, str]) -> bool:
    return secrets.compare_digest(form.get("csrf_token", ""), _csrf(request))


def _redirect_msg(path: str, **params: str) -> RedirectResponse:
    if params:
        query = "&".join(f"{key}={quote(str(value))}" for key, value in params.items())
        path = f"{path}?{query}"
    return _redirect(path)


# ─── 조직원(외식식재사업부) 목록 캐시 ──────────────────────────
_ORG_CACHE: dict = {"ts": 0.0, "rows": []}
_ORG_CACHE_TTL = 600


def _fetch_org_members() -> list[dict]:
    """매출 데이터에서 사업부 조직원(사번·이름·소속)을 조회한다. 10분 캐시.
    최근 3개월 내 매출액이 있는 영업사원만 로드한다.
    """
    if _ORG_CACHE["rows"] and (time.time() - _ORG_CACHE["ts"]) < _ORG_CACHE_TTL:
        return _ORG_CACHE["rows"]
    import logging
    import datetime as _dt
    import main
    # 최근 3개월 년월(yyyyMM) 목록 (당월 포함 직전 3개월)
    _today = _dt.date.today()
    _months = []
    _y, _m = _today.year, _today.month
    for _ in range(3):
        _months.append(f"{_y:04d}{_m:02d}")
        _m -= 1
        if _m == 0:
            _m = 12
            _y -= 1
    _month_in = ", ".join(f"'{ym}'" for ym in _months)
    try:
        rows = main._safe_query(f"""
            SELECT DISTINCT `영업사원` AS emp_code, `영업사원명` AS name, `부서명` AS team
            FROM {main.T_MAIN}
            WHERE `사업부명` = '{main.AUTH_DEPT}'
              AND `영업사원` IS NOT NULL AND `영업사원` <> ''
              AND `영업사원명` IS NOT NULL AND `영업사원명` <> ''
              AND `년월` IN ({_month_in})
              AND `매출액` IS NOT NULL AND `매출액` <> 0
            ORDER BY team, name
        """)
        members = [
            {"emp_code": str(r.get("emp_code", "")).strip(), "name": (r.get("name") or "").strip(), "team": (r.get("team") or "").strip()}
            for r in rows if str(r.get("emp_code", "")).strip()
        ]
        _ORG_CACHE["rows"] = members
        _ORG_CACHE["ts"] = time.time()
        return members
    except Exception as exc:
        logging.getLogger("admin_router").warning(f"[users] 조직원 조회 실패: {exc}")
        return _ORG_CACHE["rows"]


def _load_usage_stats() -> dict:
    """토큰 사용량 로그를 사용자별·일별로 집계한다."""
    from collections import defaultdict
    import main
    path = getattr(main, "_USAGE_FILE", "")
    empty = {"users": [], "days": [], "total_calls": 0, "total_tokens": 0, "llm_calls": 0}
    if not path or not os.path.exists(path):
        return empty
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return empty
    logs = data.get("logs", [])
    users: dict = defaultdict(lambda: {"name": "", "calls": 0, "tokens": 0, "llm": 0, "last": ""})
    days: dict = defaultdict(lambda: {"calls": 0, "tokens": 0})
    total_calls = total_tokens = llm_calls = 0
    for entry in logs:
        uid = entry.get("user_id", "")
        row = users[uid]
        row["name"] = entry.get("user_name", uid) or uid
        row["calls"] += 1
        tokens = int(entry.get("total_tokens", 0) or 0)
        row["tokens"] += tokens
        if entry.get("dify"):
            row["llm"] += 1
            llm_calls += 1
        ts = entry.get("ts", "")
        if ts > row["last"]:
            row["last"] = ts
        day = entry.get("date", "")
        days[day]["calls"] += 1
        days[day]["tokens"] += tokens
        total_calls += 1
        total_tokens += tokens
    user_rows = sorted(users.values(), key=lambda x: x["calls"], reverse=True)
    day_rows = [{"date": d, **days[d]} for d in sorted(days.keys()) if d][-14:]
    return {"users": user_rows, "days": day_rows, "total_calls": total_calls, "total_tokens": total_tokens, "llm_calls": llm_calls}


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    if _current_admin(request):
        return _redirect("/admin")
    # 이미 DB에 활성 관리자 계정이 있거나, 최초 부트스트랩용 환경변수가 설정돼 있으면 로그인 가능
    configured = any(a.get("active") for a in admin_db.list_admins()) or bool(
        os.getenv("ADMIN_CONSOLE_USERNAME") and os.getenv("ADMIN_CONSOLE_PASSWORD")
    )
    return _render(request, "admin_login.html", configured=configured, error="")


@router.post("/login", response_class=HTMLResponse)
async def login(request: Request):
    form = await _read_form(request)
    if not secrets.compare_digest(form.get("csrf_token", ""), _csrf(request)):
        return _render(request, "admin_login.html", configured=True, error="보안 토큰이 만료되었습니다. 다시 시도해주세요.")
    account = admin_db.authenticate(form.get("username", "").strip(), form.get("password", ""))
    if not account:
        return _render(request, "admin_login.html", configured=True, error="아이디 또는 비밀번호가 올바르지 않습니다.")
    response = _redirect("/admin")
    response.set_cookie(
        _SESSION_COOKIE,
        _make_session(account["admin_id"]),
        max_age=_SESSION_MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=os.getenv("ADMIN_SESSION_HTTPS_ONLY", "false").lower() == "true",
    )
    response.set_cookie(_CSRF_COOKIE, secrets.token_urlsafe(32), max_age=_SESSION_MAX_AGE, httponly=False, samesite="lax")
    return response


@router.post("/logout")
async def logout(request: Request):
    form = await _read_form(request)
    response = _redirect("/admin/login")
    if secrets.compare_digest(form.get("csrf_token", ""), _csrf(request)):
        return _delete_auth_cookies(response)
    return response


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    _require_admin(request)
    return _render(request, "admin_dashboard.html", metrics=admin_db.dashboard_metrics())


@router.get("/voc", response_class=HTMLResponse)
async def voc_list(request: Request, status: str = "", category: str = "", q: str = ""):
    _require_admin(request)
    cases = admin_db.list_voc_cases(status=status, category=category, keyword=q)
    return _render(request, "admin_voc_list.html", cases=cases, status=status, category=category, query=q, statuses=admin_db.VOC_STATUSES, categories=admin_db.VOC_CATEGORIES)


@router.get("/voc/{case_id}", response_class=HTMLResponse)
async def voc_detail(request: Request, case_id: str):
    _require_admin(request)
    case = admin_db.get_voc_case(case_id)
    if not case:
        raise HTTPException(status_code=404, detail="VOC를 찾을 수 없습니다.")
    return _render(request, "admin_voc_detail.html", case=case, admins=admin_db.list_admins(), statuses=admin_db.VOC_STATUSES, categories=admin_db.VOC_CATEGORIES)


@router.post("/voc/{case_id}")
async def voc_update(
    request: Request,
    case_id: str,
):
    admin = _require_admin(request, ("system_admin", "voc_operator"))
    form = await _read_form(request)
    if not secrets.compare_digest(form.get("csrf_token", ""), _csrf(request)):
        raise HTTPException(status_code=403, detail="보안 토큰이 유효하지 않습니다.")
    admin_db.update_voc_case(
        case_id,
        admin["admin_id"],
        category=form.get("category", "미분류"),
        status=form.get("status", "new"),
        priority=form.get("priority", "normal"),
        owner_admin_id=form.get("owner_admin_id", ""),
        public_answer=form.get("public_answer", ""),
        internal_note=form.get("internal_note", ""),
        answer_type=form.get("answer_type", ""),
    )
    return _redirect(f"/admin/voc/{case_id}?saved=1")


@router.post("/voc/{case_id}/publish")
async def voc_publish(request: Request, case_id: str):
    admin = _require_admin(request, ("system_admin", "voc_operator"))
    form = await _read_form(request)
    if not secrets.compare_digest(form.get("csrf_token", ""), _csrf(request)):
        raise HTTPException(status_code=403, detail="보안 토큰이 유효하지 않습니다.")
    try:
        admin_db.publish_voc_as_faq(case_id, admin["admin_id"], form.get("patterns", "").splitlines())
    except ValueError as exc:
        return _redirect(f"/admin/voc/{case_id}?error={exc}")
    return _redirect(f"/admin/voc/{case_id}?published=1")


@router.get("/faq", response_class=HTMLResponse)
async def faq_list(request: Request):
    _require_admin(request)
    return _render(request, "admin_faq_list.html", faqs=admin_db.list_faqs())


@router.get("/users", response_class=HTMLResponse)
async def users(request: Request):
    _require_admin(request, ("system_admin",))
    import main
    registered = main._load_users()
    whitelist = main._load_whitelist()
    blacklist = list(main._load_blacklist())

    reg_by_emp: dict = {}
    registered_users: list = []
    for uid, info in registered.items():
        row = {**info, "kakao_id": uid}
        registered_users.append(row)
        emp = str(info.get("emp_code", "")).strip()
        if emp:
            reg_by_emp[emp] = row

    bl_set = {str(x).strip() for x in blacklist}
    wl_set = {str(x).strip() for x in whitelist.keys()}
    overrides = main._load_team_overrides()
    org = _fetch_org_members()

    name_by_emp = {m["emp_code"]: m["name"] for m in org}
    for emp, entry in whitelist.items():
        name_by_emp.setdefault(str(emp).strip(), entry.get("name", ""))
    for emp, row in reg_by_emp.items():
        name_by_emp.setdefault(emp, row.get("name", ""))

    members = [
        {**m, "team": overrides.get(m["emp_code"], m["team"]),
         "registered": m["emp_code"] in reg_by_emp, "whitelisted": m["emp_code"] in wl_set, "blacklisted": m["emp_code"] in bl_set}
        for m in org
    ]
    # 최근 3개월 매출이 없어 조직 조회에 안 잡히는 화이트리스트 관리자도 목록에 포함
    org_emps = {m["emp_code"] for m in org}
    for emp, entry in whitelist.items():
        emp = str(emp).strip()
        if emp and emp not in org_emps:
            members.append({
                "emp_code": emp,
                "name": entry.get("name", ""),
                "team": overrides.get(emp, entry.get("team", "")),
                "registered": emp in reg_by_emp,
                "whitelisted": True,
                "blacklisted": emp in bl_set,
            })
    blacklist_rows = [{"emp_code": str(ec).strip(), "name": name_by_emp.get(str(ec).strip(), "")} for ec in blacklist]

    # (이름·사번) 동일 항목 중복 제거 — 조직 조회와 화이트리스트가 겹치는 경우 하나만 남긴다
    _seen: set = set()
    _deduped: list = []
    for m in members:
        key = (str(m.get("name", "")).strip(), str(m.get("emp_code", "")).strip())
        if key in _seen:
            continue
        _seen.add(key)
        _deduped.append(m)
    members = _deduped

    return _render(
        request, "admin_users.html",
        members=members, registered_users=registered_users,
        registered_count=len(registered_users), blacklist_rows=blacklist_rows,
        admins=admin_db.list_admins(), team_options=main.TEAM_OPTIONS,
    )


@router.post("/users/add")
async def users_add(request: Request):
    admin = _require_admin(request, ("system_admin",))
    form = await _read_form(request)
    if not _valid_csrf(request, form):
        raise HTTPException(status_code=403, detail="보안 토큰이 유효하지 않습니다.")
    import main
    name = form.get("name", "").strip()
    emp = form.get("emp_code", "").strip()
    team = form.get("team", "").strip()
    if not name or not re.fullmatch(r"\d{6,10}", emp):
        return _redirect_msg("/admin/users", error="이름과 6~10자리 사번을 입력하세요.")
    if emp in {str(x).strip() for x in main._load_blacklist()}:
        return _redirect_msg("/admin/users", error="블랙리스트에 등록된 사번입니다.")
    if main._find_user_by_emp_code(emp):
        return _redirect_msg("/admin/users", error="이미 등록된 사용자입니다.")
    if not team:
        try:
            rows = main._safe_query(
                f"SELECT DISTINCT `지점명` FROM {main.T_MAIN} WHERE `사업부명`='{main.AUTH_DEPT}' AND `영업사원`='{emp}' LIMIT 1"
            )
            if rows:
                team = (rows[0].get("지점명") or "").strip()
        except Exception:
            team = ""
    with main._users_lock:
        wl = main._load_whitelist()
        wl[emp] = {"name": name, "team": team, "added_at": time.strftime("%Y-%m-%d %H:%M:%S")}
        main._save_whitelist(wl)
    admin_db.record_audit(admin["admin_id"], "user_whitelisted", "chatbot_user", emp, None, {"name": name, "team": team})
    return _redirect_msg("/admin/users", added="1")


@router.post("/users/cancel")
async def users_cancel(request: Request):
    admin = _require_admin(request, ("system_admin",))
    form = await _read_form(request)
    if not _valid_csrf(request, form):
        raise HTTPException(status_code=403, detail="보안 토큰이 유효하지 않습니다.")
    import main
    emp = form.get("emp_code", "").strip()
    name = form.get("name", "").strip()
    if not re.fullmatch(r"\d{6,10}", emp):
        return _redirect_msg("/admin/users", error="올바른 사번을 입력하세요.")
    with main._users_lock:
        users_all = main._load_users()
        del_uid = next((uid for uid, info in users_all.items() if str(info.get("emp_code", "")).strip() == emp), None)
        if del_uid:
            users_all.pop(del_uid, None)
            main._save_users(users_all)
        wl = main._load_whitelist()
        if emp in wl:
            wl.pop(emp, None)
            main._save_whitelist(wl)
        bl = main._load_blacklist()
        if emp not in bl:
            bl.append(emp)
            main._save_blacklist(bl)
    admin_db.record_audit(admin["admin_id"], "user_cancelled", "chatbot_user", emp, None, {"name": name})
    return _redirect_msg("/admin/users", canceled="1")


@router.post("/users/unblock")
async def users_unblock(request: Request):
    admin = _require_admin(request, ("system_admin",))
    form = await _read_form(request)
    if not _valid_csrf(request, form):
        raise HTTPException(status_code=403, detail="보안 토큰이 유효하지 않습니다.")
    import main
    emp = form.get("emp_code", "").strip()
    if not re.fullmatch(r"\d{6,10}", emp):
        return _redirect_msg("/admin/users", error="올바른 사번을 입력하세요.")
    with main._users_lock:
        bl = main._load_blacklist()
        if emp in bl:
            bl.remove(emp)
            main._save_blacklist(bl)
    admin_db.record_audit(admin["admin_id"], "user_unblocked", "chatbot_user", emp)
    return _redirect_msg("/admin/users", unblocked="1")


@router.post("/users/set-team")
async def users_set_team(request: Request):
    admin = _require_admin(request, ("system_admin",))
    form = await _read_form(request)
    if not _valid_csrf(request, form):
        return JSONResponse({"ok": False, "error": "보안 토큰이 유효하지 않습니다."}, status_code=403)
    import main
    emp = form.get("emp_code", "").strip()
    team = form.get("team", "").strip()
    if not re.fullmatch(r"\d{6,10}", emp):
        return JSONResponse({"ok": False, "error": "올바른 사번이 아닙니다."}, status_code=400)
    if team not in main.TEAM_OPTIONS:
        return JSONResponse({"ok": False, "error": "허용되지 않은 소속입니다."}, status_code=400)
    with main._users_lock:
        overrides = main._load_team_overrides()
        before = overrides.get(emp, "")
        overrides[emp] = team
        main._save_team_overrides(overrides)
        # 화이트리스트 등록자는 화이트리스트 소속도 함께 갱신
        wl = main._load_whitelist()
        if emp in wl:
            wl[emp]["team"] = team
            main._save_whitelist(wl)
        # 이미 카톡 등록된 사용자라면 등록 정보 소속도 갱신
        users_all = main._load_users()
        changed = False
        for uid, info in users_all.items():
            if str(info.get("emp_code", "")).strip() == emp:
                info["team"] = team
                changed = True
        if changed:
            main._save_users(users_all)
    admin_db.record_audit(admin["admin_id"], "user_team_changed", "chatbot_user", emp, {"team": before}, {"team": team})
    return JSONResponse({"ok": True, "emp_code": emp, "team": team})


@router.get("/usage", response_class=HTMLResponse)
async def usage(request: Request):
    _require_admin(request)
    return _render(request, "admin_usage.html", **_load_usage_stats())


@router.get("/audit", response_class=HTMLResponse)
async def audit(request: Request):
    _require_admin(request, ("system_admin", "voc_operator"))
    return _render(request, "admin_audit.html", logs=admin_db.recent_audit_logs())


@router.get("/api/stats")
async def api_stats(request: Request):
    _require_admin(request)
    return admin_db.dashboard_metrics()

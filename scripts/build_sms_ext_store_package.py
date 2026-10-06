"""SMS 발송 브릿지 — Chrome 웹 스토어 / Edge 추가 기능 스토어 업로드용 패키지 생성.

사용법 (dify-practice 루트에서):
    python scripts/build_sms_ext_store_package.py

결과: dist/sms-bridge-store-v{version}.zip  (Chrome 웹 스토어와 Edge 스토어에 동일 파일 업로드)

개발자 모드(ZIP) 배포본과의 차이
  - manifest 의 "key" 제거: 스토어는 key 필드 업로드를 허용하지 않고 자체 ID를 발급한다.
    → 발급된 ID를 App Service 환경변수 SMS_EXT_CWS_ID / SMS_EXT_EDGE_ID 에 넣으면 포털이 자동 인식.
  - localhost 개발용 주소 제거 (운영 포털 주소만 유지 → 심사 시 권한 최소화)
  - 스토어 필수 아이콘(16/32/48/128px) 추가, 설명문 132자 제한 준수
"""
from __future__ import annotations

import io
import json
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXT_DIR = ROOT / "browser-extension-sms-bridge"
ICON_SRC = ROOT / "icons" / "v2_dongwon_blue.png"
OUT_DIR = ROOT / "dist"

STORE_NAME = "동원홈푸드 SMS 발송 브릿지"
STORE_DESC = "동원홈푸드 세일즈 액션 플랫폼의 DM 문자를 사용자 본인의 사내 Direct 로그인 세션으로 발송하는 임직원 전용 도구입니다."
LOCAL_PREFIXES = ("http://localhost", "https://localhost", "http://127.0.0.1", "https://127.0.0.1")


def _icons() -> dict[str, bytes]:
    from PIL import Image

    src = Image.open(ICON_SRC).convert("RGBA")
    out: dict[str, bytes] = {}
    for size in (16, 32, 48, 128):
        buf = io.BytesIO()
        src.resize((size, size), Image.LANCZOS).save(buf, "PNG")
        out[f"icons/icon{size}.png"] = buf.getvalue()
    return out


def main() -> int:
    manifest = json.loads((EXT_DIR / "manifest.json").read_text(encoding="utf-8"))
    manifest.pop("key", None)
    manifest["name"] = STORE_NAME
    manifest["description"] = STORE_DESC
    assert len(STORE_DESC) <= 132, "스토어 설명은 132자 이하여야 합니다."
    manifest["host_permissions"] = [h for h in manifest.get("host_permissions", []) if not h.startswith(LOCAL_PREFIXES)]
    ec = manifest.get("externally_connectable", {})
    ec["matches"] = [m for m in ec.get("matches", []) if not m.startswith(LOCAL_PREFIXES)]
    manifest["externally_connectable"] = ec
    icons = _icons()
    manifest["icons"] = {str(s): f"icons/icon{s}.png" for s in (16, 32, 48, 128)}
    manifest["action"] = {"default_title": STORE_NAME, "default_icon": {str(s): f"icons/icon{s}.png" for s in (16, 32)}}

    version = manifest.get("version", "0")
    OUT_DIR.mkdir(exist_ok=True)
    out = OUT_DIR / f"sms-bridge-store-v{version}.zip"
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        zf.write(EXT_DIR / "background.js", "background.js")
        for name, data in icons.items():
            zf.writestr(name, data)
        # 스토어 등록 화면용 128px 아이콘(목록 이미지)도 함께 보관
    (OUT_DIR / f"sms-bridge-store-icon128.png").write_bytes(icons["icons/icon128.png"])

    lines = [
        f"[완료] {out}",
        f"  version      : {version}",
        f"  permissions  : {', '.join(manifest.get('permissions', []))}",
        f"  host_perms   : {', '.join(manifest['host_permissions'])}",
        "",
        "[스토어 등록 시 입력값]",
        "  공개 범위        : Chrome = '비공개(Unlisted)' / Edge = '숨김(Hidden)'",
        "  개인정보처리방침 : https://dw-fsi-platform-cgg6apc4ffaxb4d5.koreacentral-01.azurewebsites.net/portal/sms-extension/privacy",
        "  단일 목적        : 사내 포털에서 요청한 문자를 사용자 본인의 사내 Direct 세션으로 발송",
        "",
        "[등록 후] 발급된 ID를 App Service 환경변수에 입력 → 포털 팝업이 자동으로 '원클릭 설치'로 전환",
        "  SMS_EXT_CWS_ID=<Chrome 웹 스토어 ID>   SMS_EXT_EDGE_ID=<Edge 스토어 ID(선택)>",
    ]
    sys.stdout.buffer.write(("\n".join(lines) + "\n").encode("utf-8"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

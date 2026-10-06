"""Chrome 웹 스토어 / Edge 추가 기능 스토어 등록용 스크린샷(1280x800) 2장을 생성한다.

사용법 (dify-practice 루트에서):
    python scripts/build_sms_ext_store_screenshots.py

결과: dist/store_screenshot_1.png, dist/store_screenshot_2.png
"""
from __future__ import annotations

import asyncio
from pathlib import Path

OUT_DIR = Path(__file__).resolve().parent.parent / "dist"

_CSS = """
* { box-sizing: border-box; margin: 0; padding: 0; }
body { width: 1280px; height: 800px; font-family: 'Malgun Gothic', 'Apple SD Gothic Neo', sans-serif;
  background: linear-gradient(135deg, #071015 0%, #0b1720 55%, #064f88 100%); color: #fff; overflow: hidden; position: relative; }
.wrap { position: relative; z-index: 2; height: 100%; display: flex; flex-direction: column; padding: 64px 80px; }
.brand { display: flex; align-items: center; gap: 14px; margin-bottom: 36px; }
.brand .logo { width: 46px; height: 46px; border-radius: 13px; background: linear-gradient(135deg,#18b9c7,#0891b2);
  display: grid; place-items: center; font-size: 22px; box-shadow: 0 8px 22px rgba(24,185,199,.4); }
.brand b { font-size: 17px; letter-spacing: .3px; }
.brand small { display: block; color: #7fd8e0; font-size: 11px; font-weight: 800; letter-spacing: 2px; margin-top: 2px; }
h1 { font-size: 46px; line-height: 1.28; letter-spacing: -1px; max-width: 980px; }
h1 em { color: #4fd1db; font-style: normal; }
p.sub { font-size: 19px; color: #c7d3dc; margin-top: 22px; max-width: 760px; line-height: 1.7; }
.badges { display: flex; gap: 12px; margin-top: 30px; flex-wrap: wrap; }
.badge { background: rgba(255,255,255,.08); border: 1px solid rgba(255,255,255,.18); border-radius: 999px;
  padding: 9px 18px; font-size: 14px; font-weight: 700; color: #eef6f8; }
.badge b { color: #4fd1db; }
.flow { display: flex; align-items: center; gap: 0; margin-top: auto; }
.step { flex: 1; background: rgba(255,255,255,.06); border: 1px solid rgba(255,255,255,.16); border-radius: 20px;
  padding: 26px 24px; text-align: center; backdrop-filter: blur(6px); }
.step .ico { font-size: 34px; margin-bottom: 12px; }
.step b { display: block; font-size: 16px; margin-bottom: 8px; }
.step span { font-size: 13px; color: #b9c6ce; line-height: 1.6; display: block; }
.arrow { flex: 0 0 56px; text-align: center; font-size: 26px; color: #4fd1db; }
.glow1 { position: absolute; top: -120px; right: -120px; width: 420px; height: 420px; border-radius: 50%;
  background: radial-gradient(circle, rgba(24,185,199,.35), transparent 70%); z-index: 1; }
.glow2 { position: absolute; bottom: -160px; left: -100px; width: 480px; height: 480px; border-radius: 50%;
  background: radial-gradient(circle, rgba(8,78,136,.5), transparent 70%); z-index: 1; }
.footer-note { margin-top: 20px; font-size: 13px; color: #8aa0ab; }
"""

_SLIDE_1 = f"""<!doctype html><html lang="ko"><head><meta charset="utf-8"><style>{_CSS}</style></head>
<body>
<div class="glow1"></div><div class="glow2"></div>
<div class="wrap">
  <div class="brand">
    <span class="logo">📲</span>
    <div><b>동원홈푸드 SMS 발송 브릿지</b><small>SALES ACTION PLATFORM · EXTENSION</small></div>
  </div>
  <h1>세일즈 액션 플랫폼의 DM을<br><em>내 Direct 로그인 세션</em>으로 안전하게 발송</h1>
  <p class="sub">
    이 확장 프로그램은 단 하나의 일만 합니다 — 사내 포털(세일즈 액션 플랫폼)에서 영업담당자가
    요청한 안내 문자(SMS/LMS)를, 이 브라우저에 이미 로그인되어 있는 사내 Direct(direct.dongwon.com)
    세션을 그대로 사용해 전달하는 것입니다. 별도 로그인 정보를 저장하거나 외부로 전송하지 않습니다.
  </p>
  <div class="badges">
    <div class="badge"><b>✓</b> 본인 세션으로만 발송</div>
    <div class="badge"><b>✓</b> 쿠키·개인정보 외부 전송 없음</div>
    <div class="badge"><b>✓</b> 사내 도메인 외 접근 없음</div>
    <div class="badge"><b>✓</b> 동원홈푸드 임직원 전용</div>
  </div>
</div>
</body></html>"""

_SLIDE_2 = f"""<!doctype html><html lang="ko"><head><meta charset="utf-8"><style>{_CSS}</style></head>
<body>
<div class="glow1"></div><div class="glow2"></div>
<div class="wrap">
  <div class="brand">
    <span class="logo">🔗</span>
    <div><b>동작 방식 — 단일 목적 흐름</b><small>HOW IT WORKS</small></div>
  </div>
  <h1 style="font-size:38px;">포털 발송 클릭 → 내 세션으로 전달 → 결과만 기록</h1>
  <p class="sub">확장은 백그라운드에서만 동작하며 별도의 화면(팝업)이 없습니다. 사내 포털 화면에서 발송 과정을 그대로 보여줍니다.</p>
  <div class="flow">
    <div class="step"><div class="ico">🖥️</div><b>① 포털에서 DM 발송</b><span>영업담당자가 세일즈 액션<br>플랫폼에서 발송 버튼 클릭</span></div>
    <div class="arrow">➜</div>
    <div class="step"><div class="ico">🔐</div><b>② 본인 세션 확인</b><span>이 브라우저의 Direct<br>로그인 세션을 그대로 사용</span></div>
    <div class="arrow">➜</div>
    <div class="step"><div class="ico">💬</div><b>③ 사내 시스템 발송</b><span>direct.dongwon.com 으로만<br>발송 요청 전달</span></div>
    <div class="arrow">➜</div>
    <div class="step"><div class="ico">📋</div><b>④ 포털에 결과 기록</b><span>성공/실패 결과만 포털<br>발송 이력에 저장</span></div>
  </div>
  <div class="footer-note">host_permissions 는 direct.dongwon.com · dongwon.net · 사내 포털 도메인으로 한정되어 있습니다.</div>
</div>
</body></html>"""


async def main() -> None:
    from playwright.async_api import async_playwright

    OUT_DIR.mkdir(exist_ok=True)
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page(viewport={"width": 1280, "height": 800}, device_scale_factor=1)
        for i, html in enumerate((_SLIDE_1, _SLIDE_2), start=1):
            await page.set_content(html)
            await page.wait_for_timeout(120)
            out = OUT_DIR / f"store_screenshot_{i}.png"
            await page.screenshot(path=str(out))
            print(f"[완료] {out}")
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())

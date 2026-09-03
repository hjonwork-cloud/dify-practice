"""
배민상회 신규(미등록) 직배송 셀러 탐지 스크립트
======================================================
목적: 현재 크롤링 대상(price_baemin_sellers 테이블, 9개 고정 셀러)에
      등록되지 않은 새 직배송 셀러가 배민상회에 입점했는지 확인.

배경:
  - 배민상회 상품 API(`/front-api/v1/sellers/{sellerId}/goods/paging`)는
    "특정 sellerId의 상품 목록"만 조회 가능한 구조이며, "전체 셀러 목록"을
    반환하는 공개 API가 확인되지 않았음(과거 _explore_baemin_sellers*.py
    탐색 결과).
  - 따라서 신규 셀러는 mart.baemin.com 직배송 카테고리 페이지를 브라우저로
    렌더링해 __NEXT_DATA__ / 네트워크 응답 / HTML에서 sellerId를 역추출하는
    방식으로만 확인 가능하다.

실행:
  python discover_new_baemin_sellers.py

출력:
  - 페이지에서 발견된 전체 셀러(id, name)
  - 그중 현재 등록되지 않은(=신규 후보) 셀러만 별도로 강조 출력
"""
import asyncio
import json
import re
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from playwright.async_api import async_playwright

# ── 현재 크롤링 대상(price_baemin_sellers, 등록된 9개 셀러) ────────────────
REGISTERED_SELLERS = {
    "907":  "이너피스",
    "2090": "그로우식자재",
    "2089": "스마일푸드",
    "1384": "다봄푸드",
    "1774": "온국민신선몰",
    "2057": "세현F&B",
    "2006": "파라도",
    "2039": "현대그린푸드",
    "2005": "얌피쉬",
}

# 탐색 대상 후보 URL (직배송 카테고리/이벤트 페이지)
CANDIDATE_URLS = [
    "https://mart.baemin.com/direct",
    "https://mart.baemin.com/event/1341",
    "https://mart.baemin.com",
]

HEADERS_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
              "AppleWebKit/537.36 Chrome/126.0.0.0 Safari/537.36")


def _extract_from_text(text: str) -> dict:
    """HTML/JSON 텍스트에서 sellerId + sellerName 조합 추출."""
    found = {}
    # 패턴1: {"id":123,"name":"업체명"} 형태 (seller 객체)
    for m in re.finditer(r'"seller"\s*:\s*\{\s*"id"\s*:\s*(\d+)\s*,\s*"name"\s*:\s*"([^"]{1,40})"', text):
        found[m.group(1)] = m.group(2)
    # 패턴2: "sellerId":123 ... "sellerName":"업체명" (근접 위치)
    for m in re.finditer(r'"sellerId"\s*:\s*(\d+)[^}]{0,120}?"sellerName"\s*:\s*"([^"]{1,40})"', text):
        found.setdefault(m.group(1), m.group(2))
    for m in re.finditer(r'"sellerName"\s*:\s*"([^"]{1,40})"[^}]{0,120}?"sellerId"\s*:\s*(\d+)', text):
        found.setdefault(m.group(2), m.group(1))
    # 패턴3: sellerId만 (이름 미확인)
    for m in re.finditer(r'"sellerId"\s*:\s*(\d+)', text):
        found.setdefault(m.group(1), None)
    for m in re.finditer(r'/sellers?/(\d+)', text):
        found.setdefault(m.group(1), None)
    return found


async def _scan_url(url: str) -> dict:
    found: dict[str, str | None] = {}
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent=HEADERS_UA,
        )
        page = await ctx.new_page()

        captured_bodies = []

        async def on_response(resp):
            try:
                ct = resp.headers.get("content-type", "")
                if resp.status == 200 and "json" in ct:
                    body = await resp.json()
                    captured_bodies.append(json.dumps(body, ensure_ascii=False))
            except Exception:
                pass

        page.on("response", on_response)

        try:
            await page.goto(url, wait_until="networkidle", timeout=25000)
        except Exception as e:
            print(f"  ⚠ 페이지 로드 실패({url}): {e}")
            await browser.close()
            return found

        await page.wait_for_timeout(2000)
        # 스크롤 + 더보기 클릭으로 지연로드 콘텐츠까지 확보
        for _ in range(10):
            await page.mouse.wheel(0, 1800)
            await page.wait_for_timeout(500)
        for _ in range(3):
            try:
                btn = page.locator("button:has-text('더보기')").first
                if await btn.is_visible(timeout=800):
                    await btn.click()
                    await page.wait_for_timeout(1000)
            except Exception:
                break

        # 1) 네트워크 응답 바디
        for body_str in captured_bodies:
            found.update({k: v for k, v in _extract_from_text(body_str).items() if k not in found or v})

        # 2) __NEXT_DATA__
        try:
            next_data_raw = await page.evaluate(
                "() => { const el = document.getElementById('__NEXT_DATA__'); return el ? el.textContent : null; }"
            )
            if next_data_raw:
                found.update({k: v for k, v in _extract_from_text(next_data_raw).items() if k not in found or v})
        except Exception:
            pass

        # 3) 렌더링된 HTML 전체
        content = await page.content()
        found.update({k: v for k, v in _extract_from_text(content).items() if k not in found or v})

        await browser.close()
    return found


async def main():
    print("=" * 70)
    print("배민상회 신규 직배송 셀러 탐지")
    print("=" * 70)
    print(f"현재 등록된(크롤링 대상) 셀러: {len(REGISTERED_SELLERS)}개")
    for sid, name in REGISTERED_SELLERS.items():
        print(f"  - {sid}: {name}")
    print()

    all_found: dict[str, str | None] = {}
    for url in CANDIDATE_URLS:
        print(f"[탐색] {url}")
        result = await _scan_url(url)
        print(f"  → {len(result)}개 sellerId 패턴 발견")
        for sid, name in result.items():
            if sid not in all_found or (name and not all_found.get(sid)):
                all_found[sid] = name

    print()
    print("=" * 70)
    print(f"전체 발견된 sellerId 패턴: {len(all_found)}개")
    print("=" * 70)
    for sid, name in sorted(all_found.items(), key=lambda x: int(x[0])):
        tag = "✅ 등록됨" if sid in REGISTERED_SELLERS else "🆕 미등록(신규 후보)"
        print(f"  {sid:>6s}  {name or '(이름 미확인)':20s}  {tag}")

    new_candidates = {sid: n for sid, n in all_found.items() if sid not in REGISTERED_SELLERS}
    print()
    if new_candidates:
        print(f"🆕 신규 후보 {len(new_candidates)}건 발견 — price_baemin_sellers 테이블 등록 검토 필요")
        for sid, name in new_candidates.items():
            print(f"    seller_id={sid}  name={name or '?'}")
    else:
        print("신규 셀러 후보를 찾지 못했습니다.")
        print("※ 참고: 배민상회는 '전체 셀러 목록' 공개 API가 없어 페이지 렌더링만으로는")
        print("   sellerId를 100% 확인하지 못할 수 있습니다. 아래 방법을 권장합니다:")
        print("   1) mart.baemin.com에서 셀러명으로 직접 검색 → 상세페이지 URL의 sellerId 확인")
        print("   2) 경쟁사 분석 보고서(배민상회 직배송 셀러 입점 검토 보고.md)의 미등록 후보")
        print("      (베이킹몬/동그랑/청정식자재/유엠)를 사이트에서 직접 검색해 sellerId 확보")


if __name__ == "__main__":
    asyncio.run(main())

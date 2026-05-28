# -*- coding: utf-8 -*-
"""
DWHF ChatBot 아이콘 생성기 v3
- 사각형 배경 (라운드 코너)
- 파란색 좌상→우하 대각선 그라디언트
- 동원홈푸드 글로브 마크 + Pretendard ExtraBold 흰색 텍스트
- 텍스트 꽉 채움
"""
import os, math
from PIL import Image, ImageDraw, ImageFont, ImageFilter

OUT      = r"e:\git-copilot\dify-practice\icons"
FONT_EB  = os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\Windows\Fonts\Pretendard-ExtraBold.otf")
os.makedirs(OUT, exist_ok=True)

# ── 동원홈푸드 브랜드 컬러
BLUE_LT  = (0,   145, 234)   # 좌상 (밝은 파랑)
BLUE_DK  = (13,  60,  157)   # 우하 (짙은 파랑)
WHITE    = (255, 255, 255)


def gradient_rect(size, c1, c2, corner_r=0):
    """좌상→우하 대각선 그라디언트 사각형 이미지 반환"""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    # 픽셀별 그라디언트 합성
    raw = img.load()
    diag = math.sqrt(2) * size
    for y in range(size):
        for x in range(size):
            t = (x + y) / (2 * size - 2)   # 0.0 ~ 1.0 (대각선)
            r = int(c1[0] + (c2[0]-c1[0]) * t)
            g = int(c1[1] + (c2[1]-c1[1]) * t)
            b = int(c1[2] + (c2[2]-c1[2]) * t)
            raw[x, y] = (r, g, b, 255)

    # 라운드 코너 마스크 적용
    if corner_r > 0:
        mask = Image.new("L", (size, size), 0)
        md   = ImageDraw.Draw(mask)
        md.rounded_rectangle([0, 0, size-1, size-1], radius=corner_r, fill=255)
        img.putalpha(mask)

    return img


def draw_globe(d, cx, cy, r, line_color=WHITE):
    """동원 글로브 마크: 파란 원 + 흰 격자"""
    lw = max(1, r // 10)
    # 위도선 5줄
    for frac in [0.62, 0.30, 0, -0.30, -0.62]:
        y_l = cy + int(r * frac)
        hw  = int(math.sqrt(max(0, r**2 - (r*frac)**2)) * 0.97)
        if hw > 2:
            d.line([(cx-hw, y_l), (cx+hw, y_l)], fill=line_color, width=lw)
    # 경선 5줄
    for frac in [-0.58, -0.24, 0, 0.24, 0.58]:
        x_l = cx + int(r * frac)
        hh  = int(math.sqrt(max(0, r**2 - (r*frac)**2)) * 0.97)
        if hh > 2:
            d.line([(x_l, cy-hh), (x_l, cy+hh)], fill=line_color, width=lw)


def fit_font(text, font_path, max_w, max_h, start_size=200):
    """텍스트가 max_w × max_h 안에 딱 맞는 최대 폰트 크기 반환"""
    dummy = Image.new("RGBA", (1, 1))
    dd    = ImageDraw.Draw(dummy)
    for sz in range(start_size, 4, -1):
        try:
            fnt  = ImageFont.truetype(font_path, sz)
        except:
            fnt  = ImageFont.load_default()
        bb   = dd.textbbox((0, 0), text, font=fnt)
        tw, th = bb[2]-bb[0], bb[3]-bb[1]
        if tw <= max_w and th <= max_h:
            return fnt, tw, th
    return ImageFont.load_default(), max_w, max_h


def make_icon(size=256, variant="A"):
    """
    variant:
      A - [글로브 위]   DONGWON / 동원홈푸드  (2행 텍스트, 큰 글씨)
      B - [글로브 좌]   DONGWON / 동원홈푸드  (좌우 배치, 실제 로고 레이아웃)
      C - 글로브 없음, 'DW' 초대형 + 아래 작은 동원홈푸드
      D - 'Dongwon\n동원홈푸드' 텍스트만 꽉 채움, 글로브 소형 우상단
    """
    PAD   = int(size * 0.07)
    CR    = int(size * 0.14)   # 라운드 코너 반지름

    img = gradient_rect(size, BLUE_LT, BLUE_DK, corner_r=CR)
    d   = ImageDraw.Draw(img)

    if variant == "A":
        # ── 글로브 상단 중앙, 텍스트 아래 2행
        globe_r  = int(size * 0.18)
        globe_cx = size // 2
        globe_cy = PAD + globe_r + int(size * 0.02)

        # 글로브 원형 배경 (반투명 흰)
        d.ellipse([globe_cx-globe_r, globe_cy-globe_r,
                   globe_cx+globe_r, globe_cy+globe_r],
                  fill=(255,255,255,255))
        # 글로브 격자 (파란색으로, 원 위에)
        dg = ImageDraw.Draw(img)
        dg.ellipse([globe_cx-globe_r, globe_cy-globe_r,
                    globe_cx+globe_r, globe_cy+globe_r],
                   fill=(*BLUE_DK, 255))
        draw_globe(d, globe_cx, globe_cy, globe_r, line_color=WHITE)

        # 텍스트 영역
        text_top  = globe_cy + globe_r + int(size * 0.04)
        text_h    = size - text_top - PAD
        row_h     = text_h // 2

        fnt1, tw1, th1 = fit_font("Dongwon",   FONT_EB, size-PAD*2, row_h)
        fnt2, tw2, th2 = fit_font("동원홈푸드", FONT_EB, size-PAD*2, row_h)

        d.text(((size-tw1)//2, text_top + (row_h-th1)//2),
               "Dongwon", fill=WHITE, font=fnt1)
        d.text(((size-tw2)//2, text_top + row_h + (row_h-th2)//2),
               "동원홈푸드", fill=(200, 230, 255), font=fnt2)

    elif variant == "B":
        # ── 실제 동원홈푸드 로고 레이아웃: 글로브 좌, 텍스트 우 2행
        area_w = size - PAD*2
        area_h = size - PAD*2

        globe_r  = int(area_h * 0.30)
        globe_cx = PAD + globe_r
        globe_cy = size // 2

        d.ellipse([globe_cx-globe_r, globe_cy-globe_r,
                   globe_cx+globe_r, globe_cy+globe_r],
                  fill=(*BLUE_DK, 255))
        draw_globe(d, globe_cx, globe_cy, globe_r, line_color=WHITE)

        # 텍스트 오른쪽 영역
        tx    = globe_cx + globe_r + int(size * 0.04)
        t_w   = size - tx - PAD
        row_h = area_h // 2

        fnt1, tw1, th1 = fit_font("Dongwon",   FONT_EB, t_w, row_h)
        fnt2, tw2, th2 = fit_font("동원홈푸드", FONT_EB, t_w, row_h - int(size*0.04))

        base_y = size//2 - row_h + (row_h-th1)//2
        d.text((tx, base_y),           "Dongwon",   fill=WHITE,              font=fnt1)
        d.text((tx, base_y + row_h),   "동원홈푸드", fill=(200, 230, 255),   font=fnt2)

    elif variant == "C":
        # ── "DW" 초대형 + 하단 작은 텍스트
        fnt_dw, tw, th = fit_font("DW", FONT_EB, size-PAD*2, int(size*0.55))
        d.text(((size-tw)//2, int(size*0.08)), "DW", fill=WHITE, font=fnt_dw)

        fnt_sub, tw2, th2 = fit_font("동원홈푸드", FONT_EB, size-PAD*2, int(size*0.17))
        d.text(((size-tw2)//2, int(size*0.68)), "동원홈푸드", fill=(200,230,255), font=fnt_sub)

        # 글로브 소형 우상단
        gr = int(size * 0.10)
        gx = size - PAD - gr
        gy = PAD + gr
        d.ellipse([gx-gr, gy-gr, gx+gr, gy+gr], fill=(*BLUE_DK, 255))
        draw_globe(d, gx, gy, gr)

    elif variant == "D":
        # ── 텍스트 꽉 채움 (글로브 없음), 'Dongwon' 대형 + '동원홈푸드' 소형
        fnt1, tw1, th1 = fit_font("Dongwon",   FONT_EB, size-PAD*2, int(size*0.48))
        fnt2, tw2, th2 = fit_font("동원홈푸드", FONT_EB, size-PAD*2, int(size*0.28))

        total_h = th1 + int(size*0.04) + th2
        y1 = (size - total_h) // 2
        y2 = y1 + th1 + int(size*0.04)

        d.text(((size-tw1)//2, y1), "Dongwon",   fill=WHITE,            font=fnt1)
        d.text(((size-tw2)//2, y2), "동원홈푸드", fill=(200,230,255),   font=fnt2)

        # 상단 미니 장식 선
        lw = max(2, size//64)
        d.line([(PAD, y1-int(size*0.04)), (size-PAD, y1-int(size*0.04))],
               fill=(255,255,255,120), width=lw)
        d.line([(PAD, y2+th2+int(size*0.03)), (size-PAD, y2+th2+int(size*0.03))],
               fill=(255,255,255,120), width=lw)

    return img


def save_ico(img_256, name):
    sizes = [16, 24, 32, 48, 64, 128, 256]
    icons = [img_256.resize((s, s), Image.LANCZOS) for s in sizes]
    ico_path = os.path.join(OUT, f"{name}.ico")
    png_path = os.path.join(OUT, f"{name}.png")
    icons[0].save(ico_path, format="ICO", sizes=[(s,s) for s in sizes],
                  append_images=icons[1:])
    img_256.save(png_path, "PNG")
    print(f"  ✅ {name}.ico / .png")
    return ico_path


def make_servercheck(size=256):
    """서버 / 체크 — 2행, Pretendard ExtraBold 흰색, 대각 그라디언트 사각형"""
    CR = int(size * 0.14)

    # 그라디언트 배경
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    raw = img.load()
    for y in range(size):
        for x in range(size):
            t = (x + y) / (2 * size - 2)
            rc = int(BLUE_LT[0] + (BLUE_DK[0] - BLUE_LT[0]) * t)
            gc = int(BLUE_LT[1] + (BLUE_DK[1] - BLUE_LT[1]) * t)
            bc = int(BLUE_LT[2] + (BLUE_DK[2] - BLUE_LT[2]) * t)
            raw[x, y] = (rc, gc, bc, 255)

    # 라운드 코너 마스크
    msk = Image.new("L", (size, size), 0)
    ImageDraw.Draw(msk).rounded_rectangle([0, 0, size-1, size-1], radius=CR, fill=255)
    img.putalpha(msk)

    d      = ImageDraw.Draw(img)
    PAD    = int(size * 0.06)
    area_w = size - PAD * 2
    # 2행이 들어갈 각 행 최대 높이
    area_h = (size - PAD * 2) // 2 - int(size * 0.02)

    # 두 글자 모두 같은 크기로 꽉 채우기
    for sz in range(int(size * 1.0), 4, -1):
        try:
            fnt = ImageFont.truetype(FONT_EB, sz)
        except:
            fnt = ImageFont.load_default()
            break
        bb = d.textbbox((0, 0), "서버", font=fnt)
        tw, th = bb[2] - bb[0], bb[3] - bb[1]
        if tw <= area_w and th <= area_h:
            break

    def text_size(txt):
        bb = d.textbbox((0, 0), txt, font=fnt)
        return bb[2]-bb[0], bb[3]-bb[1]

    tw1, th1 = text_size("서버")
    tw2, th2 = text_size("체크")
    gap    = int(size * 0.03)
    total  = th1 + gap + th2
    y1     = (size - total) // 2
    y2     = y1 + th1 + gap

    d.text(((size - tw1) // 2, y1), "서버", fill=WHITE, font=fnt)
    d.text(((size - tw2) // 2, y2), "체크", fill=WHITE, font=fnt)
    return img


if __name__ == "__main__":
    print("=== DWHF ChatBot '서버체크' 아이콘 생성 ===")
    print(f"폰트: {FONT_EB}")
    print(f"폰트 존재: {os.path.exists(FONT_EB)}\n")

    SIZES = [16, 24, 32, 48, 64, 128, 256]
    icons = [make_servercheck(s) for s in SIZES]

    ico_path = os.path.join(OUT, "servercheck.ico")
    png_path = os.path.join(OUT, "servercheck.png")

    # ICO: 각 사이즈를 별도 RGBA PNG로 저장 후 직접 ICO 바이너리 조립
    import struct, io

    def make_ico_bytes(images):
        """RGBA PIL Image 리스트 → ICO 바이너리"""
        n = len(images)
        # ICONDIR header: reserved(2) type(2) count(2)
        header = struct.pack("<HHH", 0, 1, n)
        # 각 이미지를 PNG 바이트로 변환
        png_bufs = []
        for im in images:
            buf = io.BytesIO()
            im.save(buf, format="PNG")
            png_bufs.append(buf.getvalue())
        # ICONDIRENTRY per image: w(1) h(1) colors(1) reserved(1) planes(2) bpp(2) size(4) offset(4)
        offset = 6 + n * 16   # header + n*ICONDIRENTRY
        entries = b""
        for im, data in zip(images, png_bufs):
            w = im.width if im.width < 256 else 0
            h = im.height if im.height < 256 else 0
            entries += struct.pack("<BBBBHHII", w, h, 0, 0, 1, 32,
                                   len(data), offset)
            offset += len(data)
        return header + entries + b"".join(png_bufs)

    ico_data = make_ico_bytes(icons)
    with open(ico_path, "wb") as f:
        f.write(ico_data)
    icons[-1].save(png_path, "PNG")

    print(f"  ✅ servercheck.ico  ({os.path.getsize(ico_path):,} bytes)")
    print(f"  ✅ servercheck.png  ({os.path.getsize(png_path):,} bytes)")
    print(f"\n저장 위치: {OUT}")

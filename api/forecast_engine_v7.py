"""
forecast_engine_v7.py
══════════════════════════════════════════════════════════════
■ v6 → v7 변경사항
  1. DOW채움(dow_fill) 런레이트 신규 — 월초 소표본 전용:
     - 적용 조건: DAYS_SO_FAR <= FILL_THRESHOLD (기본 7일)
     - 실제 데이터(days 1~sim_day) + 전월 동요일 평균으로 나머지 날 채움
     - 전월 동요일 이상치: IQR 1.5× 제거
     - 일요일 제외 (배송 없음), 공휴일 제외 (KOR_HOLIDAYS)
     - NASCENT 브랜드: 전월 해당 요일 데이터가 없는 날은 skip(0)
     - 전월 데이터 부족 시 DOW가중 런레이트 → 균일 런레이트 순 fallback
     - pred_dow_fill(so_far, prev_daily_rows, sim_date) 신규 함수
  2. 공휴일 상수 추가: KOR_HOLIDAYS (2025~2026 한국 공휴일)

■ v5 → v6 변경사항 (유지)
  1. 요일별(DOW) 가중 런레이트 신규:
     - `대금청구일` 컴럼으로 일별 매출 조회 → 요일별 평균 산출
     - compute_dow_weights(): 7 요일 각 4주+ 관측 시 정규화 가중치 반환
     - pred_run_rate_dow(): 누계 / Σw(경과일) × Σw(월전체)
     - 적용 조건: NASCENT 또는 n_months<12 또는 store_last<20
     - 데이터 부족 시 기존 균일 런레이트로 자동 fallback

■ v4 → v5 변경사항 (유지)
  1. 브랜드 93개 (기존 49 + 신규 44, 층화 샘플링)
     - A_대형(50~150억) 3개: 순남시래기 본사, 디케이치킨, 국민낙곱새
     - B_중상(15~50억) 8개: 모미락, 곱도리신, 팔공티, 봉구푸드, 홍수계찜닭, 꾸이한끼, 카페보스, 펀코리아
     - C_중형(4~15억) 15개: 돈카춘, 치히로, 반미362, 훅트포케, 도리당, 포베이 등
     - D_소형(1~4억) 12개: 파스타부오노, 유케집, 거니푸드, 떡볶이농장 등
     - E_소규모(0.3~1억) 6개: 오사카타코야끼, 훈스돈까스, 파스타고 등

■ v3 → v4 변경사항 (유지)

  1. preprocess_actual() 전처리 신규
     ① 파일럿 첫 달 드롭: 첫 N개월 매출이 전체 중앙값의 15% 미만이면 제거
        → 도로시파스타(202411 1점포), 뎁짜이, 엄지네꼬막집(202504 11점포),
          데일리에프앤비(202506-07 파일럿) 케이스
     ② 반월치 첫 달 보정: 첫 달이 두 번째 달의 50% 미만이면 제거
        → 의령소바(202508 중순 계약 시작)
     ③ 단절 후 복귀: 중간에 0원 월 이후 6개월+ 활성 데이터가 있으면 단절 前 구간 제거
        → 백채김치찌개(202410 경쟁사 이탈 → 202412 복귀)

  2. detect_anomalies() 임계 완화 (1회성 계약 이벤트 노이즈 감소)
     - YoY 급등/급락:  ±60% → ±100%
     - MoM 급등/급락:  +40%/-35% → +60%/-50%
     - 점포 급변:       ±30%/±25% → ±50%/±40%
     - 점당매출 YoY:    ±40% → ±60%
     - 첫 달→두 번째 달 MoM은 이상치 감지 제외 (이관 론칭 패턴)

  3. 브랜드 49개 동일 (기존 v3 목록 유지)
  4. 시뮬레이션 날짜: 2026-04-14 (v3 동일)
══════════════════════════════════════════════════════════════
"""
import sys, os, math, statistics, calendar
from datetime import date, datetime, timedelta
from collections import defaultdict

sys.path.insert(0, os.path.dirname(__file__))
from dotenv import load_dotenv; load_dotenv()
# run_query 는 메인 루프 또는 predict_single_brand() 호출 시 파라미터로 전달

# ──────────────────────────────────────────────
# 설정
# ──────────────────────────────────────────────
SIM_DATE      = date(2026, 4, 14)
CUR_YM        = SIM_DATE.strftime("%Y%m")
DAYS_SO_FAR   = SIM_DATE.day
DAYS_IN_MONTH = calendar.monthrange(SIM_DATE.year, SIM_DATE.month)[1]
DAY_RATIO     = DAYS_SO_FAR / DAYS_IN_MONTH

MAX_YOY_RATIO  = 8.0   # YoY 이상치 상한 (800%)
MAX_MOM_RATIO  = 2.5   # MoM 이상치 상한 (250%)
FILL_THRESHOLD   = 7     # 이 일수 이하이면 DOW채움 런레이트 사용
FILL_CAP_RATIO   = 1.50  # 채움추정 / 통계예측 비율 이 값 이상이면 cap → fallback
FILL_FLOOR_RATIO = 0.40  # 채움추정 / 통계예측 비율 이 값 이하이면 cap → fallback
FILL_WINDOW_DAYS = 28    # 직전 N일 동요일 평균 윈도우 (전월 말 기준 역산)

def _get_fill_blend_w(days_so_far: int) -> float:
    """일수에 따라 DOW채움 최대 블렌딩 가중치를 반환 (신뢰도 선형 증가)."""
    if days_so_far <= 3:
        return 0.30
    elif days_so_far <= 5:
        return 0.40
    else:
        return 0.50

# ──────────────────────────────────────────────
# 한국 공휴일 (2025~2026)
# ──────────────────────────────────────────────
KOR_HOLIDAYS = {
    # 2025
    date(2025, 1, 1),   # 신정
    date(2025, 1, 28), date(2025, 1, 29), date(2025, 1, 30),  # 설날 연휴
    date(2025, 3, 1),   # 삼일절
    date(2025, 5, 5),   # 어린이날
    date(2025, 5, 6),   # 대체공휴일
    date(2025, 5, 15),  # 부처님오신날
    date(2025, 6, 6),   # 현충일
    date(2025, 8, 15),  # 광복절
    date(2025, 10, 3),  # 개천절
    date(2025, 10, 5), date(2025, 10, 6), date(2025, 10, 7),  # 추석 연휴
    date(2025, 10, 9),  # 한글날
    date(2025, 12, 25), # 크리스마스
    # 2026
    date(2026, 1, 1),   # 신정
    date(2026, 1, 28), date(2026, 1, 29), date(2026, 1, 30),  # 설날 연휴
    date(2026, 3, 1),   # 삼일절
    date(2026, 5, 5),   # 어린이날
    date(2026, 5, 24),  # 부처님오신날
    date(2026, 6, 6),   # 현충일
    date(2026, 8, 15),  # 광복절
    date(2026, 9, 24), date(2026, 9, 25), date(2026, 9, 26),  # 추석 연휴
    date(2026, 10, 3),  # 개천절
    date(2026, 10, 9),  # 한글날
    date(2026, 12, 25), # 크리스마스
}

BRANDS = [
    # ─── 기존 20개 (v2) ───────────────────────────────────────────
    ("0000800565", "생활맥주"),
    ("0000800710", "응급실떡볶이"),
    ("0000800415", "1943본사"),
    ("0000800341", "신화푸드"),
    ("0000800699", "샐러디"),
    ("0000800555", "핵가족"),
    ("0000800818", "영칼로리포케"),
    ("0000800356", "에그드랍"),
    ("0000801053", "위드저니파트너스"),
    ("0000800664", "미분당"),
    ("0000800116", "크라운호프"),
    ("0000800193", "펀앤아이"),
    ("0000800468", "금화왕돈까스"),
    ("0000800683", "교자의신"),
    ("0000800249", "골든카우보이"),
    ("0000800531", "포차천국"),
    ("0000800662", "인생닭강정"),
    ("0000800961", "정직유부"),
    ("0000800916", "산들그린"),
    ("0000800445", "새마을포차"),
    # ─── 신규 B밴드(8~15억) 3개 ───────────────────────────────────
    ("0000800640", "백채김치찌개"),
    ("0000800513", "구도로통닭"),
    ("0000800600", "슬로우캘리"),
    # ─── 신규 C밴드(4~8억) 10개 ──────────────────────────────────
    ("0000800625", "빅스타피자"),
    ("0000800678", "감탄떡볶이"),
    ("0000800535", "블루샥"),
    ("0000800386", "김복남맥주"),
    ("0000800467", "무지개맥주"),
    ("0000800577", "연안식당"),
    ("0000800295", "더벗79대포"),
    ("0000800012", "얌샘"),
    ("0000800655", "브라운돈까스"),
    ("0000800033", "퍼스트ANT"),
    # ─── 신규 D밴드(1.5~4억) 10개 ────────────────────────────────
    ("0000800255", "미스사이공"),
    ("0000800351", "보고싶다"),
    ("0000800374", "미스터빠삭"),
    ("0000800126", "미술관"),
    ("0000800906", "도로시파스타"),
    ("0000800869", "갯벌의조개"),
    ("0000801012", "의령소바"),
    ("0000800614", "설동궁찜닭"),
    ("0000800719", "단성무이"),
    ("0000800187", "어부네코다리"),
    # ─── 신규 E밴드(0.5~1.5억) 6개 ──────────────────────────────
    ("0000800539", "호박패밀리"),
    ("0000800615", "승도리네"),
    ("0000800908", "뎁짜이"),
    ("0000801027", "데일리에프앤비"),
    ("0000800978", "엄지네꼬막집"),
    ("0000800510", "명동할머니국수"),    # ─── v5 신규 A밴드(50~150억) 3개 ─────────────────────────
    ("0000800145", "순남시래기 본사"),
    ("0000800407", "디케이치킨(본사)"),
    ("0000800833", "국민낙곱새(본사)"),
    # ─── v5 신규 B밴드(15~50억) 8개 ──────────────────────────
    ("0000800474", "모미락 본사"),
    ("0000800932", "곱도리신 본사"),
    ("0000800722", "팔공티 본사"),
    ("0000800151", "봉구푸드(봉구비어)"),
    ("0000800203", "홍수계찜닭 본사"),
    ("0000800553", "꾸이한끼 본사"),
    ("0000800909", "카페보스 본사"),
    ("0000800573", "펀코리아(히어로즈)"),
    # ─── v5 신규 C밴드(4~15억) 15개 ──────────────────────────
    ("0000800674", "돈카춘 본사"),
    ("0000800799", "치히로(본사)"),
    ("0000800812", "반미362 본사"),
    ("0000800892", "훅트포케 본사"),
    ("0000800796", "도리당 본사"),
    ("0000800214", "포베이 본사"),
    ("0000800896", "해물점 본사"),
    ("0000800870", "할매솥뚜껑 본점"),
    ("0000801108", "훗스테이크 본사"),
    ("0000800902", "시올돈 본사"),
    ("0000800690", "감성족발 본사"),
    ("0000800956", "SJ디아트"),
    ("0000800330", "가르텐비어(본사)"),
    ("0000800919", "김치퀸김치찜 본사"),
    ("0000800854", "행포케 본사"),
    # ─── v5 신규 D밴드(1~4억) 12개 ───────────────────────────
    ("0000800973", "파스타부오노(본사)"),
    ("0000800743", "유케집 본사"),
    ("0000800848", "아도겐족부심"),
    ("0000800988", "거니푸드"),
    ("0000800905", "오리지널시카고피자"),
    ("0000800578", "파스타이츠"),
    ("0000214085", "패스트몰 화성"),
    ("0000800824", "떡볶이농장"),
    ("0000801011", "인생국물두루치기"),
    ("0000800060", "비원에프엔씨(비턴)"),
    ("0000800853", "선릉돈까스(본사)"),
    ("0000209583", "자미더홍"),
    # ─── v5 신규 E밴드(0.3~1억) 6개 ──────────────────────────
    ("0000800957", "오사카타코야끼 본사"),
    ("0000186833", "훈스돈까스 가경점"),
    ("0000205559", "파스타고 서교점"),
    ("0000184606", "부처스그릴 과천점"),
    ("0000184490", "다정소반"),
    ("0000212551", "투쿡"),]

# ──────────────────────────────────────────────
# 유틸
# ──────────────────────────────────────────────
def add_months(ym: str, n: int) -> str:
    y, m = int(ym[:4]), int(ym[4:])
    m += n
    while m > 12: m -= 12; y += 1
    while m <  1: m += 12; y -= 1
    return f"{y:04d}{m:02d}"

def prev_ym(ym: str) -> str:
    return add_months(ym, -12)

def safe_div(a, b):
    return a / b if b and b != 0 else None

def ewm_ratios(vals: list, alpha=0.4) -> float:
    if not vals: return 1.0
    w, total = 0.0, 0.0
    for i, v in enumerate(vals):
        weight = (1 - alpha) ** (len(vals) - 1 - i)
        total += v * weight
        w     += weight
    return total / w if w else 1.0

def linear_trend(ym_sales: list) -> float:
    n = len(ym_sales)
    if n < 3: return ym_sales[-1][1] if ym_sales else None
    xs = list(range(n))
    ys = [s for _, s in ym_sales]
    xm = statistics.mean(xs)
    ym_ = statistics.mean(ys)
    num = sum((x - xm) * (y - ym_) for x, y in zip(xs, ys))
    den = sum((x - xm) ** 2 for x in xs)
    slope = num / den if den else 0
    return max(0, ys[-1] + slope)

# ──────────────────────────────────────────────
# v6: 요일별(DOW) 가중 런레이트
# ──────────────────────────────────────────────
def compute_dow_weights(daily_rows: list) -> dict | None:
    """
    일별 매출 데이터에서 요일별 정규화 가중치 계산.

    daily_rows: [{'date': 'YYYYMMDD', 'sales': float}, ...]
    Returns: {0:월, 1:화, 2:수, 3:목, 4:금, 5:토} 가중치 dict
             (일요일=6 은 배송없음 → 가중치 0으로 고정)
    조건 미충족 시 None 반환 → 기존 선형 런레이트로 fallback
    """
    SUNDAY = 6
    dow_sales = defaultdict(list)
    for r in daily_rows:
        d = r["date"]
        s = float(r["sales"])
        if s <= 0:
            continue
        try:
            dt = datetime.strptime(str(d), "%Y%m%d")
        except Exception:
            continue
        if dt.weekday() == SUNDAY:
            continue          # 일요일 매출은 폐기/누락 등 특수 건 → 제외
        dow_sales[dt.weekday()].append(s)   # 0=월 ~ 5=토

    # 월~토 6개 요일 중 최소 5개, 각 2회 이상 관측 시 유효
    # (특정 요일 휴무 브랜드 허용, 누락 요일은 grand_avg로 대체)
    if len(dow_sales) < 5:
        return None
    if min(len(v) for v in dow_sales.values()) < 2:
        return None

    dow_avg = {dow: statistics.mean(v) for dow, v in dow_sales.items()}
    grand_avg = statistics.mean(dow_avg.values())
    if grand_avg == 0:
        return None
    # 누락 요일(월~토 내)은 grand_avg로 대체
    for d in range(6):   # 0~5: 월~토
        if d not in dow_avg:
            dow_avg[d] = grand_avg
    # 일요일은 배송 없음 → 0
    dow_avg[SUNDAY] = 0.0
    # 각 요일 가중치 = 요일평균 / 전체평균(일~토 제외 6일 기준)
    return {dow: avg / grand_avg for dow, avg in dow_avg.items()}


def pred_run_rate_dow(actual_so_far: float, sim_date: date,
                     dow_weights: dict) -> float | None:
    """
    DOW 가중치를 활용한 런레이트 예측.

    균일 런레이트:  누계 / 경과일 * 월전체일수
    DOW 런레이트:  누계 / Σw(경과일) * Σw(월전체일)

    Returns: 예측 월 매출액 (억)
    """
    if not dow_weights or actual_so_far <= 0:
        return None

    year, month = sim_date.year, sim_date.month
    days_in_month = calendar.monthrange(year, month)[1]

    # 경과일 (1 ~ sim_date.day) 가중치 합 — 일요일=0
    elapsed_w = sum(
        dow_weights.get(date(year, month, d).weekday(), 0.0)
        for d in range(1, sim_date.day + 1)
    )
    # 월 전체 가중치 합 — 일요일=0
    total_w = sum(
        dow_weights.get(date(year, month, d).weekday(), 0.0)
        for d in range(1, days_in_month + 1)
    )

    if elapsed_w == 0:
        return None

    return actual_so_far / elapsed_w * total_w


def _iqr_mean(vals: list) -> float:
    """IQR 1.5× 이상치 제거 후 평균 (4개 미만이면 단순 평균)."""
    arr = sorted(vals)
    n = len(arr)
    if n < 4:
        return statistics.mean(arr)
    q1, q3 = arr[n // 4], arr[3 * n // 4]
    iqr = q3 - q1
    filtered = [v for v in arr if q1 - 1.5 * iqr <= v <= q3 + 1.5 * iqr]
    return statistics.mean(filtered) if filtered else statistics.mean(arr)


def pred_dow_fill(so_far: float, prev_daily_rows: list, sim_date: date,
                 brand_type: str = "") -> tuple[float | None, dict]:
    """
    v7 개선: 전월 동요일 + 공휴일 계수 채움 런레이트 (월초 FILL_THRESHOLD일 이하 전용)

    ═══ 핵심 설계 ═══════════════════════════════════════════════════
    ① base DOW 평균 — 전월 데이터에서 일요일·공휴일 제외, IQR 이상치 제거 후 평균
    ② holiday_factor — 전월에 공휴일이 있으면:
         factor = holiday_sales / prev_dow_avg[holiday.weekday()]
         여러 공휴일이면 평균 factor 사용
         전월 공휴일 없으면 factor = 1.2 (경험적 기본값 — 평일보다 높음)
    ③ actual_so_far 이상치 cap:
         expected_so_far = Σ(prev_dow_avg × holiday_factor for d in 1..sim_day)
         if actual > expected × 2.0 → expected 사용 (이상치 발주 차단)
    ④ 채움 (sim_day+1~말일):
         일요일       → 0 (배송 없음)
         공휴일       → prev_dow_avg[dow] × holiday_factor
         일반 평일    → prev_dow_avg[dow]
         데이터 없는 요일(NASCENT 등) → 0 (보수적)

    Returns: (추정 월 합계, 진단 메타 dict)  /  (None, {}) — 전월 데이터 없음
    """
    if so_far <= 0:
        return None, {}

    year, month = sim_date.year, sim_date.month
    days_in_month = calendar.monthrange(year, month)[1]

    # 직전 FILL_WINDOW_DAYS일 윈도우 — 전월 말일 ~ 역산 (현재 월 데이터 제외)
    if month == 1:
        prev_y, prev_m = year - 1, 12
    else:
        prev_y, prev_m = year, month - 1
    window_end   = date(prev_y, prev_m, calendar.monthrange(prev_y, prev_m)[1])
    window_start = window_end - timedelta(days=FILL_WINDOW_DAYS - 1)

    # ── ① 직전 28일 일별 데이터 분류 ─────────────────────────────
    prev_dow_raw:     dict[int, list[float]] = defaultdict(list)  # 정상 평일
    prev_hol_items:   list[tuple[int, float, float]] = []         # (dow, sales, base_expected)

    for r in prev_daily_rows:
        d_str = str(r["date"])
        s = float(r["sales"])
        if s <= 0:
            continue
        try:
            dt = datetime.strptime(d_str, "%Y%m%d").date()
        except Exception:
            continue
        if not (window_start <= dt <= window_end):
            continue
        if dt.weekday() == 6:       # 일요일 → 완전 제외
            continue
        if dt in KOR_HOLIDAYS:      # 공휴일 → 별도 수집
            prev_hol_items.append((dt.weekday(), s, 0.0))  # base는 나중에 채움
        else:
            prev_dow_raw[dt.weekday()].append(s)

    if not prev_dow_raw:
        return None, {}

    # ── ② base DOW 평균 (IQR 이상치 제거, 일요일·공휴일 제외) ────
    prev_dow_avg: dict[int, float] = {}
    for dow, vals in prev_dow_raw.items():
        prev_dow_avg[dow] = _iqr_mean(vals)

    grand_avg = statistics.mean(prev_dow_avg.values())

    # ── ③ holiday_factor 산출 ────────────────────────────────────
    # 전월 공휴일 데이터가 있으면 실측치 / DOW기대값 비율 평균
    hol_factors = []
    for dow, hol_sales, _ in prev_hol_items:
        base = prev_dow_avg.get(dow, grand_avg)
        if base > 0:
            hol_factors.append(hol_sales / base)
    holiday_factor = _iqr_mean(hol_factors) if hol_factors else 1.2
    # 경험적 범위 clip: 0.3 ~ 3.0 (지나친 이상치 방지)
    holiday_factor = max(0.3, min(3.0, holiday_factor))

    # ── ④ 실제 누계(so_far) 이상치 cap ─────────────────────────
    # expected_so_far: 1~sim_day를 전월 DOW 평균+공휴일계수로 추정
    expected_so_far = 0.0
    for day in range(1, sim_date.day + 1):
        dt_check = date(year, month, day)
        dow = dt_check.weekday()
        if dow == 6:
            continue
        base = prev_dow_avg.get(dow, grand_avg)
        if dt_check in KOR_HOLIDAYS:
            expected_so_far += base * holiday_factor
        else:
            expected_so_far += base

    used_so_far = so_far
    capped = False
    if expected_so_far > 0 and so_far > expected_so_far * 2.0:
        used_so_far = expected_so_far   # 이상치 발주 → 기대값으로 대체
        capped = True

    # ── ⑤ 채움 (sim_day+1 ~ 말일) ────────────────────────────────
    filled = 0.0
    for day in range(sim_date.day + 1, days_in_month + 1):
        dt = date(year, month, day)
        dow = dt.weekday()
        if dow == 6:
            continue   # 일요일 → 0
        base = prev_dow_avg.get(dow, 0.0)   # NASCENT: 없는 요일 → 0
        if base <= 0:
            continue
        if dt in KOR_HOLIDAYS:
            filled += base * holiday_factor
        else:
            filled += base

    meta = {
        "holiday_factor": holiday_factor,
        "hol_obs": len(hol_factors),
        "capped": capped,
        "expected_so_far": expected_so_far,
        "used_so_far": used_so_far,
    }
    return used_so_far + filled, meta

# ──────────────────────────────────────────────
# v4 CORE: 전처리 함수
# ──────────────────────────────────────────────
def preprocess_actual(actual: dict) -> tuple[dict, list[str]]:
    """
    계약 구조 이벤트를 전처리하여 예측에 노이즈가 되는 구간을 제거.

    반환: (정제된 actual dict, 드롭된 월 리스트)

    처리 규칙 (우선순위 순):
    ① 단절 후 복귀: 중간에 매출 0원 월이 있고, 이후 6개월+ 연속 활성이면
       → 0원 이전 구간 전체 드롭 (백채김치찌개: 경쟁사 이탈→복귀)
    ② 파일럿 첫 달 드롭: 처음 1~3개월 매출이 전체 중앙값의 15% 미만이면 제거
       → 이관 전 테스트 점포 1~2개짜리 기간 제거
          (도로시파스타 202411, 엄지네꼬막집 202504, 뎁짜이 202411 이전)
    ③ 반월치 첫 달: 첫 달이 두 번째 달의 50% 미만이면 제거
       → 월 중순 계약 시작으로 첫 달 매출이 절반 미만인 경우
          (의령소바 202508)
    """
    months = sorted(actual.keys())
    dropped = []

    if len(months) < 2:
        return actual, dropped

    # ① 단절 후 복귀 처리
    # 0원(또는 거의 0) 월 탐색 — 단, 첫 달과 마지막 달 제외
    zero_months = [m for m in months[1:-1] if actual[m]["sales"] < 0.01]
    if zero_months:
        last_zero = max(zero_months)
        after_zero = [m for m in months if m > last_zero]
        # 복귀 후 6개월 이상 데이터가 있으면 → 단절 전 구간 모두 드롭
        if len(after_zero) >= 6:
            before_zero = [m for m in months if m <= last_zero]
            dropped.extend(before_zero)
            months = after_zero

    if len(months) < 2:
        return {m: actual[m] for m in months}, dropped

    # ② 파일럿 첫 달 드롭
    all_sales = [actual[m]["sales"] for m in months]
    # 중앙값 기준으로 파일럿 임계 설정 (평균은 초기 급성장 브랜드에서 왜곡됨)
    median_sales = statistics.median(all_sales)
    pilot_threshold = median_sales * 0.15

    # 앞에서 최대 3개월까지만 드롭 허용, 나머지 최소 4개월은 유지
    max_drop = min(3, len(months) - 4)
    drop_prefix = 0
    for i in range(max_drop):
        if actual[months[i]]["sales"] < pilot_threshold:
            drop_prefix = i + 1
        else:
            break
    if drop_prefix > 0:
        dropped.extend(months[:drop_prefix])
        months = months[drop_prefix:]

    if len(months) < 2:
        return {m: actual[m] for m in months}, dropped

    # ③ 반월치 첫 달 보정
    # 첫 달이 두 번째 달의 50% 미만이면 반월치로 판단하여 드롭
    if actual[months[0]]["sales"] < actual[months[1]]["sales"] * 0.50:
        dropped.append(months[0])
        months = months[1:]

    return {m: actual[m] for m in months}, dropped


# ──────────────────────────────────────────────
# 이상치 감지 (v4: 임계 완화, 첫 달 MoM 제외)
# ──────────────────────────────────────────────
def detect_anomalies(actual: dict) -> list:
    """
    월별 데이터에서 이상 징후 감지.
    v4 변경: 임계 완화 (계약 이벤트 노이즈 감소)
      - YoY: ±60% → ±100%
      - MoM: +40%/-35% → +60%/-50%
      - 점포: ±30%/±25% → ±50%/±40%
      - 점당매출 YoY: ±40% → ±60%
      - 브랜드 첫 달 → 두 번째 달 MoM 제외 (이관 론칭 패턴)
    """
    months = sorted(actual.keys())
    flags = []
    first_ym = months[0] if months else None

    for i, ym in enumerate(months):
        d = actual[ym]
        prv_y = prev_ym(ym)
        prv_m = add_months(ym, -1)
        is_second_month = (i == 1)   # 첫 달→두 번째 달 MoM 제외

        # YoY 이상
        if prv_y in actual and actual[prv_y]["sales"] > 0:
            yoy = d["sales"] / actual[prv_y]["sales"] - 1
            if yoy > 1.00:   # v4: 100% 초과
                flags.append((ym, "YoY급등", f"+{yoy*100:.0f}%"))
            elif yoy < -0.50:  # v4: -50% 미만
                flags.append((ym, "YoY급락", f"{yoy*100:.0f}%"))

        # MoM 이상 (첫 달→두 번째 달은 제외)
        if not is_second_month and prv_m in actual and actual[prv_m]["sales"] > 0:
            mom = d["sales"] / actual[prv_m]["sales"] - 1
            if mom > 0.60:    # v4: +60% 초과
                flags.append((ym, "MoM급등", f"+{mom*100:.0f}%"))
            elif mom < -0.50:  # v4: -50% 미만
                flags.append((ym, "MoM급락", f"{mom*100:.0f}%"))

        # 점포수 급변 (첫 달→두 번째 달 제외, 베이스 점포 5개 이상)
        if not is_second_month and prv_m in actual and actual[prv_m]["stores"] > 5:
            store_chg = (d["stores"] - actual[prv_m]["stores"]) / actual[prv_m]["stores"]
            if store_chg > 0.50:   # v4: +50% 초과
                flags.append((ym, "점포급증", f"+{store_chg*100:.0f}%"))
            elif store_chg < -0.40:  # v4: -40% 미만
                flags.append((ym, "점포급감", f"{store_chg*100:.0f}%"))

        # 점당매출 이상 (YoY 기준)
        if prv_y in actual and actual[prv_y]["per_store"] > 0:
            ps_yoy = d["per_store"] / actual[prv_y]["per_store"] - 1
            if abs(ps_yoy) > 0.60:  # v4: ±60%
                flag_type = "점당↑" if ps_yoy > 0 else "점당↓"
                flags.append((ym, flag_type, f"{ps_yoy*100:+.0f}%"))

    return flags

# ──────────────────────────────────────────────
# 브랜드 유형 분류
# ──────────────────────────────────────────────
BRAND_TYPES = {
    "NASCENT":           "신생/초기",
    "HYPER_GROWTH":      "급성장",
    "DECLINING":         "급감소",
    "STRUCTURAL_CHANGE": "구조변화",
    "VOLATILE":          "고변동",
    "STABLE":            "안정",
}

def classify_brand(actual: dict):
    months_data = sorted(actual.keys())
    n = len(months_data)

    yoy_list = []
    for ym in months_data:
        p = prev_ym(ym)
        if p in actual and actual[p]["sales"] > 0:
            ratio = actual[ym]["sales"] / actual[p]["sales"]
            if ratio <= MAX_YOY_RATIO:
                yoy_list.append(ratio - 1)

    recent_yms = months_data[-6:]
    yoy_recent = []
    for ym in recent_yms:
        p = prev_ym(ym)
        if p in actual and actual[p]["sales"] > 0:
            ratio = actual[ym]["sales"] / actual[p]["sales"]
            if ratio <= MAX_YOY_RATIO:
                yoy_recent.append(ratio - 1)

    mom_list = []
    for i in range(1, n):
        prv, cur = months_data[i-1], months_data[i]
        if actual[prv]["sales"] > 0:
            r = actual[cur]["sales"] / actual[prv]["sales"]
            if r <= MAX_MOM_RATIO:
                mom_list.append(r - 1)

    store_first = statistics.mean([actual[m]["stores"] for m in months_data[:3]])
    store_last  = statistics.mean([actual[m]["stores"] for m in months_data[-3:]])
    store_trend = safe_div(store_last - store_first, store_first) or 0

    if n >= 12:
        mid = n // 2
        s_before = statistics.mean([actual[m]["stores"] for m in months_data[:mid]])
        s_after  = statistics.mean([actual[m]["stores"] for m in months_data[mid:]])
        store_change_ratio = safe_div(s_after - s_before, s_before) or 0
    else:
        store_change_ratio = 0

    sales_vals = [actual[m]["sales"] for m in months_data]
    sales_mean = statistics.mean(sales_vals)
    sales_cv   = (statistics.stdev(sales_vals) / sales_mean) if sales_mean > 0 and n > 2 else 0
    mom_std    = statistics.stdev(mom_list) if len(mom_list) >= 3 else 0

    avg_yoy        = statistics.mean(yoy_list)   if yoy_list   else None
    avg_yoy_recent = statistics.mean(yoy_recent) if yoy_recent else None

    metrics = {
        "n_months":           n,
        "avg_yoy":            avg_yoy,
        "avg_yoy_recent6":    avg_yoy_recent,
        "store_trend_all":    store_trend,
        "store_change_ratio": store_change_ratio,
        "sales_cv":           sales_cv,
        "mom_std":            mom_std,
        "first_ym":           months_data[0],
        "last_ym":            months_data[-1],
        "store_first":        store_first,
        "store_last":         store_last,
    }

    # ── 분류 (우선순위 순) ────────────────────────────────────────
    # 0. NASCENT 강화: 초기 점포≤5 → 최근≥30, 36개월 이상 운영분 제외
    if store_first <= 5 and store_last >= 30 and n < 36:
        return "NASCENT", metrics

    # 1. 신생: 18개월 미만
    if n < 18:
        return "NASCENT", metrics

    # 2. 급성장: 최근6M YoY > 25% + 점포증가 > 8%
    if avg_yoy_recent is not None and avg_yoy_recent > 0.25 and store_trend > 0.08:
        return "HYPER_GROWTH", metrics

    # 3. 구조변화: 점포 전반/후반 20% 차이 + YoY 변동 큼
    if abs(store_change_ratio) > 0.20 and avg_yoy is not None and abs(avg_yoy) > 0.10:
        return "STRUCTURAL_CHANGE", metrics

    # 4. 급감소: 최근6M YoY < -15% 또는 점포트렌드 < -10%
    if (avg_yoy_recent is not None and avg_yoy_recent < -0.15) or store_trend < -0.10:
        return "DECLINING", metrics

    # 5. 고변동: CV > 0.18 또는 MoM std > 0.20
    if sales_cv > 0.18 or mom_std > 0.20:
        return "VOLATILE", metrics

    return "STABLE", metrics

# ──────────────────────────────────────────────
# 예측 함수
# ──────────────────────────────────────────────
def _yoy_ratios_n(actual, ym, n=6, key="sales"):
    ratios = []
    cur = ym
    for _ in range(n):
        cur = add_months(cur, -1)
        prv = prev_ym(cur)
        if cur in actual and prv in actual and actual[prv][key] > 0:
            ratio = actual[cur][key] / actual[prv][key]
            if key == "sales" and ratio > MAX_YOY_RATIO:
                continue
            ratios.append(ratio)
    return list(reversed(ratios))

def _mom_ratios_n(actual, ym, n=3, key="sales"):
    ratios = []
    cur = ym
    for _ in range(n):
        prv = add_months(cur, -1)
        if cur in actual and prv in actual and actual[prv][key] > 0:
            r = actual[cur][key] / actual[prv][key]
            if key == "sales" and r > MAX_MOM_RATIO:
                r = MAX_MOM_RATIO
            ratios.append(r)
        cur = prv
    return list(reversed(ratios))

def pred_yoy_avg(actual, ym, n=6):
    r = _yoy_ratios_n(actual, ym, n)
    if not r: return None
    p = prev_ym(ym)
    return actual[p]["sales"] * (sum(r) / len(r)) if p in actual else None

def pred_yoy_ewm(actual, ym, n=6, alpha=0.4):
    r = _yoy_ratios_n(actual, ym, n)
    if not r: return None
    p = prev_ym(ym)
    return actual[p]["sales"] * ewm_ratios(r, alpha) if p in actual else None

def pred_mom_avg(actual, ym, n=3):
    r = _mom_ratios_n(actual, ym, n)
    if not r: return None
    prv = add_months(ym, -1)
    return actual[prv]["sales"] * (sum(r) / len(r)) if prv in actual else None

def pred_trend_extrap(actual, ym, n=6):
    seg = []
    cur = ym
    for _ in range(n):
        cur = add_months(cur, -1)
        if cur in actual:
            seg.append((cur, actual[cur]["sales"]))
    seg = sorted(seg)
    if len(seg) < 3: return None
    return linear_trend(seg)

def pred_store_based(actual, ym):
    r_st = _mom_ratios_n(actual, ym, 3, "stores")
    r_ps = _mom_ratios_n(actual, ym, 3, "per_store")
    prv  = add_months(ym, -1)
    if prv not in actual or not r_st or not r_ps:
        return None
    pred_stores    = actual[prv]["stores"] * (sum(r_st) / len(r_st))
    pred_per_store = actual[prv]["per_store"] * (sum(r_ps) / len(r_ps))
    return max(0, pred_stores * pred_per_store)

# ──────────────────────────────────────────────
# 앙상블 가중치
# ──────────────────────────────────────────────
STRATEGY_WEIGHTS = {
    # (yoy_avg, yoy_ewm, mom_avg, trend, store_based)
    "NASCENT":           (0.00, 0.00, 0.45, 0.25, 0.30),
    "HYPER_GROWTH":      (0.00, 0.10, 0.45, 0.20, 0.25),
    "DECLINING":         (0.20, 0.30, 0.15, 0.35, 0.00),
    "STRUCTURAL_CHANGE": (0.10, 0.20, 0.30, 0.15, 0.25),
    "VOLATILE":          (0.30, 0.20, 0.30, 0.10, 0.10),
    "STABLE":            (0.45, 0.25, 0.20, 0.10, 0.00),
}

def pred_ensemble(actual, ym, brand_type):
    w = STRATEGY_WEIGHTS.get(brand_type, STRATEGY_WEIGHTS["STABLE"])
    preds = [
        pred_yoy_avg(actual, ym),
        pred_yoy_ewm(actual, ym),
        pred_mom_avg(actual, ym),
        pred_trend_extrap(actual, ym),
        pred_store_based(actual, ym),
    ]
    total_w, total_v = 0.0, 0.0
    for weight, val in zip(w, preds):
        if val is not None and val > 0:
            total_v += weight * val
            total_w += weight
    return total_v / total_w if total_w > 0 else None

def pred_final(actual, ym, brand_type, run_rate):
    stat = pred_ensemble(actual, ym, brand_type)
    if run_rate is None or DAYS_SO_FAR == 0:
        return stat, stat, None
    rr_weight = DAY_RATIO ** 1.5
    st_weight = 1 - rr_weight
    if stat is None:
        return run_rate, None, run_rate
    weighted = stat * st_weight + run_rate * rr_weight
    return weighted, stat, run_rate

# ──────────────────────────────────────────────
# 백테스트 (2025.01~2026.03)
# ──────────────────────────────────────────────
def backtest(actual, brand_type):
    test_months = ([f"2025{m:02d}" for m in range(1, 13)]
                   + ["202601", "202602", "202603"])
    test_months = [ym for ym in test_months if ym in actual]
    if not test_months:
        return {}, []

    errs = {k: [] for k in ["yoy", "yoy_ewm", "mom", "trend", "store", "ensemble"]}
    detail = []
    for ym in test_months:
        real = actual[ym]["sales"]
        preds = {
            "yoy":      pred_yoy_avg(actual, ym),
            "yoy_ewm":  pred_yoy_ewm(actual, ym),
            "mom":      pred_mom_avg(actual, ym),
            "trend":    pred_trend_extrap(actual, ym),
            "store":    pred_store_based(actual, ym),
            "ensemble": pred_ensemble(actual, ym, brand_type),
        }
        row = {"ym": ym, "real": real}
        for k, p in preds.items():
            row[k] = p
            if p is not None and real > 0:
                errs[k].append(abs(p - real) / real * 100)
        detail.append(row)

    mape = {k: (sum(v) / len(v) if v else None) for k, v in errs.items()}
    return mape, detail


# ══════════════════════════════════════════════════════════════════════
# 단일 브랜드 예측 API  (main.py 에서 임포트하여 사용)
# ══════════════════════════════════════════════════════════════════════
T_MAIN_FE = "h_hmfo.gd_dcube.`01_sap_sales_custmasters`"

def predict_single_brand(zc_name: str, today: date, run_q) -> dict | None:
    """
    zc_name : ZC본부명 (정확히 일치)
    today   : 예측 기준일 (당월 경과일수 결정)
    run_q   : SQL 실행 callable (→ list[dict]), e.g. main._safe_query
    Returns : {
        'forecast'   : float (억원),   # 당월 최종 예측
        'so_far'     : float (억원),   # 당월 현재까지 실적
        'stat_pred'  : float | None,   # 통계 앙상블 단독 예측
        'brand_type' : str,
        'days_so_far': int,
    } | None (데이터 없음)
    """
    cur_ym        = today.strftime("%Y%m")
    days_so_far   = today.day
    days_in_month = calendar.monthrange(today.year, today.month)[1]

    # 월별 매출+점포 (~ cur_ym 포함)
    sql1 = f"""
SELECT `년월`,
       SUM(`매출액`) / 1000000 AS sales,
       COUNT(DISTINCT `ZB본지점`) AS stores
FROM {T_MAIN_FE}
WHERE `사업부명` = '외식식재사업부' AND `ZC본부명` = '{zc_name}'
  AND `년월` <= '{cur_ym}'
GROUP BY `년월` ORDER BY `년월`
"""
    try:
        rows1 = run_q(sql1)
    except Exception:
        return None
    actual_raw = {}
    for r in rows1:
        s  = float(r.get("sales") or 0)
        st = int(r.get("stores") or 0)
        if st == 0: continue
        actual_raw[str(r["년월"])] = {"sales": s, "stores": st, "per_store": s / st}
    if not actual_raw:
        return None

    # 당월 so_far (월 집계)
    so_far_rows = [v["sales"] for k, v in actual_raw.items() if k == cur_ym]
    so_far = so_far_rows[0] if so_far_rows else 0.0

    # 통계 학습은 cur_ym 제외
    actual_for_stat = {k: v for k, v in actual_raw.items() if k < cur_ym}
    if not actual_for_stat:
        return {"forecast": so_far, "so_far": so_far, "stat_pred": None,
                "brand_type": "NASCENT", "days_so_far": days_so_far}

    actual, _ = preprocess_actual(actual_for_stat)
    if not actual:
        return None
    brand_type, metrics = classify_brand(actual)

    # 직전 6개월 일별 (DOW채움 + DOW가중 런레이트용)
    last_6m = add_months(cur_ym, -6)
    sql3 = f"""
SELECT `대금청구일` AS date, SUM(`매출액`)/1000000 AS sales
FROM {T_MAIN_FE}
WHERE `사업부명` = '외식식재사업부' AND `ZC본부명` = '{zc_name}'
  AND `년월` >= '{last_6m}' AND `년월` < '{cur_ym}'
GROUP BY `대금청구일` ORDER BY `대금청구일`
"""
    try:
        rows3 = run_q(sql3)
        dow_weights = compute_dow_weights(rows3)
    except Exception:
        rows3, dow_weights = [], None

    stat_pre = pred_ensemble(actual, cur_ym, brand_type)

    # DOW 채움 (월초 소표본)
    fill_rr, fill_meta = None, {}
    if days_so_far <= FILL_THRESHOLD and rows3:
        fill_rr, fill_meta = pred_dow_fill(so_far, rows3, today, brand_type)

    # cap 체크
    if fill_rr is not None and stat_pre is not None and stat_pre > 0:
        ratio = fill_rr / stat_pre
        if ratio > FILL_CAP_RATIO or ratio < FILL_FLOOR_RATIO:
            fill_rr, fill_meta = None, {}

    # 적응형 블렌딩
    effective_fill_w = 0.0
    if fill_rr is not None and stat_pre is not None and stat_pre > 0:
        divergence       = abs(fill_rr - stat_pre) / stat_pre
        fill_conf        = max(0.0, 1.0 - divergence)
        effective_fill_w = _get_fill_blend_w(days_so_far) * fill_conf

    # DOW 가중 런레이트 (v6 기존)
    dow_rr  = pred_run_rate_dow(so_far, today, dow_weights) if dow_weights else None
    USE_DOW = (dow_rr is not None and (
        brand_type == "NASCENT" or
        metrics["n_months"] < 12 or
        metrics["store_last"] < 20
    ))

    # 최종 예측
    if fill_rr is not None and stat_pre is not None and effective_fill_w > 0:
        forecast = stat_pre * (1 - effective_fill_w) + fill_rr * effective_fill_w
    else:
        day_ratio = days_so_far / days_in_month
        rr_weight = day_ratio ** 1.5
        run_rate  = dow_rr if USE_DOW else (
            (so_far / days_so_far * days_in_month) if days_so_far else 0
        )
        if stat_pre is None:
            forecast = run_rate
        else:
            forecast = stat_pre * (1 - rr_weight) + run_rate * rr_weight

    return {
        "forecast":    forecast,
        "so_far":      so_far,
        "stat_pred":   stat_pre,
        "brand_type":  brand_type,
        "days_so_far": days_so_far,
    }


# ──────────────────────────────────────────────
# 메인 루프  (스크립트 직접 실행 시에만 동작)
# ──────────────────────────────────────────────
_SCRIPT_MODE = __name__ == '__main__'
if _SCRIPT_MODE:
    from main import run_query  # noqa: E402
else:
    run_query = None  # type: ignore[assignment]

summary_rows   = []
anomaly_report = []

for ZC_CODE, BRAND_NAME in BRANDS:
    if not _SCRIPT_MODE:
        break  # 모듈로 임포트 시 루프 본문 실행 안 함
    # SQL1: 월별 매출+점포
    sql1 = f"""
SELECT `년월`,
       SUM(`매출액`) / 1000000 AS sales,
       COUNT(DISTINCT `ZB본지점`) AS stores,
       SUM(`매출액`) / 1000000 / COUNT(DISTINCT `ZB본지점`) AS per_store
FROM h_hmfo.gd_dcube.`01_sap_sales_custmasters`
WHERE `사업부명` = '외식식재사업부' AND `ZC본부` = '{ZC_CODE}'
  AND `년월` <= '202604'
GROUP BY `년월` ORDER BY `년월`
"""
    rows1 = run_query(sql1)
    actual_raw = {}
    for r in rows1:
        s = float(r["sales"])
        st = int(r["stores"])
        if st == 0: continue
        actual_raw[r["년월"]] = {
            "sales":     s,
            "stores":    st,
            "per_store": s / st,
        }
    if not actual_raw:
        print(f"[{BRAND_NAME}] 데이터 없음"); continue

    # ── v4 전처리 ──────────────────────────────────────────────────
    actual, dropped_months = preprocess_actual(actual_raw)
    if not actual:
        print(f"[{BRAND_NAME}] 전처리 후 데이터 없음"); continue

    # SQL2: 신규/폐업 점포
    sql2 = f"""
SELECT `ZB본지점`, MIN(`년월`) AS first_ym, MAX(`년월`) AS last_ym
FROM h_hmfo.gd_dcube.`01_sap_sales_custmasters`
WHERE `사업부명` = '외식식재사업부' AND `ZC본부` = '{ZC_CODE}'
  AND `년월` <= '202604'
GROUP BY `ZB본지점`
"""
    rows2 = run_query(sql2)
    latest_ym     = max(actual.keys())
    new_stores    = {}
    closed_stores = {}
    for r in rows2:
        fy, ly = r["first_ym"], r["last_ym"]
        new_stores[fy]    = new_stores.get(fy, 0) + 1
        if ly < latest_ym:
            closed_stores[ly] = closed_stores.get(ly, 0) + 1

    # 분류 + 이상치 감지
    brand_type, metrics = classify_brand(actual)
    anomalies = detect_anomalies(actual)
    if anomalies:
        anomaly_report.append((BRAND_NAME, ZC_CODE, brand_type, anomalies))

    # SQL3: DOW 가중치 및 전월 채움용 일별 매출 (최근 6개월)
    last_6m = add_months(CUR_YM, -6)   # 예: 202510
    sql3 = f"""
SELECT `대금청구일` AS date, SUM(`매출액`)/1000000 AS sales
FROM h_hmfo.gd_dcube.`01_sap_sales_custmasters`
WHERE `사업부명` = '외식식재사업부' AND `ZC본부` = '{ZC_CODE}'
  AND `년월` >= '{last_6m}' AND `년월` < '{CUR_YM}'
GROUP BY `대금청구일` ORDER BY `대금청구일`
"""
    try:
        rows3 = run_query(sql3)
        dow_weights = compute_dow_weights(rows3)
    except Exception:
        rows3 = []
        dow_weights = None

    # 202604 시뮬레이션
    actual_apr = actual.get("202604", {}).get("sales", 0)
    so_far_sim  = actual_apr * (DAYS_SO_FAR / DAYS_IN_MONTH)
    daily_avg   = so_far_sim / DAYS_SO_FAR if DAYS_SO_FAR else 0

    # 통계 앙상블을 미리 계산 (cap 판단 + 적응형 블렌딩에 사용)
    stat_pre = pred_ensemble(actual, CUR_YM, brand_type)

    # ── v7: 월초 소표본 → DOW채움+공휴일계수 런레이트 우선 적용 ──
    fill_rr, fill_meta = None, {}
    if DAYS_SO_FAR <= FILL_THRESHOLD and rows3:
        fill_rr, fill_meta = pred_dow_fill(so_far_sim, rows3, SIM_DATE, brand_type)

    # ── cap 체크: 채움추정이 통계예측 대비 너무 크거나 작으면 신뢰 불가 ──
    fill_capped_reason = ""
    if fill_rr is not None and stat_pre is not None and stat_pre > 0:
        ratio = fill_rr / stat_pre
        if ratio > FILL_CAP_RATIO:
            fill_capped_reason = f"cap↑(채움/통계={ratio:.2f}>{FILL_CAP_RATIO})"
            fill_rr, fill_meta = None, {}
        elif ratio < FILL_FLOOR_RATIO:
            fill_capped_reason = f"cap↓(채움/통계={ratio:.2f}<{FILL_FLOOR_RATIO})"
            fill_rr, fill_meta = None, {}

    # ── 적응형 블렌딩 가중치 (채움추정 ↔ 통계예측 괴리도로 신뢰도 조정) ──
    effective_fill_w = 0.0
    if fill_rr is not None and stat_pre is not None and stat_pre > 0:
        divergence    = abs(fill_rr - stat_pre) / stat_pre    # 0=완전일치, 1=100%차이
        fill_conf     = max(0.0, 1.0 - divergence)            # 1→0으로 감소
        effective_fill_w = _get_fill_blend_w(DAYS_SO_FAR) * fill_conf  # 최대 days별 가중치

    # DOW 가중 런레이트 (v6 기존)
    dow_rr = pred_run_rate_dow(so_far_sim, SIM_DATE, dow_weights) if dow_weights else None
    USE_DOW = (dow_rr is not None and (
        brand_type == "NASCENT" or
        metrics["n_months"] < 12 or
        metrics["store_last"] < 20
    ))

    # ── 최종 예측 계산 ──────────────────────────────────────────────
    if fill_rr is not None and stat_pre is not None and effective_fill_w > 0:
        # DOW채움 적응형 블렌딩: stat을 우선으로, fill_rr 신뢰도만큼 가중
        run_rate        = fill_rr
        run_rate_method = f"DOW채움(w={effective_fill_w:.2f})"
        final_pred  = stat_pre * (1 - effective_fill_w) + fill_rr * effective_fill_w
        stat_pred   = stat_pre
        rr          = fill_rr
    else:
        # DOW가중 또는 균일 런레이트 → 기존 pred_final 사용
        if fill_rr is None and fill_capped_reason:
            run_rate_method = f"균일(채움{fill_capped_reason})"
            run_rate = daily_avg * DAYS_IN_MONTH
        elif USE_DOW:
            run_rate        = dow_rr
            run_rate_method = "DOW가중"
        else:
            run_rate        = daily_avg * DAYS_IN_MONTH
            run_rate_method = "균일" if dow_weights is None else "균일(조건불충족)"
        final_pred, stat_pred, rr = pred_final(actual, CUR_YM, brand_type, run_rate)

    # 백테스트
    mape, detail = backtest(actual, brand_type)

    # 202604 오차
    apr_err = None
    if final_pred is not None and actual_apr > 0:
        apr_err = (final_pred - actual_apr) / actual_apr * 100

    # ════════════════════════════════════════════════════════════════
    # 출력
    # ════════════════════════════════════════════════════════════════
    btype_label = BRAND_TYPES.get(brand_type, brand_type)
    print(f"\n{'═'*120}")
    print(f"  [{BRAND_NAME}]  ({ZC_CODE})  유형: {btype_label}({brand_type})")
    if dropped_months:
        print(f"  ⚙ v4전처리: {', '.join(dropped_months)} 드롭 (파일럿/반월치/단절전구간)")
    avg_yoy_str = f"{metrics['avg_yoy']*100:+.1f}%" if metrics['avg_yoy'] is not None else "N/A"
    r6_str      = f"{metrics['avg_yoy_recent6']*100:+.1f}%" if metrics['avg_yoy_recent6'] is not None else "N/A"
    raw_first   = min(actual_raw.keys())
    print(f"  raw:{raw_first}~{metrics['last_ym']}  학습:{metrics['first_ym']}~{metrics['last_ym']} ({metrics['n_months']}m)  "
          f"avg_yoy={avg_yoy_str}  recent6={r6_str}  "
          f"store_trend={metrics['store_trend_all']*100:+.1f}%  "
          f"cv={metrics['sales_cv']:.3f}  mom_std={metrics['mom_std']:.3f}  "
          f"점포 {metrics['store_first']:.0f}→{metrics['store_last']:.0f}")
    ws = STRATEGY_WEIGHTS.get(brand_type, STRATEGY_WEIGHTS["STABLE"])
    wkeys = ["YoY_avg", "YoY_EWM", "MoM_avg", "Trend", "Store기반"]
    wstr = "  가중치: " + "  ".join(f"{k}={v:.0%}" for k, v in zip(wkeys, ws) if v > 0)
    print(wstr)

    # 트렌드 (2024 이후, raw 데이터 기준 출력 / 드롭된 월은 [드롭] 표기)
    print(f"{'─'*120}")
    print(f"  {'월':<8} {'매출(억)':>8} {'점포':>5} {'점당(억)':>8}  {'YoY':>7} {'MoM':>7}  {'新':>4} {'廢':>4} {'순':>4}  [이상]")
    print(f"  {'─'*105}")
    for ym, d in sorted(actual_raw.items()):
        if ym < "202401": continue
        prv_y = prev_ym(ym)
        prv_m = add_months(ym, -1)
        s_yoy = f"{d['sales']/actual_raw[prv_y]['sales']*100-100:+.1f}%" if prv_y in actual_raw and actual_raw[prv_y]['sales'] > 0 else "  N/A "
        s_mom = f"{d['sales']/actual_raw[prv_m]['sales']*100-100:+.1f}%" if prv_m in actual_raw and actual_raw[prv_m]['sales'] > 0 else "  N/A "
        nw = new_stores.get(ym, 0)
        cl = closed_stores.get(ym, 0)
        flag_mark = " ◀" if ym >= "202501" else ""
        drop_mark = " [드롭]" if ym in dropped_months else ""
        anom = [f"{t}({v})" for am, t, v in anomalies if am == ym]
        anom_str = " !" + ",".join(anom) if anom else ""
        print(f"  {ym:<8} {d['sales']:>8.2f} {d['stores']:>5} {d['per_store']:>8.3f}  "
              f"{s_yoy:>7} {s_mom:>7}  {nw:>4} {cl:>4} {nw-cl:>+4}{flag_mark}{drop_mark}{anom_str}")

    # 런레이트
    rr_w = DAY_RATIO ** 1.5
    stat_str  = f"{stat_pred:.2f}억"  if stat_pred  is not None else "N/A"
    final_str = f"{final_pred:.2f}억" if final_pred is not None else "N/A"
    extra_tag = ""
    if fill_rr is not None:
        hf   = fill_meta.get('holiday_factor', 1.0)
        cap  = " [cap]" if fill_meta.get('capped') else ""
        extra_tag = f"  채움={fill_rr:.2f}억(hol×{hf:.2f}{cap} w={effective_fill_w:.2f})"
    elif fill_capped_reason:
        extra_tag = f"  [{fill_capped_reason}]"
    elif dow_rr is not None:
        extra_tag = f"  DOW가중={dow_rr:.2f}억"
    print(f"\n  [{SIM_DATE.strftime('%m/%d')}시뮬] 누계={so_far_sim:.2f}억  런레이트({run_rate_method})={run_rate:.2f}억{extra_tag}  "
          f"앙상블={stat_str}  rr_w={rr_w:.3f}  ▶최종={final_str}")
    if actual_apr > 0 and final_pred is not None:
        sym = "✓" if abs(apr_err) < 5 else ("△" if abs(apr_err) < 10 else "✗")
        print(f"  ▶ 202604 실제={actual_apr:.2f}억  예측={final_pred:.2f}억  오차={apr_err:+.1f}% [{sym}]")

    # 백테스트
    if detail and metrics["n_months"] >= 18:
        print(f"\n  백테스트 MAPE (2025.01~2026.03):")
        best_k, best_v = None, 999
        for k, v in mape.items():
            if v is not None and v < best_v: best_v = v; best_k = k
        mape_parts = []
        for k, v in mape.items():
            if v is not None:
                star = " ★" if k == best_k else ""
                mape_parts.append(f"{k}={v:.1f}%{star}")
        print("  " + "  ".join(mape_parts))

    summary_rows.append({
        "brand":      BRAND_NAME,
        "zc":         ZC_CODE,
        "type":       btype_label,
        "months":     metrics["n_months"],
        "avg_yoy":    (f"{metrics['avg_yoy']*100:+.1f}%" if metrics["avg_yoy"] is not None else "N/A"),
        "ens_mape":   f"{mape.get('ensemble'):.1f}%"  if mape.get("ensemble") is not None else "N/A",
        "final_pred": (f"{final_pred:.2f}억" if final_pred is not None else "N/A"),
        "actual_apr": f"{actual_apr:.2f}억" if actual_apr else "—",
        "apr_err":    (f"{apr_err:+.1f}%" if apr_err is not None else "—"),
        "dropped":    len(dropped_months),
    })

# ═══════════════════════════════════════════════════════════════════
# 종합 요약 / 이상치 리포트 (스크립트 직접 실행 시에만)
# ═══════════════════════════════════════════════════════════════════
if _SCRIPT_MODE:
    print(f"\n\n{'═'*140}")
    print(f"  v6 종합 요약  | 시뮬: 2026-04-14 | 백테스트: 2025.01~2026.03 | 총 {len(summary_rows)}개 브랜드")
    print(f"  v4 전처리 적용: 파일럳 첫 달 드롭 / 반월치 보정 / 단절 후 복귀 구간 정리")
    print(f"  v4 전처리 적용: 파일럿 첫 달 드롭 / 반월치 보정 / 단절 후 복귀 구간 정리")
    print(f"{'═'*140}")
    print(f"  {'브랜드':<16} {'유형':<12} {'개월':>5} {'평균YoY':>9} {'앙상블MAPE':>11} "
          f"{'예측(4/14)':>10} {'실제202604':>11} {'오차':>7} {'드롭월':>5}")
    print(f"  {'─'*106}")
    for r in summary_rows:
        drop_str = f"{r['dropped']}개월" if r['dropped'] > 0 else "—"
        print(f"  {r['brand']:<16} {r['type']:<12} {r['months']:>5} {r['avg_yoy']:>9} "
              f"{r['ens_mape']:>11} {r['final_pred']:>10} {r['actual_apr']:>11} {r['apr_err']:>7} {drop_str:>5}")

    compare_rows = [r for r in summary_rows if r['apr_err'] not in ("—","N/A")]
    accurate    = [r for r in compare_rows if abs(float(r['apr_err'].rstrip('%'))) <= 5]
    near_ok     = [r for r in compare_rows if 5 < abs(float(r['apr_err'].rstrip('%'))) <= 10]
    problematic = [r for r in compare_rows if abs(float(r['apr_err'].rstrip('%'))) > 10]

    print(f"\n  ■ 202604 예측 정확도:")
    print(f"    ✓ 오차≤5%({len(accurate)}개): {', '.join(r['brand'] for r in accurate)}")
    print(f"    △ 5~10%({len(near_ok)}개):  {', '.join(r['brand']+'('+r['apr_err']+')' for r in near_ok)}")
    print(f"    ✗ 10%초과({len(problematic)}개): {', '.join(r['brand']+'('+r['apr_err']+')' for r in problematic)}")

    type_mapes = defaultdict(list)
    for r in summary_rows:
        if r['ens_mape'] != "N/A":
            type_mapes[r['type']].append(float(r['ens_mape'].rstrip('%')))
    print(f"\n  ■ 유형별 앙상블 MAPE:")
    for t, vals in sorted(type_mapes.items()):
        marker = " ← 개선필요" if sum(vals)/len(vals) > 15 else ""
        print(f"    {t:<12} avg={sum(vals)/len(vals):.1f}%  ({len(vals)}개){marker}")

    dropped_brands = [r for r in summary_rows if r['dropped'] > 0]
    if dropped_brands:
        print(f"\n  ■ v4 전처리 드롭 현황 ({len(dropped_brands)}개 브랜드):")
        for r in dropped_brands:
            print(f"    {r['brand']}: {r['dropped']}개월 드롭")

    print(f"\n\n{'░'*140}")
    print(f"  [이상치 감지 리포트]  v4: 임계 완화(YoY±100%, MoM±60%, 점포±50%), 첫 달MoM 제외")
    print(f"{'░'*140}")
    for brand, zc, btype, flags in anomaly_report:
        btype_label = BRAND_TYPES.get(btype, btype)
        recent_flags = [(ym, t, desc) for ym, t, desc in flags if ym >= "202401"]
        if not recent_flags:
            continue
        print(f"\n  [{brand}] ({zc}) [{btype_label}]")
        by_ym = defaultdict(list)
        for ym, t, desc in recent_flags:
            by_ym[ym].append(f"{t}:{desc}")
        for ym in sorted(by_ym.keys()):
            print(f"    {ym}  " + "  /  ".join(by_ym[ym]))

    print(f"\n  총 이상치 브랜드: {len([b for b,_,_,f in anomaly_report if any(ym>='202401' for ym,_,_ in f)])}개")
    print()

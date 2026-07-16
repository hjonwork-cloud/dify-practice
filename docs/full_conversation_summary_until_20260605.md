# 전체 대화/작업 이력 종합 정리

작성일: 2026-06-05

> 범위: 오늘 대화뿐 아니라, 이전 세션에서 이어진 챗봇 서버 개발/수정/데이터 검토/산출물 생성 작업까지 포함한 누적 정리입니다.

---

## 1. 프로젝트 개요

### 1.1 프로젝트 목적

Databricks 데이터를 기반으로 Dify/Kakao 챗봇에서 영업/매출/미출고/수익성/액션 제안 질의에 답변하는 FastAPI 미들웨어 서버를 운영/개선하는 작업을 진행했다.

주요 목적은 다음과 같다.

1. 챗봇 질의가 불필요하게 Dify로 넘어가지 않고 서버 내 SQL bypass 로직으로 빠르게 응답하도록 개선
2. 브랜드/사업부/영업사원/상품/미출고/수익성 관련 질의 응답 품질 개선
3. 기존 Databricks 테이블 지원 종료에 대비해 신규 FSI 테이블 기반 호환 뷰 생성
4. 신규/기존/중단 거래처 분석 기준을 확정하고 Excel 산출
5. 작업 내역을 문서화하여 향후 운영/전환 시 참고 가능하도록 정리

---

## 2. 주요 시스템 구성

### 2.1 서비스 구조

| 구성 | 설명 |
|---|---|
| FastAPI 서버 | Dify HTTP Tool과 Databricks SQL 사이의 미들웨어 |
| Dify | 챗봇 LLM/Agent 질의 처리 |
| Kakao | 사용자 대화 채널 |
| Databricks SQL Warehouse | 매출/고객/미출고/수익성 데이터 조회 |
| Python scripts | 데이터 검증, Excel/PPT/보고서 산출 |

### 2.2 핵심 코드 파일

| 파일 | 역할 |
|---|---|
| `api/main.py` | FastAPI 메인 서버, `/query` endpoint, 대부분의 SQL bypass 로직 |
| `api/action_signals.py` | 액션 제안/신호 탐지 로직 |
| `api/action_router.py` | 액션 제안 API router |
| `api/forecast_engine_v7.py` | 브랜드/매출 forecast 관련 로직 |
| `docs/create_chatbot_sales_custmasters_compat_v.sql` | 신규 FSI 기반 호환 매출 뷰 생성 SQL |
| `docs/sales_table_migration_review_20260605.md` | 신규 매출 테이블 전환 검토 문서 |
| `scripts/export_customer_lifecycle_2020_2025.py` | 신규/기존/중단 거래처 Excel 산출 스크립트 |

---

## 3. Databricks 접속/환경 정보

### 3.1 Databricks 설정

`api/main.py` 기준:

| 항목 | 값 |
|---|---|
| Host | `https://adb-707807361397497.17.azuredatabricks.net` |
| SQL Warehouse HTTP Path | `/sql/1.0/warehouses/acc2ec933ffef2d0` |
| API Key 기본값 | `dify-secret-1234` |

### 3.2 Python 실행 환경

Databricks SQL 작업에 실제 사용한 Python:

`e:\git-copilot\.conda\python.exe`

해당 환경에서 확인된 주요 패키지:

| 패키지 | 버전/상태 |
|---|---|
| `databricks-sql-connector` | 사용 가능 |
| `pandas` | `2.3.3` |
| `openpyxl` | `3.1.5` |

주의:

- VS Code Python 환경 자동 감지 결과와 실제 Databricks 작업 환경이 달랐다.
- Databricks 작업은 `e:\git-copilot\.conda\python.exe`를 사용해야 정상 동작했다.

---

## 4. 기존 운영 테이블

### 4.1 기존 주요 테이블

| 용도 | 테이블 |
|---|---|
| 매출 메인 | `h_hmfo.gd_dcube.01_sap_sales_custmasters` |
| 미출고 | `h_hmfo.gd_dcube.46_helo_periodic_unshipped` |
| 수익성 | `h_hmfo.gd_dcube.00_customers_cm` |
| 상품/품목 관련 일부 기능 | `h_hmfo.gd_dcube.01_sap_sales_items` |
| 가격/거래처 관련 일부 기능 | `h_hmfo.gd_dcube.02_sap_price_custmasters` |

### 4.2 코드 상수

현재 운영 코드의 주요 상수는 아직 기존 테이블을 바라본다.

| 파일 | 상수 | 현재 값 |
|---|---|---|
| `api/main.py` | `T_MAIN` | `h_hmfo.gd_dcube.01_sap_sales_custmasters` |
| `api/main.py` | `T_MISULGO` | `h_hmfo.gd_dcube.46_helo_periodic_unshipped` |
| `api/main.py` | `T_PROFIT` | `h_hmfo.gd_dcube.00_customers_cm` |
| `api/action_signals.py` | `T_MAIN` | 기존 매출 테이블 |
| `api/action_signals.py` | `T_MISULGO` | 기존 미출고 테이블 |
| `api/forecast_engine_v7.py` | `T_MAIN_FE` | 기존 매출 테이블 |

---

## 5. 챗봇 SQL bypass 및 Dify fallback 개선 이력

## 5.1 브랜드 매출 월 미명시 질의 fall-through 문제

### 문제

월을 명시하지 않은 브랜드 매출 질의가 서버에서 처리되지 않고 Dify로 넘어가는 문제가 있었다.

대표 질의:

- `신화푸드 매출액`

### 원인

월 미명시 브랜드 매출 처리 로직에서:

1. 당월 데이터 없음
2. fuzzy candidate 없음
3. `pass`로 다음 로직 진행
4. 이후 브랜드명이 영업사원명처럼 취급되어 Dify로 넘어감

### 조치

- 당월 데이터가 없으면 전월 데이터로 fallback
- fallback 결과를 즉시 return
- 더 이상 영업사원/Dify 로직으로 흘러가지 않도록 수정

커밋:

- `b1aaaae`

---

## 5.2 브랜드 매출 전월 fallback 카드 문구 수정

### 문제

당월 데이터가 없어 전월 데이터로 fallback했는데, 카드에는 `이번달 누계/예상`처럼 보이는 forecast 문구가 남아 있었다.

### 조치

- 전월 fallback에서는 forecast 카드 사용 중단
- 단순 매출액 카드로 응답
- 예: `5월 매출액 ... ※ 6월 데이터 미적재 — 5월 기준`

커밋:

- `eceac6d`

---

## 5.3 `사업부 5월 매출` bypass 실패 문제

### 문제

아래 질의 중 두 번째가 Dify로 넘어갔다.

| 질의 | 결과 |
|---|---|
| `외식식재사업부 5월 매출액` | 정상 bypass |
| `사업부 5월 매출액` | Dify로 넘어감 |

### 원인

월별 총매출 정규식이 `사업부` 앞에 명시적 텍스트가 있어야 매칭되도록 되어 있었다.

### 조치

- 단독 `사업부` 키워드를 `외식식재사업부`로 정규화
- 정규화된 문장으로 월별 총매출 패턴 매칭

커밋:

- `4370bfb`

---

## 6. 이전 세션 주요 개선 사항

이전 세션에서 이미 완료된 개선 사항은 다음과 같다.

### 6.1 수익성 QR/응답 개선

- 수익성 관련 quick reply/응답 흐름 수정
- CM, 공헌이익률, 수익성 질의를 통합적으로 인식하도록 개선
- 최신 수익성 기간을 동적으로 조회하도록 보완

관련 테이블:

- `h_hmfo.gd_dcube.00_customers_cm`

---

### 6.2 Sales Action Suggestion 모듈 개선

- 영업 액션 제안 신호 탐지 로직 보강
- `api/action_signals.py`와 `api/action_router.py` 중심으로 작업
- ITEM_CHURN, 미출고, 매출 저하/상승 등 액션 후보 탐지 구조 개선

주의:

- ITEM_CHURN 등 일부 로직은 `ZC본부명`, `ZB본지점명`, `거래처명`, `자재명`, `매출액`, `매출수량` 등에 의존한다.
- 신규 호환 뷰는 현재 `ZC본부명`이 공백이므로, 운영 전환 시 브랜드명 기반 액션 신호는 영향이 있다.

---

### 6.3 HTML report 개선

- 챗봇/리포트용 HTML 출력 품질 개선
- 보고서 포맷, 표기, 가독성 관련 보완

---

### 6.4 Decimal serialization 오류 수정

- Databricks SQL 결과의 Decimal 값이 JSON 직렬화에서 오류를 일으키는 문제 수정
- 응답 직렬화 과정에서 숫자 타입 처리 보강

---

### 6.5 Jinja2 cache workaround

- Jinja2 template cache 관련 문제 우회
- 템플릿 수정 후 반영되지 않거나 cache가 꼬이는 문제를 완화

---

### 6.6 ITEM_CHURN 개선

- 품목 이탈/중단 감지 로직 개선
- 매출수량/매출액 기준으로 거래 중단 또는 감소 신호를 탐지하는 구조 보완

---

### 6.7 미출고 fallback 날짜 표시 개선

- 미출고 데이터가 특정 기준일에 없을 경우 fallback 날짜를 명확히 표시
- 사용자에게 어떤 기준일 데이터인지 보여주도록 응답 개선

---

### 6.8 월별 브랜드 매출 bypass 디버깅

- 월별 브랜드 매출 질의가 Dify로 넘어가는 케이스 디버깅
- 현재 세션의 브랜드 매출 월 미명시 fallback 수정으로 이어짐

---

## 7. 신규 FSI 테이블 전환 검토

### 7.1 전환 배경

기존 테이블 지원 종료 예정:

- `01_sap_sales_custmasters`
- `46_helo_periodic_unshipped`

이에 따라 신규 FSI 테이블로 대체 가능성을 검토했다.

### 7.2 신규 후보 테이블

| 용도 | 신규 후보 테이블 |
|---|---|
| 매출 상세 | `h_hmfo_fsi.gd_rst_ing.sap_zsdr0008_sales_history_analysis_list_rst_ing_f` |
| 고객 마스터 | `h_hmfo_fsi.gd_rst_ing.sap_zsdrxd03_customer_master_rst_ing_d` |
| 미출고 | `h_hmfo_fsi.gd_fsi_ent.helo_periodic_unshipped_hist_f` |

### 7.3 검토 결과 요약

| 영역 | 판단 |
|---|---|
| 매출 테이블 대체 | 매출 상세 + 고객마스터 join 필요 |
| 미출고 테이블 대체 | 비교적 단순, 신규 테이블의 `총미출수량` 등 사용 필요 |
| 고객 계층 복원 | ZA/ZB/ZP는 self join으로 복원 가능 |
| ZC본부명 복원 | 현재 원천 부족, 공백 처리 결정 |
| 상품 그룹/기존자재번호 | 추가 상품/자재 마스터 필요 |

---

## 8. 신규 매출 호환 뷰 생성

### 8.1 생성 목적

코드 내 SQL 수정량을 최소화하기 위해 신규 FSI 매출 상세 테이블과 고객마스터를 조인하여 기존 `01_sap_sales_custmasters`와 같은 컬럼명을 갖는 호환 뷰를 만들었다.

최종 뷰:

`h_hmfo_fsi_dm.gd_rst_ing.chatbot_sales_custmasters_compat_v`

SQL 파일:

- `docs/create_chatbot_sales_custmasters_compat_v.sql`

검토 문서:

- `docs/sales_table_migration_review_20260605.md`

---

### 8.2 주요 매핑

| 기존 컬럼 | 신규 매핑 |
|---|---|
| `년도` | `date_format(s.대금청구일, 'yyyy')` |
| `년월` | `date_format(s.대금청구일, 'yyyyMM')` |
| `대금청구일` | `date_format(s.대금청구일, 'yyyyMMdd')` |
| `사업부` | `'20'` |
| `사업부명` | `'외식식재사업부'` |
| `부서` | `s.부서코드` |
| `부서명` | `s.부서명` |
| `지점` | `s.지점코드` |
| `지점명` | `s.지점명` |
| `거래처` | `LPAD(s.고객코드, 10, '0')` |
| `거래처명` | `s.고객명` |
| `영업사원` | `c.사원번호` |
| `영업사원명` | `c.영업사원명` |
| `ZA거래처` | `c.ZA대표거래처`, 없으면 `s.고객코드` |
| `ZA거래처명` | 고객마스터 self join |
| `ZB본지점` | `c.ZB본부`, 없으면 `s.고객코드` |
| `ZB본지점명` | 고객마스터 self join |
| `ZP대표고객` | `c.ZP본사`, 없으면 `s.고객코드` |
| `ZP대표고객명` | 고객마스터 self join |
| `ZC본부` | `c.FC본부` 우선, 없으면 `c.ZP본사`, 없으면 `s.고객코드` |
| `ZC본부명` | 공백 문자열 |
| `자재` | `s.상품코드` |
| `자재명` | `s.상품명` |
| `매출액` | `s.정가 / 100` |
| `매출원가` | `s.매출원가 / 100` |
| `매출수량` | `s.수량` |
| `단가` | `s.평균단가 / 100` |
| `플랜트` | `c.납품센터` |
| `플랜트명` | `c.납품센터명` |

---

### 8.3 추가 필요 컬럼

아래 컬럼은 현재 placeholder 상태다.

| 컬럼 | 현재 상태 | 필요 조치 |
|---|---|---|
| `ZC본부명` | 공백 | IT 원천 컬럼 또는 별도 매핑 필요 |
| `자재그룹` | `NULL` | 상품/자재 마스터 필요 |
| `자재그룹명` | `NULL` | 상품/자재 마스터 필요 |
| `기존자재번호` | `NULL` | 상품/자재 마스터 필요 |

---

### 8.4 권한/DDL 실행 이슈

#### 이슈 1: `/query` endpoint의 자동 SQL rewrite

`/query` endpoint는 기본적으로 `ZA거래처`를 `ZC본부`로 바꾸는 `_replace_za_with_zc`를 적용한다.

DDL에 `ZA거래처`와 `ZC본부`가 모두 있을 때 이 rewrite가 적용되어 컬럼 중복 오류가 발생했다.

오류:

```text
[COLUMN_ALREADY_EXISTS] The column zc본부 already exists
```

해결:

- DDL 실행 시 `main.run_query(sql_text, raw=True)` 사용

#### 이슈 2: 원래 목표 schema 권한 없음

처음 생성하려던 schema:

`h_hmfo_fsi.gd_rst_ing`

오류:

```text
PERMISSION_DENIED: User does not have CREATE TABLE on Schema 'h_hmfo_fsi.gd_rst_ing'
```

해결:

- 사용자 요청에 따라 `h_hmfo_fsi_dm.gd_rst_ing`에 생성

---

### 8.5 생성 후 검증

최종 생성 뷰:

`h_hmfo_fsi_dm.gd_rst_ing.chatbot_sales_custmasters_compat_v`

검증 결과:

| 검증 | 결과 |
|---|---:|
| 202604 전체 count | 2,044,849 |
| 외식3팀 샐러디 202604 count | 130,420 |
| 외식3팀 샐러디 202604 매출 | 2,916,730,688 |
| 외식3팀 샐러디 202604 수량 | 355,785.52 |
| 외식3팀 샐러디 202604 거래처 수 | 407 |
| 외식3팀 샐러디 202604 ZB본지점 수 | 407 |
| 외식3팀 샐러디 202604 ZC본부 수 | 3 |

---

## 9. 기존 테이블과 신규 호환 뷰 컬럼 비교

사용자가 신규 뷰 컬럼명이 기존 컬럼과 같은지 확인 요청했다.

비교 대상:

- 기존: `h_hmfo.gd_dcube.01_sap_sales_custmasters`
- 신규: `h_hmfo_fsi_dm.gd_rst_ing.chatbot_sales_custmasters_compat_v`

DESCRIBE 비교 결과:

| 항목 | 결과 |
|---|---:|
| 기존 컬럼 수 | 82 |
| 신규 컬럼 수 | 83 |
| 기존 컬럼 중 신규에 없는 컬럼 | 0 |
| 컬럼 순서 차이 | 0 |
| 컬럼 타입 차이 | 0 |
| 신규에만 추가된 컬럼 | `file_name` |

결론:

- 기존 SQL에서 사용하던 컬럼명은 모두 존재
- 컬럼 순서와 타입도 일치
- 대부분의 SQL은 `FROM` 대상만 바꾸면 동작하도록 설계됨

주의:

- `ZC본부명`, `자재그룹`, `자재그룹명`, `기존자재번호`는 값이 비어 있거나 `NULL`

---

## 10. Databricks Catalog에서 뷰 이름 변경 방법 안내

사용자가 신규 뷰 이름을 어디서 바꾸는지 문의했다.

안내한 경로:

1. Databricks 접속
2. 왼쪽 메뉴 `Catalog`
3. Catalog: `h_hmfo_fsi_dm`
4. Schema: `gd_rst_ing`
5. View: `chatbot_sales_custmasters_compat_v`
6. 우측 상단 또는 더보기 메뉴에서 `Rename`

SQL 방식:

```sql
ALTER VIEW h_hmfo_fsi_dm.gd_rst_ing.chatbot_sales_custmasters_compat_v
RENAME TO h_hmfo_fsi_dm.gd_rst_ing.새이름;
```

대안:

```sql
CREATE OR REPLACE VIEW h_hmfo_fsi_dm.gd_rst_ing.새이름 AS
SELECT *
FROM h_hmfo_fsi_dm.gd_rst_ing.chatbot_sales_custmasters_compat_v;

DROP VIEW h_hmfo_fsi_dm.gd_rst_ing.chatbot_sales_custmasters_compat_v;
```

---

## 11. 신규/기존/중단 거래처 기준 정리

### 11.1 최초 제안 기준

처음에는 일반적인 기준을 제안했다.

| 구분 | 최초 제안 기준 |
|---|---|
| 신규 | 기준연도 매출 있음 + 이전 전체 매출 이력 없음 |
| 기존 | 기준연도 매출 있음 + 이전 매출 이력 있음 |
| 중단 | 전년도 매출 있음 + 기준연도 매출 없음 |

### 11.2 사용자 정정

사용자는 챗봇의 신규 매출 기준이 다음과 같다고 정정했다.

`전년 10월 이후 최초 매출 발생`

### 11.3 최종 확정 기준

기준연도 `Y` 기준:

| 구분 | 최종 기준 |
|---|---|
| 신규 | `Y`년 매출 있음 + 최초 매출일이 `Y-1년 10월 1일` 이후 |
| 기존 | `Y`년 매출 있음 + 최초 매출일이 `Y-1년 10월 1일` 이전 |
| 중단 | `Y-1`년 매출 있음 + `Y`년 매출 없음 |

예: 2026년 기준

| 구분 | 조건 |
|---|---|
| 신규 | 2026년 매출 있음 + 최초 매출일 `>= 2025-10-01` |
| 기존 | 2026년 매출 있음 + 최초 매출일 `< 2025-10-01` |
| 중단 | 2025년 매출 있음 + 2026년 매출 없음 |

사용자는 중단 기준을 `전년도 매출 있음 + 해당 연도 매출 없음`으로 확정했다.

---

## 12. 2020~2025 신규/기존/중단 거래처 Excel 산출

### 12.1 1차 요청

사용자 요청:

- 2020년부터 2025년까지 연도별 시트 생성
- 컬럼 구성:
  - `ZC코드`
  - `ZC코드명`
  - `ZA코드`
  - `ZA코드명`
  - `신규기존중단여부`
- `ZC코드`가 8로 시작하지 않으면 전부 `개인형`으로 치환
- 2019년 데이터도 있으므로 2020년 판단에 사용 가능

### 12.2 1차 산출 결과

출력 파일:

`exports/customer_lifecycle_2020_2025.xlsx`

시트:

- `2020`
- `2021`
- `2022`
- `2023`
- `2024`
- `2025`
- `summary`

1차 요약:

| 연도 | 기존 | 신규 | 중단 | 합계 |
|---:|---:|---:|---:|---:|
| 2020 | 648 | 761 | 325 | 1,734 |
| 2021 | 880 | 714 | 386 | 1,980 |
| 2022 | 990 | 548 | 508 | 2,046 |
| 2023 | 1,054 | 568 | 401 | 2,023 |
| 2024 | 1,082 | 769 | 420 | 2,271 |
| 2025 | 1,226 | 672 | 494 | 2,392 |

검증:

| 항목 | 결과 |
|---|---:|
| 전체 행 수 | 12,446 |
| `개인형` 치환 행 수 | 9,065 |
| `개인형`이 아닌데 ZC코드가 8로 시작하지 않는 오류 | 0 |

---

## 13. 2020~2025 Excel 산출 기준 변경: FC/ZC 기준

### 13.1 사용자 추가 요청

사용자가 추가로 기준을 변경했다.

요청 내용:

1. `개인형/FC` 컬럼 추가
2. `ZC코드`가 8로 시작하면 `FC`, 아니면 `개인형`
3. `FC`인 경우:
   - `ZA코드`, `ZA코드명` 비움
   - `ZC코드` 기준으로 신규/기존/중단 판단
   - 연도별 같은 `ZC코드`는 한 줄만 나오게 처리
4. `개인형`인 경우:
   - `ZA코드` 기준으로 신규/기존/중단 판단
   - `ZC코드`, `ZC코드명`은 `개인형`

### 13.2 최종 컬럼

| 순서 | 컬럼 |
|---:|---|
| 1 | `개인형/FC` |
| 2 | `ZC코드` |
| 3 | `ZC코드명` |
| 4 | `ZA코드` |
| 5 | `ZA코드명` |
| 6 | `신규기존중단여부` |

### 13.3 파일 잠금 이슈

기존 Excel 파일이 열려 있어 덮어쓰기가 실패했다.

오류:

```text
PermissionError: [Errno 13] Permission denied: 'E:\git-copilot\dify-practice\exports\customer_lifecycle_2020_2025.xlsx'
```

조치:

- 파일이 열려 있어 덮어쓰기가 안 될 경우 timestamp가 붙은 새 파일로 저장하도록 스크립트 보완

### 13.4 최종 산출 파일

`exports/customer_lifecycle_2020_2025_20260605_164125.xlsx`

최종 요약:

| 연도 | 기존 | 신규 | 중단 | 합계 |
|---:|---:|---:|---:|---:|
| 2020 | 603 | 717 | 316 | 1,636 |
| 2021 | 815 | 665 | 374 | 1,854 |
| 2022 | 899 | 482 | 499 | 1,880 |
| 2023 | 941 | 505 | 376 | 1,822 |
| 2024 | 967 | 686 | 371 | 2,024 |
| 2025 | 1,093 | 586 | 450 | 2,129 |

검증 결과:

| 검증 항목 | 결과 |
|---|---:|
| `FC`인데 `ZA코드` 또는 `ZA코드명`이 채워진 건 | 0 |
| `FC` 내 연도별 `ZC코드` 중복 | 0 |
| `개인형/FC` 구분과 `ZC코드` 규칙 불일치 | 0 |

---

## 14. 생성/수정된 문서 및 산출물

| 파일 | 설명 |
|---|---|
| `docs/create_chatbot_sales_custmasters_compat_v.sql` | 신규 FSI 기반 호환 매출 뷰 생성 SQL |
| `docs/sales_table_migration_review_20260605.md` | 신규 매출 테이블 전환 검토 문서 |
| `docs/conversation_summary_20260605.md` | 2026-06-05 당일 대화 요약 |
| `docs/full_conversation_summary_until_20260605.md` | 지금까지의 전체 대화/작업 이력 종합 정리 |
| `scripts/export_customer_lifecycle_2020_2025.py` | 신규/기존/중단 거래처 Excel 산출 스크립트 |
| `exports/customer_lifecycle_2020_2025.xlsx` | 1차 거래처 lifecycle 산출물 |
| `exports/customer_lifecycle_2020_2025_20260605_164125.xlsx` | FC/ZC 기준 반영 최종 산출물 |

---

## 15. 주요 커밋 기록

| 커밋 | 내용 |
|---|---|
| `b1aaaae` | 월 미명시 브랜드 매출 당월 데이터 없음 시 전월 fallback |
| `eceac6d` | 브랜드 매출 전월 fallback 시 forecast 카드 대신 단순 포맷 사용 |
| `4370bfb` | 사업부 단독 키워드를 외식식재사업부로 정규화 |
| `2b8e274` | 신규 매출 조인 호환 테이블 검토 및 생성 SQL 추가 |
| `16d4a3c` | 호환 매출뷰 생성 스키마를 `h_hmfo_fsi_dm`로 변경 |

---

## 16. 현재 Git/작업 상태 관련 주의

작업 중 확인된 Git 상태에는 다음 항목이 있었다.

| 항목 | 상태 | 비고 |
|---|---|---|
| `api/.token_cache` | modified | 로컬 Databricks 인증 토큰 캐시, 커밋 비추천 |
| `api/action_store.db` | untracked | 로컬 runtime DB로 보임, 커밋 비추천 |
| `exports/` | untracked | Excel 산출물 포함 |
| `scripts/` | untracked | Excel 산출 스크립트 포함 |

주의:

- `.token_cache`는 인증정보 관련 파일이므로 커밋하지 않는 것이 안전하다.
- `action_store.db`도 runtime/local data 성격이면 커밋하지 않는 것이 좋다.
- `exports/`는 산출물 보관 정책에 따라 커밋 여부를 결정하면 된다.
- `scripts/export_customer_lifecycle_2020_2025.py`는 재사용 가능성이 있으므로 필요 시 커밋 대상이다.

---

## 17. 남은 작업 및 리스크

### 17.1 운영 테이블 전환 미완료

아직 운영 코드가 신규 호환 뷰를 사용하도록 변경되지는 않았다.

전환 대상 후보:

| 파일 | 변경 대상 |
|---|---|
| `api/main.py` | `T_MAIN` |
| `api/action_signals.py` | `T_MAIN` |
| `api/forecast_engine_v7.py` | `T_MAIN_FE` |
| `api/main.py` | Dify prompt 내 테이블명 안내 |

### 17.2 `ZC본부명` 공백 문제

신규 호환 뷰의 `ZC본부명`은 현재 공백이다.

영향:

- `ZC본부명 LIKE '%샐러디%'` 같은 브랜드명 기반 SQL은 신규 뷰에서 동작하지 않음
- 코드 기반 `ZC본부 IN (...)` 방식은 가능

필요 조치:

- IT에 `ZC본부명` 원천 컬럼 요청
- 또는 별도 ZC 코드명 매핑 테이블 확보

### 17.3 상품/자재 컬럼 미보강

현재 `NULL` placeholder인 컬럼:

- `자재그룹`
- `자재그룹명`
- `기존자재번호`

필요 조치:

- 상품/자재 마스터 확보
- 호환 뷰에 추가 join 적용

### 17.4 미출고 테이블 전환 미구현

기존 미출고 테이블:

`h_hmfo.gd_dcube.46_helo_periodic_unshipped`

신규 후보:

`h_hmfo_fsi.gd_fsi_ent.helo_periodic_unshipped_hist_f`

상태:

- 대체 가능성은 확인
- 운영 코드 전환은 아직 미수행
- 신규 컬럼 기준으로 SQL 수정 필요

### 17.5 신규 호환 뷰 기반 regression test 필요

운영 전환 전 다음 질의군 테스트가 필요하다.

- 브랜드 월별 매출
- 월 미명시 브랜드 매출
- 사업부 월별 매출
- 영업사원 매출
- 지점/부서 매출
- 상품/품목 매출
- 미출고 관련 질의
- 액션 제안/ITEM_CHURN
- 수익성 질의와의 상호 영향

---

## 18. 최종 요약

지금까지의 작업으로 다음을 완료했다.

1. 챗봇 주요 매출 질의가 Dify로 잘못 넘어가는 문제들을 수정했다.
2. 브랜드 매출 fallback 응답 문구를 명확하게 개선했다.
3. `사업부` 단독 질의를 `외식식재사업부`로 정규화하여 bypass되도록 했다.
4. 기존 매출 테이블 지원 종료에 대비해 신규 FSI 매출 상세 + 고객마스터 기반 호환 뷰를 설계했다.
5. `h_hmfo_fsi_dm.gd_rst_ing.chatbot_sales_custmasters_compat_v` 뷰를 실제 생성했다.
6. 기존 `01_sap_sales_custmasters`와 컬럼명/순서/타입 호환성을 검증했다.
7. 2020~2025년 신규/기존/중단 거래처 산출 기준을 확정했다.
8. FC는 ZC 기준, 개인형은 ZA 기준으로 신규/기존/중단을 판단하는 최종 Excel을 생성했다.
9. 전체 작업 내용을 문서화했다.

가장 큰 남은 의사결정은 운영 코드를 신규 호환 뷰로 전환할지 여부이며, 전환 전 `ZC본부명`과 상품/자재 관련 placeholder 컬럼의 영향 범위를 반드시 점검해야 한다.

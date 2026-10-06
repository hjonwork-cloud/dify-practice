# 동원홈푸드 SMS 발송 브릿지 (Chrome/Edge 확장)

포털에서 "DM 발송" 버튼을 누르면, **이 확장이 설치된 PC에서는** 사용자가 이미
`direct.dongwon.com`에 로그인해서 갖고 있는 세션 쿠키를 그대로 이용해 확장이
직접 SMS를 발송합니다. 서버(Azure)는 이 발송을 대신 시도하지 않고, 확장이
보고한 성공/실패 결과만 로그에 기록합니다. **확장이 없는 PC는 기존과 동일하게
서버가 관리자 등록 쿠키로 발송**합니다(자동 폴백, 코드 변경 불필요).

## 왜 CORS 문제가 없는가
- 일반 웹페이지의 `fetch()`는 응답에 `Access-Control-Allow-Origin` 헤더가 없으면
  브라우저가 차단합니다 (`direct.dongwon.com`은 이 헤더를 아예 보내지 않는 legacy
  ASP.NET 사이트라 브라우저 페이지 JS로는 절대 호출 불가 — 이미 검증됨).
- 확장 프로그램의 **background service worker**는 `manifest.json`의
  `host_permissions`에 등록된 도메인에 한해 이 CORS 검사를 받지 않습니다
  (Chrome/Edge 확장 API의 공식 동작). 그래서 `background.js`가 대신 호출합니다.
- `credentials: 'include'` 옵션 덕분에, 사용자가 평소 `direct.dongwon.com`에
  로그인되어 있으면(그룹웨어 상시 로그인 가정과 일치) 그 세션 쿠키가 자동으로
  실려서 발송됩니다. **쿠키를 읽거나 저장하거나 어디로 전송하지 않습니다.**

## 파일 구성
- `manifest.json` — 확장 설정. `key` 필드에 고정 공개키가 들어있어 **어느 PC에서
  로드하든 항상 같은 확장 ID(`bndgknhcmfakobpppgndlpdloalmkbne`)**를 가집니다.
  이 ID는 포털 프론트(`portal_brand_report_action.html`)의 `SMS_EXT_ID` 상수와
  반드시 일치해야 통신이 됩니다.
- `background.js` — 실제 발송 로직 (외부 페이지의 메시지를 받아 Direct에 발송).
- `../_ext_private_key.pem` (확장 폴더 밖, `dify-practice/` 루트) — 위 고정 ID를
  만든 개인키. **이 폴더 안에 두면 Chrome이 "키 파일을 포함합니다" 경고를 띄우고
  향후 배포/패키징 시 문제가 될 수 있어 의도적으로 폴더 밖에 둠.** git에도 커밋되지
  않습니다(`.gitignore`에 `*.pem` 추가됨). 분실 시 `../_ext_gen_key.py`를 다시
  돌리면 되지만, 그러면 ID가 바뀌므로 포털 프론트의 `SMS_EXT_ID`도 같이 바꿔야
  합니다. **분실하지 않도록 안전한 곳에 별도 백업 권장.**
- `../_ext_gen_key.py` (확장 폴더 밖) — 위 키/ID를 생성한 1회성 스크립트 (참고용,
  재실행 불필요).

## 사용자 설치 방법 (포털 로그인 팝업)
확장이 없거나 구버전인 사용자가 포털에 로그인하면 **설치 안내 팝업**이 뜹니다
(`api/templates/portal_base.html`, 조회 전용 계정 제외).
1. 팝업의 **[설치 파일 다운로드]** → `GET /portal/sms-extension/download?browser=chrome|edge`
   (`sms-bridge/` 확장 폴더 + `SETUP.bat` + `README.txt` 를 서버가 즉석 ZIP으로 생성)
2. ZIP '모두 압축 풀기' → `SETUP.bat` 더블클릭
   - 확장 파일을 `%LOCALAPPDATA%\DWHF\SmsBridge` 로 복사(다운로드 폴더 정리로 지워지지 않게)
   - 그 경로를 클립보드에 복사 + 브라우저 확장 관리 페이지 자동 오픈
3. '개발자 모드' 켜기 → '압축해제된 확장 프로그램 로드' → 폴더 선택 창에 Ctrl+V → [폴더 선택]
4. 포털로 돌아와 **[설치 확인]** (페이지 새로고침 후 ping으로 검증)

업데이트: `manifest.json`의 `version`을 올리고 배포하면, 구버전 사용자에게 팝업이 다시 뜹니다.
같은 경로에 덮어쓰므로 `SETUP.bat` 재실행 후 확장 카드의 새로고침(↻)만 누르면 됩니다.

> ⚠️ 브라우저 보안 정책상 웹페이지가 확장을 '클릭 1번'으로 설치하는 것은 불가능합니다.
> 진짜 원클릭/무조작 설치는 ① Chrome 웹 스토어 '비공개(Unlisted)' 등록 또는
> ② IT 정책(`ExtensionInstallForcelist`) 배포가 필요합니다(아래 '원클릭 설치' 참고).

## 원클릭 설치 (Chrome + Edge 공용) — 스토어 등록
| 경로 | Chrome | Edge | 비고 |
|---|---|---|---|
| Chrome 웹 스토어 (Unlisted) | 클릭 2번 | 최초 1회 '다른 스토어 허용' + 클릭 2번 | 등록비 US$5(1회), 1개 등록으로 두 브라우저 커버 |
| + Edge 추가 기능 스토어 (Hidden) | – | 클릭 2번 | 등록 무료, Edge 사용자 비중이 높으면 추가 |
| IT 정책 강제 설치 | 0번 | 0번 | 아래 레지스트리/GPO, 스토어 ID 필요 |

1. 패키지 생성: `python scripts/build_sms_ext_store_package.py` → `dist/sms-bridge-store-v{버전}.zip`
   (`key` 제거, localhost 제거, 아이콘 추가, 설명 132자 제한 반영)
2. 같은 ZIP을 Chrome 웹 스토어 개발자 대시보드(공개 범위 **비공개/Unlisted**)와
   Edge Partner Center(공개 범위 **숨김/Hidden**, 선택)에 업로드
   - 개인정보처리방침 URL: `https://<포털>/portal/sms-extension/privacy`
   - 권한 사유: direct.dongwon.com 세션 예열(tabs/scripting/webNavigation), Referer 보정(declarativeNetRequest),
     세션 진단(cookies/webRequest) — 심사 반려 시 진단용 권한(cookies, webRequest, declarativeNetRequestFeedback) 제거 검토
3. **심사 "제출 완료" ≠ "게시 완료"다.** 대시보드 상태가 "검토 중"인 동안은 ID가 있어도 아직 사용자가
   접근 가능한 공개 페이지가 아니므로, 원클릭 버튼을 켜면 클릭 시 오류 화면을 보게 된다. 그래서 ID 등록과
   버튼 노출을 분리했다 — App Service 환경변수에 ID를 미리 넣어둬도 아래 스위치가 꺼져 있으면 포털은
   계속 ZIP(설치 파일) 안내만 보여준다:
   - `SMS_EXT_CWS_ID=<웹 스토어 ID>` / `SMS_EXT_EDGE_ID=<Edge 스토어 ID>`(선택, 미리 넣어둬도 무방)
   - `SMS_EXT_MIN_VERSION=<요구 버전>`(선택: 스토어 심사 대기 중 저장소 버전이 앞설 때 '업데이트 필요' 오표시 방지)
   - `SMS_EXT_STORE_ENABLED=1` ← **대시보드 상태가 "게시됨/Published"로 바뀐 걸 직접 확인한 뒤에만** 추가.
     이 값이 없으면 위 ID가 설정돼 있어도 원클릭 버튼은 계속 숨겨지고 ZIP 설치 안내만 노출된다.
4. 포털은 스토어본/개발자 모드본 ID를 모두 ping 해서 응답하는 쪽을 사용하므로 기존 ZIP 설치자도 그대로 동작합니다.
   스토어본은 새 버전이 **자동 업데이트**되고, 개발자 모드 경고 팝업도 뜨지 않습니다.

### 무조작 설치 (IT 협조) — 관리자 권한 레지스트리 또는 GPO
```reg
Windows Registry Editor Version 5.00

[HKEY_LOCAL_MACHINE\SOFTWARE\Policies\Google\Chrome\ExtensionInstallForcelist]
"1"="<웹 스토어 ID>;https://clients2.google.com/service/update2/crx"

[HKEY_LOCAL_MACHINE\SOFTWARE\Policies\Microsoft\Edge\ExtensionInstallForcelist]
"1"="<웹 스토어 ID>;https://clients2.google.com/service/update2/crx"
; Edge 스토어에 등록했다면 위 대신: "1"="<Edge 스토어 ID>;https://edge.microsoft.com/extensionwebstorebase/v1/crx"
```
사용자는 브라우저 재시작만 하면 설치가 끝나고 삭제할 수도 없습니다. 스토어 없이 자체 서버(.crx 호스팅)로 강제 설치하는 방식은
도메인 가입/관리 PC에서만 허용되므로, 스토어 ID 방식이 가장 단순합니다.

## 스토어 등록 제출 텍스트 (그대로 복사해서 쓰면 됨)
Chrome 웹 스토어 개발자 대시보드 "항목 수정" 화면에서 아래 항목들이 비어 있으면 게시가 막힌다.
각 필드에 해당하는 문구를 아래에 정리해 둔다(Edge Partner Center도 거의 동일한 항목 구성).

### 1) 스토어 등록정보(Store listing) 탭
- **카테고리**: 생산성(Productivity) — 업무용 내부 도구이므로 이 카테고리가 가장 적합.
- **언어**: 한국어 (추가로 등록할 언어 없으면 한국어 1개만 선택해도 통과됨)
- **아이콘(128×128)**: `dist/sms-bridge-store-icon128.png` 업로드
  (`python scripts/build_sms_ext_store_package.py` 실행 시 함께 생성됨)
- **스크린샷(1280×800, 1장 이상 필수)**: `python scripts/build_sms_ext_store_screenshots.py` 실행 →
  `dist/store_screenshot_1.png`, `dist/store_screenshot_2.png` 생성. 2장 모두 업로드 권장.
- **상세 설명(Description)**:
  ```
  동원홈푸드 세일즈 액션 플랫폼(Sales Action Platform) 전용 내부 업무 확장 프로그램입니다.

  ■ 무엇을 하나요?
  세일즈 액션 플랫폼에서 영업담당자가 가맹점에 보내는 안내 문자(SMS/LMS) 발송 버튼을 누르면,
  이 확장 프로그램이 현재 브라우저에 이미 로그인되어 있는 사내 Direct(direct.dongwon.com)
  세션을 그대로 사용하여 문자 발송 요청을 대신 전달합니다.

  ■ 왜 필요한가요?
  사내 문자 발송 시스템(Direct)은 로그인한 사용자 본인만 발송 권한을 가집니다. 이 확장이
  없으면 포털 서버가 공용 계정으로 대신 발송해야 해서, 발신 이력이 실제 담당자 기준으로
  남지 않고 공용 세션이 끊기면 발송이 지연될 수 있습니다.

  ■ 개인정보 처리
  - 수신번호, 문자 내용 등은 오직 direct.dongwon.com 으로 전달하는 데에만 사용되며
    별도로 저장하거나 외부로 전송하지 않습니다.
  - 쿠키 값을 읽어 저장하거나 서버로 전송하지 않습니다. 브라우저가 가진 로그인 세션을
    그대로 '이용'만 합니다.
  - 사내 Direct, 그룹웨어(dongwon.net), 세일즈 액션 플랫폼 도메인 외에는 어떤 요청도
    보내지 않습니다.

  ■ 사용 대상
  동원홈푸드 임직원 중 세일즈 액션 플랫폼을 사용하는 영업담당자 전용입니다.
  ```

### 2) 개인정보 보호 관행(Privacy practices) 탭
- **단일 목적 설명(Single purpose description)**:
  ```
  이 확장 프로그램은 동원홈푸드 세일즈 액션 플랫폼(사내 포털)에서 영업담당자가 요청한 안내
  문자(SMS/LMS)를, 사용자가 이미 사내 Direct 시스템(direct.dongwon.com)에 로그인되어 있는
  '본인의' 브라우저 세션을 그대로 사용해 전달하는 단 하나의 기능만 수행합니다. 그 외의
  기능은 없습니다.
  ```
- **원격 코드 사용 이유(Are you using remote code?)**: "예"를 선택했다면 사유란에 아래 입력
  (eval/원격 스크립트 삽입이 전혀 없으므로 "아니오"로 바꿀 수 있는지도 먼저 확인할 것):
  ```
  이 확장은 외부에서 코드를 가져와 실행하지 않습니다(eval, new Function, 원격 스크립트
  삽입 없음). 다만 세션 예열을 위해 scripting API로 사용자가 클릭한 포털 탭에서
  "window.open(URL)" 한 줄만 실행하여 direct.dongwon.com 페이지를 새 탭으로 엽니다. 이는
  일반적인 웹 탐색이며, 해당 탭의 콘텐츠는 그 사이트 자신의 오리진에서 로드·실행되어
  확장 프로그램의 코드 실행 권한 범위 밖에서 동작합니다. 확장의 실행 코드는 패키지에 포함된
  background.js 한 파일로만 구성되어 있으며, 런타임에 외부 코드를 주입하지 않습니다.
  ```
- **호스트 권한 사용 이유(Host permission justification)**:
  ```
  호스트 권한은 사내 문자 발송 시스템(direct.dongwon.com), 사내 그룹웨어 SSO(dongwon.net,
  dongwon.com), 세일즈 액션 플랫폼 포털(Azure App Service 도메인) 으로 한정되어 있습니다.
  이 권한은 (1) 사용자의 기존 로그인 세션 쿠키로 Direct에 발송 요청을 전달하고, (2) 세션이
  끊어졌을 때 로그인 체인(SSO)이 정상 완료되는지 확인하며, (3) 포털에서 보낸 발송 요청만
  수신하기 위해 필요합니다. 그 외 도메인에는 어떤 요청도 보내지 않습니다.
  ```
- **개별 권한 사유(각 permission 옆 입력란)**:

  | 권한 | 사유 |
  |---|---|
  | tabs | 세션 예열을 위해 Direct 로그인 페이지를 새 창(팝업)으로 열고, 그 탭이 로드를 완료했는지 확인하기 위해 사용합니다. 탭의 URL 전환 여부만 확인하며 페이지 내용을 읽지 않습니다. |
  | cookies | direct.dongwon.com 에 유효한 로그인 세션 쿠키가 존재하는지 확인하는 용도로만 사용합니다. 쿠키 값을 외부 서버로 전송하거나 디스크에 저장하지 않습니다. |
  | webNavigation | 세션 예열용 탭이 로그인 → SSO 인증 → 콜백의 리디렉션 체인을 거쳐 목적 페이지(SMS 발송 화면)까지 정상적으로 도달했는지 진단하기 위해 사용합니다. 사용자의 다른 브라우징 활동은 추적하지 않습니다. |
  | webRequest | 세션 예열 시 서버가 인증 쿠키(Set-Cookie)를 정상적으로 내려주는지 읽기 전용으로 진단하기 위해 사용합니다. 요청을 가로채거나 내용을 변경하지 않습니다(변경은 declarativeNetRequest로만 수행). |
  | scripting | 세션 예열을 위해 사용자가 클릭한 포털 탭에서 "window.open()" 한 줄을 실행해 Direct 로그인 팝업을 띄우는 데에만 사용합니다. 페이지의 다른 내용을 읽거나 수정하지 않습니다. |
  | declarativeNetRequestWithHostAccess | 세션 예열 요청이 사내 SSO의 오픈 리다이렉트 방지 로직을 통과하도록, direct.dongwon.com·www.dongwon.net 으로 가는 요청의 Referer 헤더를 신뢰 가능한 사내 페이지 주소로 보정하는 데에만 사용합니다. 그 외 요청 내용은 변경하지 않습니다. |
  | declarativeNetRequestFeedback | 위 Referer 보정 규칙이 실제로 적용되고 있는지 진단(디버깅)하기 위해서만 사용하며, 수집한 정보를 외부로 전송하지 않습니다. |

- **데이터 사용 인증(Certify data usage compliance)**: 위 설명대로 데이터를 사내 발송 목적 외에
  사용/판매/공유하지 않으므로 체크박스에 동의 표시.
- **개인정보처리방침 URL**: `https://<포털 도메인>/portal/sms-extension/privacy`
  (이미 `GET /portal/sms-extension/privacy` 로 구현되어 있음 — 로그인 없이 접근 가능)

### 3) 계정 설정(Account) — 게시자 연락처 이메일
대시보드 좌측 "설정" 메뉴에서 연락처 이메일을 등록하고, 수신 메일의 인증 링크를 클릭해야
"이메일 미인증" 오류가 사라진다. 담당자 사내 메일(예: AI/플랫폼 운영 담당자)로 등록 권장.

## 광고성 정보 컴플라이언스 (v1.1.0~)
- 포털 화면: `[광고]` 접두어와 `무료수신거부 : <링크>` 는 편집 불가 영역(🔒)으로 분리되고,
  본문만 수정할 수 있습니다.
- 발송 직전 `POST /portal/dm-prepare` 가 수신거부/발송시간을 검증하고 **최종 문구를 서버가 확정**합니다.
  본문에 사용자가 넣은 [광고]/수신거부 문구는 제거 후 정규 문구로 재조립(`_apply_ad_compliance`).
- 확장(`background.js`의 `isAdCompliant`)도 `[광고]`로 시작하고 맨 끝이
  `무료수신거부 : https://…/portal/o/…` 가 아니면 발송을 거부합니다(이중 안전장치).
- 서버 로그 단계에서 정규 문구와 다르면 `compliance_mismatch` 로 기록합니다.

## 테스트(파일럿) 설치 방법 — 개발자 모드
1. Chrome/Edge 주소창에 `chrome://extensions` (Edge는 `edge://extensions`) 입력
2. 우측 상단 "개발자 모드" 켜기
3. "압축해제된 확장 프로그램을 로드" 클릭 → 이 폴더
   (`dify-practice/browser-extension-sms-bridge`) 선택
4. 목록에 "동원홈푸드 SMS 발송 브릿지"가 뜨면 설치 완료. ID가
   `bndgknhcmfakobpppgndlpdloalmkbne` 인지 확인 (다르면 `key` 필드가 누락된 것).
5. `https://direct.dongwon.com` 에 한 번 로그인해둔 상태에서, 포털 DM 발송 버튼을
   눌러 정상 발송되는지 확인.

## 전사 배포(향후, IT 협의 필요)
개발자 모드 수동 설치는 파일럿용입니다. 실제 전사 배포는 사내 IT가 이미 쓰는
Chrome/Edge 관리 정책(그룹 정책 `ExtensionInstallForcelist` 또는 Chrome
Enterprise/Intune)으로 **사용자 조작 없이 자동 설치**할 수 있습니다. 이 경우도
`manifest.json`의 `key`가 고정되어 있어 배포 방식이 바뀌어도 확장 ID가 동일하게
유지되므로 포털 코드 수정이 필요 없습니다. 사내 정책 기반 배포를 위해서는
1) 확장 파일을 사내에서 접근 가능한 위치(사내 웹서버 또는 CRX 업데이트 매니페스트)에
   호스팅해야 하며, 2) 해당 정책 설정은 IT의 협조가 필요합니다.

## 프론트/백엔드 연동 지점
- 프론트: `api/templates/portal_brand_report_action.html`의
  `_trySendSmsViaExtension()` — DM 발송 시 먼저 확장에 `ping`을 보내 설치 여부를
  확인하고, 있으면 확장으로 실제 발송, 없으면 `null`을 반환해 서버 폴백을 유도.
- 백엔드: `api/portal_router.py`의 `dm_send_with_price()` — 요청에
  `client_sms_result`가 포함되어 있으면(확장이 이미 발송) 서버는 재발송하지 않고
  그 결과만 로그(`dm_send_logs`)에 반영. 없으면 기존처럼 서버가 공유 쿠키로 발송.

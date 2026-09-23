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

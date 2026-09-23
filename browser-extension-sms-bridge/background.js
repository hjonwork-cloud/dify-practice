/*
 * SMS 발송 브릿지 - background service worker (Manifest V3)
 * ─────────────────────────────────────────────────────────
 * 역할: 포털 페이지(externally_connectable에 등록된 origin)에서 보낸 메시지를 받아,
 *       이 브라우저에 "이미 로그인되어 있는" direct.dongwon.com 세션 쿠키를 그대로
 *       사용해 SMS 발송(Send_SMS) 요청을 대신 수행한다.
 *
 * 왜 CORS에 걸리지 않는가?
 *   - 일반 웹페이지의 fetch()는 브라우저가 응답의 Access-Control-Allow-Origin 헤더를
 *     검사해서 차단하지만, 확장 프로그램의 background(service worker)에서 보내는
 *     요청은 manifest의 host_permissions에 등록된 호스트에 한해 이 CORS 검사가
 *     적용되지 않는다(Chrome/Edge 확장 API의 공식 동작). 이미 사내에 배포된 다른
 *     확장(예: Teams/Copilot 브릿지)도 동일한 externally_connectable 패턴을 사용한다.
 *   - fetch에 credentials:'include' 를 쓰면 direct.dongwon.com 도메인에 대해
 *     브라우저가 보유한 쿠키(ASP.NET_SessionId, Smart2Application)가 자동으로 첨부된다.
 *     이 쿠키는 사용자가 평소 그룹웨어/Direct에 로그인되어 있으면 이미 존재하므로,
 *     별도 로그인 자동화나 쿠키 저장이 전혀 필요 없다.
 *
 * 보안 설계:
 *   - 쿠키 값을 직접 읽거나(chrome.cookies API) 어딘가에 저장/전송하지 않는다.
 *     fetch가 브라우저 쿠키 저장소를 통해 "간접적으로만" 사용하도록 한다.
 *   - externally_connectable.matches 로 등록된 포털 origin에서 보낸 메시지만 처리한다.
 *   - host_permissions 은 direct.dongwon.com 하나로 최소화한다.
 */

const SMS_SERVER_URL = "https://direct.dongwon.com/website/Common/SMS/SMS_Service.aspx?CFN_OpenLayerName=SMS_Service&popupType=popup";
const SMS_API_URL = "https://direct.dongwon.com/website/Common/SMS/SMS_Service.aspx/Send_SMS";

/** Direct 응답 본문에서 인증 만료/무효 여부를 판별 (백엔드 로직과 동일 기준) */
function looksLikeAuthError(bodyText) {
  return (
    bodyText.includes("Error Notice") ||
    bodyText.includes("찾으시려는 웹페이지") ||
    bodyText.includes("Login.aspx")
  );
}

/** 실제 Send_SMS 호출 (msg가 빈 문자열이면 no-op 유효성 검사용으로도 사용 가능)
 *
 * ⚠️ 팝업 페이지(SMS_Service.aspx)를 먼저 로드하지 않고 곧바로 Send_SMS를 호출하면
 *    "인증 만료"처럼 보이는 오류가 발생하는 현상이 관찰됨(로그인 세션 자체는 유효한데도
 *    발생) — 그룹웨어에서 SMS 발송 팝업을 먼저 띄운 뒤 포털에서 발송하면 성공하는 것과
 *    일치. 즉 Page_Load 시점에 서버 세션에 뭔가(토큰/상태값)가 설정되어야 Send_SMS가
 *    정상 동작하는 것으로 추정됨. 그래서 매번 발송 전 팝업 페이지를 GET으로 먼저
 *    "예열"해서 이 상태를 만들어준다.
 */
async function warmUpSession() {
  try {
    await fetch(SMS_SERVER_URL, { method: "GET", credentials: "include" });
  } catch (e) {
    // 예열 실패해도 일단 발송은 시도해본다 (네트워크 일시 오류일 수 있음)
  }
}

async function callSendSms({ phone, message, callback, msgType, scheduledAt }) {
  await warmUpSession();

  const payload = {
    pNumsCount: phone ? "1" : "0",
    pDstaddr: (phone || "").replace(/[^0-9]/g, ""),
    pMsg: message || "",
    pMsgType: msgType || (message && message.length > 80 ? "2" : "1"),
    pRequestTime: scheduledAt || "",
    pCallBack: (callback || "").replace(/[^0-9-]/g, ""),
    pPath: "",
  };

  const resp = await fetch(SMS_API_URL, {
    method: "POST",
    credentials: "include", // ← 브라우저의 direct.dongwon.com 쿠키를 자동 첨부
    headers: {
      "Content-Type": "application/json; charset=UTF-8",
      "Accept": "application/json, text/javascript, */*; q=0.01",
      "X-Requested-With": "XMLHttpRequest",
    },
    body: JSON.stringify(payload),
  });

  const bodyText = await resp.text();

  if (looksLikeAuthError(bodyText)) {
    return {
      success: false,
      status: resp.status,
      message: "로그인이 만료되었거나 Direct 세션이 없습니다. direct.dongwon.com에 한 번 로그인 후 다시 시도해 주세요.",
    };
  }
  if (resp.status !== 200) {
    return { success: false, status: resp.status, message: `응답 상태코드 ${resp.status}` };
  }
  return { success: true, status: resp.status, message: bodyText };
}

chrome.runtime.onMessageExternal.addListener((message, sender, sendResponse) => {
  const type = message && message.type;

  if (type === "ping") {
    sendResponse({ ok: true, version: chrome.runtime.getManifest().version });
    return false; // 동기 응답, 채널 유지 불필요
  }

  if (type === "test_cookie") {
    // 실제 발송 없이(수신번호/내용 없이) 인증 상태만 확인
    callSendSms({ phone: "", message: "", callback: "", msgType: "1", scheduledAt: "" })
      .then((result) => sendResponse({ ok: true, test: result }))
      .catch((err) => sendResponse({ ok: false, error: String(err) }));
    return true; // 비동기 응답 대기
  }

  if (type === "send_sms") {
    const { phone, msg, callback, msgType, scheduledAt } = message;
    const messageText = message.message || msg; // 호출측 호환을 위해 msg/message 둘 다 허용
    callSendSms({
      phone,
      message: messageText,
      callback,
      msgType,
      scheduledAt,
    })
      .then((result) => sendResponse({ ok: true, result }))
      .catch((err) => sendResponse({ ok: false, error: String(err) }));
    return true; // 비동기 응답 대기
  }

  sendResponse({ ok: false, error: `알 수 없는 요청 유형: ${type}` });
  return false;
});

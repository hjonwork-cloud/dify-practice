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
 * ⚠️ fetch()로 팝업 HTML만 받아오는 방식은 실패함이 확인됨 — 페이지 안의 JS(세션
 *    초기화 또는 SSO 재인증)가 전혀 실행되지 않기 때문. 그룹웨어에서 수동으로 팝업을
 *    띄우면 성공하는 것과 동일하게 만들기 위해, 실제 창(탭)을 열어 페이지를 완전히
 *    로드한 후(JS 실행 포함) 닫는 방식으로 바꿈.
 * ⚠️ 화면 밖(off-screen) 좌표는 Chrome이 "화면에 50% 이상 보여야 함" 정책으로
 *    거부함(Invalid value for bounds). 그래서 정상 화면 내 좌표로 작게 생성한 뒤,
 *    생성 직후(state:"minimized"로 즉시 만들지 않고) chrome.windows.update로
 *    최소화하는 방식으로 변경 — 탐색은 이미 시작된 뒤 최소화되므로 로딩이 취소되지
 *    않음.
 *
 * ✅ netlog(chrome://net-export) 분석으로 실제 성공 흐름을 확인함:
 *    direct.dongwon.com/.../SMS_Service.aspx (세션 만료)
 *      → 302 Login.aspx?ReturnUrl=SMS_Service.aspx
 *      → 302 WebSite/SsoHelper.aspx
 *      → 302 www.dongwon.net/sso/oidc/authorize?...&redirect_uri=Callback.aspx
 *      → 302 oidc/login → 302 oidc/authorize&continue
 *      → 302 direct.dongwon.com/.../Callback.aspx?code=...  (여기서 Set-Cookie: 새 세션!)
 *      → 302 SMS_Service.aspx (최종, 성공)
 *    이건 전부 서버가 302로 자동 처리하는 흐름이라, SMS_Service.aspx 하나만 열어도
 *    "정상적으로는" 브라우저가 이 체인을 전부 따라가야 한다. 별도로 SsoHelper.aspx를
 *    먼저 열 필요는 없음(오히려 ReturnUrl 파라미터가 없어 더 불확실함).
 *    그런데도 예열이 "완료"라고 찍히면서 Send_SMS가 여전히 실패하는 사례가 있었으므로,
 *    예열이 끝난 시점에 탭이 실제로 어디에 도달했는지(SMS_Service.aspx까지 갔는지,
 *    아니면 Login.aspx/oidc 로그인 페이지에 멈춰 있는지) 콘솔에 진단 로그를 남긴다.
 */
/** direct.dongwon.com에 남아있는 쿠키를 전부 지운다.
 *
 * 왜 필요한가: 콘솔 로그로 확인한 결과, 예열용 창을 SMS_Service.aspx로 바로 열면
 * 서버가 Login.aspx → SsoHelper.aspx → OIDC 인증 → Callback.aspx 순으로 정상적으로
 * 리다이렉트를 태우지만, 최종적으로 SMS_Service.aspx가 아니라 포털 홈
 * (www.dongwon.net/portalapp/home)으로 돌아와 버리는 현상이 있었다.
 * OIDC 콜백에는 state 파라미터가 없어서 "인증 후 어디로 돌아갈지"는 서버 세션에
 * 저장된 값을 따르는 것으로 보이는데, 이 확장 프로그램으로 과거에 시도했던 여러
 * 예열 방식(Sso Helper 직접 방문 등)이 같은 세션 쿠키에 잘못된 "돌아갈 곳" 정보를
 * 남겨뒀을 가능성이 있다. 예열 직전에 direct.dongwon.com 쿠키를 전부 지워서 완전히
 * 깨끗한 상태에서 Login.aspx부터 다시 시작하게 만든다.
 * (Brity SSO 로그인 자체는 다른 도메인의 쿠키이므로 영향받지 않는다.)
 */
async function clearDirectCookies() {
  try {
    const cookies = await chrome.cookies.getAll({ domain: "direct.dongwon.com" });
    console.log("[SMS 브릿지] direct.dongwon.com 쿠키 삭제 대상:", cookies.map((c) => c.name));
    await Promise.all(
      cookies.map((c) => {
        const protocol = c.secure ? "https:" : "http:";
        const url = `${protocol}//${c.domain.replace(/^\./, "")}${c.path}`;
        return chrome.cookies.remove({ url, name: c.name }).catch(() => {});
      })
    );
  } catch (e) {
    console.log("[SMS 브릿지] 쿠키 삭제 중 예외:", e);
  }
}

async function warmUpSession(timeoutMs = 12000) {
  await clearDirectCookies();
  return new Promise((resolve) => {
    let settled = false;
    const finish = (reason) => {
      if (!settled) {
        settled = true;
        console.log("[SMS 브릿지] warmUpSession 종료:", reason);
        resolve();
      }
    };
    try {
      chrome.windows.create(
        { url: SMS_SERVER_URL, focused: false, type: "popup",
          width: 420, height: 320, left: 0, top: 0 },
        (win) => {
          if (chrome.runtime.lastError || !win) {
            console.log("[SMS 브릿지] warmUpSession 창 생성 실패:", chrome.runtime.lastError);
            finish("create_failed"); return;
          }
          const winId = win.id;
          const tabId = win.tabs && win.tabs[0] && win.tabs[0].id;
          console.log("[SMS 브릿지] warmUpSession 창 생성됨:", winId, tabId);
          // 탐색이 시작된 뒤 곧바로 최소화 (생성 시점에 바로 minimized로 만들면
          // 일부 Chrome/Edge 버전에서 로딩 자체가 취소되는 현상이 있어 순서를 분리함)
          try { chrome.windows.update(winId, { state: "minimized" }); } catch (e) {}

          // 진단: 예열 중 이 탭(메인 프레임)이 실제로 거쳐가는 모든 URL을 순서대로 기록.
          // 최종 URL만 봐서는 리다이렉트 체인이 정확히 어디서 갈라지는지 알 수 없어서,
          // chrome.webNavigation으로 매 단계(요청 시작/커밋 시점)를 실시간으로 남긴다.
          function onBeforeNav(details) {
            if (details.tabId === tabId && details.frameId === 0) {
              console.log("[SMS 브릿지][nav 시작]", details.url);
            }
          }
          function onCommitted(details) {
            if (details.tabId === tabId && details.frameId === 0) {
              console.log("[SMS 브릿지][nav 커밋]", details.url, "(transitionType=" + details.transitionType + ", qualifiers=" + JSON.stringify(details.transitionQualifiers) + ")");
            }
          }
          function onErrorOccurred(details) {
            if (details.tabId === tabId && details.frameId === 0) {
              console.log("[SMS 브릿지][nav 오류]", details.url, details.error);
            }
          }
          try {
            chrome.webNavigation.onBeforeNavigate.addListener(onBeforeNav);
            chrome.webNavigation.onCommitted.addListener(onCommitted);
            chrome.webNavigation.onErrorOccurred.addListener(onErrorOccurred);
          } catch (e) {
            console.log("[SMS 브릿지] webNavigation 리스너 등록 실패:", e);
          }

          // 진단(핵심): chrome.webNavigation.onCommitted는 여러 번의 302 리다이렉트가
          // 있어도 "최종 도착 URL" 한 번만 알려준다. 그 사이의 모든 302 hop(Login.aspx
          // → SsoHelper.aspx → oidc/authorize → oidc/login → Callback.aspx 등)을 실제로
          // 보려면 chrome.webRequest.onBeforeRedirect로 요청 단위 리다이렉트를 추적해야
          // 한다. main_frame 요청만, 이 tabId에 한해 기록한다.
          function onBeforeRedirect(details) {
            if (details.tabId === tabId && details.type === "main_frame") {
              console.log("[SMS 브릿지][redirect]", details.statusCode, details.url, "→", details.redirectUrl);
            }
          }
          function onCompleted(details) {
            if (details.tabId === tabId && details.type === "main_frame") {
              console.log("[SMS 브릿지][요청 완료]", details.statusCode, details.url);
            }
          }
          function onRequestErrorOccurred(details) {
            if (details.tabId === tabId && details.type === "main_frame") {
              console.log("[SMS 브릿지][요청 오류]", details.url, details.error);
            }
          }
          try {
            chrome.webRequest.onBeforeRedirect.addListener(
              onBeforeRedirect,
              { urls: ["https://direct.dongwon.com/*", "https://www.dongwon.net/*"] }
            );
            chrome.webRequest.onCompleted.addListener(
              onCompleted,
              { urls: ["https://direct.dongwon.com/*", "https://www.dongwon.net/*"] }
            );
            chrome.webRequest.onErrorOccurred.addListener(
              onRequestErrorOccurred,
              { urls: ["https://direct.dongwon.com/*", "https://www.dongwon.net/*"] }
            );
          } catch (e) {
            console.log("[SMS 브릿지] webRequest 리스너 등록 실패:", e);
          }

          const removeNavListeners = () => {
            try { chrome.webNavigation.onBeforeNavigate.removeListener(onBeforeNav); } catch (e) {}
            try { chrome.webNavigation.onCommitted.removeListener(onCommitted); } catch (e) {}
            try { chrome.webNavigation.onErrorOccurred.removeListener(onErrorOccurred); } catch (e) {}
            try { chrome.webRequest.onBeforeRedirect.removeListener(onBeforeRedirect); } catch (e) {}
            try { chrome.webRequest.onCompleted.removeListener(onCompleted); } catch (e) {}
            try { chrome.webRequest.onErrorOccurred.removeListener(onRequestErrorOccurred); } catch (e) {}
          };

          const cleanupAndFinish = (reason) => {
            removeNavListeners();
            // 진단: 예열 종료 시점에 탭이 실제로 어느 URL에 있는지 확인
            try {
              chrome.tabs.get(tabId, (tab) => {
                if (!chrome.runtime.lastError && tab) {
                  console.log("[SMS 브릿지] warmUpSession 최종 탭 URL:", tab.url);
                }
                try { chrome.windows.remove(winId); } catch (e) {}
                finish(reason);
              });
            } catch (e) {
              try { chrome.windows.remove(winId); } catch (e2) {}
              finish(reason);
            }
          };

          const timer = setTimeout(() => {
            try { chrome.tabs.onUpdated.removeListener(onUpdated); } catch (e) {}
            cleanupAndFinish("timeout");
          }, timeoutMs);
          function onUpdated(updatedTabId, info) {
            if (updatedTabId === tabId && info.status === "complete") {
              console.log("[SMS 브릿지] warmUpSession 페이지 로드 완료(중간 단계일 수 있음)");
              // 리다이렉트 체인 중간에 "complete"가 여러 번 뜰 수 있으므로,
              // 마지막 안정화까지 약간의 유예 시간을 둔 뒤 최종 URL을 확인한다.
              setTimeout(() => {
                clearTimeout(timer);
                try { chrome.tabs.onUpdated.removeListener(onUpdated); } catch (e) {}
                cleanupAndFinish("loaded");
              }, 1500);
            }
          }
          chrome.tabs.onUpdated.addListener(onUpdated);
        }
      );
    } catch (e) {
      console.log("[SMS 브릿지] warmUpSession 예외:", e);
      finish("exception");
    }
  });
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
  console.log("[SMS 브릿지] Send_SMS 응답 status=%d, body(앞 500자)=%s", resp.status, bodyText.slice(0, 500));

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

/**
 * SAP 브릿지 에이전트 연동 모듈
 * 사용자 브라우저 → localhost:7788 → SAP GUI 자동 제어
 */

const SAP_AGENT_URL = "https://localhost:7788";
const SAP_LOGIN_URL = "http://sap.erp.dongwon.com:6060/saplogin//SapMLogon.application?ip=10.200.120.241";
const AGENT_TIMEOUT_MS = 3000;

/**
 * 에이전트 + SAP 상태 확인
 * @returns {Promise<{ok: boolean, status: string, message: string}>}
 */
async function checkSapStatus() {
  try {
    const resp = await Promise.race([
      fetch(`${SAP_AGENT_URL}/sap/status`),
      new Promise((_, reject) => setTimeout(() => reject(new Error("timeout")), AGENT_TIMEOUT_MS))
    ]);
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const data = await resp.json();
    return { ok: data.status === "ok", status: data.status, message: data.message || "", title: data.title || "" };
  } catch (e) {
    if (e.message === "timeout" || e.message.includes("fetch")) {
      return { ok: false, status: "agent_not_running", message: "SAP 브릿지 에이전트가 실행되지 않았습니다." };
    }
    return { ok: false, status: "error", message: e.message };
  }
}

/**
 * SAP 고객별 판가 적용
 * @param {object} payload - { customer_code, items: [{material, price, currency, valid_from, valid_to}] }
 * @returns {Promise<{success: boolean, applied_count: number, error: string}>}
 */
async function sapApplyPrice(payload) {
  try {
    const resp = await fetch(`${SAP_AGENT_URL}/sap/apply-price`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    return await resp.json();
  } catch (e) {
    return { success: false, error: e.message };
  }
}

/**
 * SAP 상태 확인 후 팝업 처리 → 성공 시 콜백 실행
 * @param {Function} onReady - SAP 준비 완료 시 실행할 함수
 */
async function withSapGuard(onReady) {
  showSapStatusModal("checking");

  const status = await checkSapStatus();

  if (status.ok) {
    hideSapStatusModal();
    await onReady();
    return;
  }

  // 상태별 안내 메시지
  if (status.status === "agent_not_running") {
    showSapStatusModal("agent_not_running");
  } else if (status.status === "not_running") {
    showSapStatusModal("sap_not_running");
  } else if (status.status === "not_logged_in") {
    showSapStatusModal("sap_not_logged_in");
  } else {
    showSapStatusModal("error", status.message);
  }
}

// ── 모달 UI ──────────────────────────────────────────────

function showSapStatusModal(state, errorMsg = "") {
  let existing = document.getElementById("sap-bridge-modal");
  if (!existing) {
    existing = document.createElement("div");
    existing.id = "sap-bridge-modal";
    existing.innerHTML = `
      <div class="sap-modal-backdrop" onclick="hideSapStatusModal()"></div>
      <div class="sap-modal-box">
        <div id="sap-modal-content"></div>
        <div class="sap-modal-actions">
          <button class="btn portal-primary" id="sap-modal-btn-primary"></button>
          <button class="btn" onclick="hideSapStatusModal()">닫기</button>
        </div>
      </div>`;
    document.body.appendChild(existing);
  }

  const content = document.getElementById("sap-modal-content");
  const btnPrimary = document.getElementById("sap-modal-btn-primary");

  const configs = {
    checking: {
      icon: "⏳", title: "SAP 상태 확인 중...",
      body: "잠시만 기다려 주세요.",
      btn: null,
    },
    agent_not_running: {
      icon: "🔌", title: "SAP 브릿지 에이전트 필요",
      body: `판가 자동 적용을 사용하려면 <b>SAP 브릿지 에이전트</b>가 실행 중이어야 합니다.<br><br>
             <small>• PC 트레이에서 에이전트 실행 확인<br>
             • 또는 <code>sap_bridge_agent.py</code> 실행</small>`,
      btn: null,
    },
    sap_not_running: {
      icon: "💻", title: "SAP GUI를 먼저 실행해주세요",
      body: "SAP GUI가 열려있지 않습니다. SAP에 로그인한 후 다시 시도해주세요.",
      btn: { label: "🔐 SAP 로그인", action: () => window.open(SAP_LOGIN_URL, "_blank") },
    },
    sap_not_logged_in: {
      icon: "🔐", title: "SAP 로그인이 필요합니다",
      body: "SAP GUI가 실행 중이지만 로그인되지 않았습니다. SAP에 로그인한 후 다시 시도해주세요.",
      btn: { label: "🔐 SAP 로그인", action: () => window.open(SAP_LOGIN_URL, "_blank") },
    },
    error: {
      icon: "⚠️", title: "SAP 연결 오류",
      body: `오류가 발생했습니다: <code>${errorMsg}</code>`,
      btn: null,
    },
  };

  const cfg = configs[state] || configs.error;
  content.innerHTML = `
    <div class="sap-modal-icon">${cfg.icon}</div>
    <h3 class="sap-modal-title">${cfg.title}</h3>
    <p class="sap-modal-body">${cfg.body}</p>`;

  if (cfg.btn) {
    btnPrimary.style.display = "";
    btnPrimary.textContent = cfg.btn.label;
    btnPrimary.onclick = cfg.btn.action;
  } else {
    btnPrimary.style.display = "none";
  }

  existing.style.display = "flex";
}

function hideSapStatusModal() {
  const m = document.getElementById("sap-bridge-modal");
  if (m) m.style.display = "none";
}

// ── 인라인 CSS ───────────────────────────────────────────

(function injectSapModalCss() {
  if (document.getElementById("sap-bridge-style")) return;
  const style = document.createElement("style");
  style.id = "sap-bridge-style";
  style.textContent = `
    #sap-bridge-modal {
      display: none; position: fixed; inset: 0; z-index: 9999;
      align-items: center; justify-content: center;
    }
    .sap-modal-backdrop {
      position: absolute; inset: 0; background: rgba(0,0,0,.45);
    }
    .sap-modal-box {
      position: relative; background: #fff; border-radius: 12px;
      padding: 2rem; max-width: 420px; width: 90%; box-shadow: 0 8px 32px rgba(0,0,0,.2);
    }
    .sap-modal-icon { font-size: 2.5rem; margin-bottom: .5rem; }
    .sap-modal-title { font-size: 1.1rem; font-weight: 700; margin: 0 0 .75rem; }
    .sap-modal-body { font-size: .9rem; color: #555; line-height: 1.6; margin: 0 0 1.5rem; }
    .sap-modal-actions { display: flex; gap: .5rem; justify-content: flex-end; }
  `;
  document.head.appendChild(style);
})();

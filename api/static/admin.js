document.addEventListener('DOMContentLoaded', () => {
  document.querySelectorAll('form[action*="/publish"]').forEach((form) => {
    form.addEventListener('submit', (event) => {
      if (!window.confirm('이 답변을 FAQ로 공개하시겠습니까? 등록한 표현과 정확히 일치하는 질문에 자동 응답됩니다.')) {
        event.preventDefault();
      }
    });
  });

  document.querySelectorAll('form.js-confirm').forEach((form) => {
    form.addEventListener('submit', (event) => {
      const message = form.dataset.confirm || '진행하시겠습니까?';
      if (!window.confirm(message)) {
        event.preventDefault();
      }
    });
  });

  document.querySelectorAll('.table-search').forEach((input) => {
    input.addEventListener('input', () => {
      const query = input.value.trim().toLowerCase();
      const table = document.getElementById(input.dataset.target);
      if (!table) return;
      table.querySelectorAll('tbody tr').forEach((row) => {
        const key = (row.dataset.search || row.textContent).toLowerCase();
        row.style.display = key.includes(query) ? '' : 'none';
      });
    });
  });

  // 폼 토글: 버튼 클릭 시 입력칸 표시/숨김
  document.querySelectorAll('.js-toggle-form').forEach((btn) => {
    btn.addEventListener('click', () => {
      const target = document.getElementById(btn.dataset.target);
      if (!target) return;
      const show = target.hidden;
      target.hidden = !show;
      if (show) {
        const first = target.querySelector('input, select');
        if (first) first.focus();
      }
    });
  });

  // 소속 드롭다운: 변경 시 섹션 상단 "변경내역 저장" 버튼으로 일괄 저장 (페이지 리로드 없음 → 스크롤 유지)
  const csrfMatch = document.cookie.match(/(?:^|;\s*)dongwon_admin_csrf=([^;]+)/);
  const csrfToken = csrfMatch ? decodeURIComponent(csrfMatch[1]) : '';
  const batchSaveBtn = document.getElementById('teamBatchSave');
  const batchInfo = document.querySelector('.team-batch-info');
  const teamSelects = document.querySelectorAll('.team-select');

  if (batchSaveBtn && teamSelects.length) {
    const dirtySelects = () => Array.from(teamSelects).filter((s) => s.value !== s.dataset.orig);
    const refreshBatchUI = () => {
      const count = dirtySelects().length;
      batchSaveBtn.hidden = count === 0;
      if (batchInfo) {
        batchInfo.hidden = count === 0;
        batchInfo.textContent = count ? `변경 ${count}건` : '';
      }
    };
    teamSelects.forEach((select) => {
      select.addEventListener('change', () => {
        select.classList.toggle('team-dirty', select.value !== select.dataset.orig);
        refreshBatchUI();
      });
    });
    batchSaveBtn.addEventListener('click', async () => {
      const changed = dirtySelects();
      if (!changed.length) return;
      batchSaveBtn.disabled = true;
      const original = batchSaveBtn.textContent;
      batchSaveBtn.textContent = '저장 중…';
      let ok = 0;
      const failed = [];
      for (const select of changed) {
        const body = new URLSearchParams({ emp_code: select.dataset.emp, team: select.value, csrf_token: csrfToken });
        try {
          const res = await fetch('/admin/users/set-team', {
            method: 'POST',
            headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
            body,
          });
          const data = await res.json().catch(() => ({}));
          if (res.ok && data.ok) {
            select.dataset.orig = select.value;
            select.classList.remove('team-dirty');
            ok += 1;
          } else {
            failed.push(data.error || select.dataset.emp);
          }
        } catch (err) {
          failed.push(select.dataset.emp);
        }
      }
      batchSaveBtn.disabled = false;
      batchSaveBtn.textContent = original;
      refreshBatchUI();
      if (failed.length) {
        window.alert(`${ok}건 저장, ${failed.length}건 실패\n${failed.join('\n')}`);
      } else if (batchInfo) {
        batchInfo.hidden = false;
        batchInfo.textContent = `✓ ${ok}건 저장됨`;
        setTimeout(() => { if (dirtySelects().length === 0) batchInfo.hidden = true; }, 2500);
      }
    });
  }

  // POST 폼 제출 후 리다이렉트 시 스크롤 위치 유지
  if ('scrollRestoration' in history) history.scrollRestoration = 'manual';
  const scrollKey = 'admin:scroll:' + location.pathname;
  const savedScroll = sessionStorage.getItem(scrollKey);
  if (savedScroll !== null) {
    const y = parseInt(savedScroll, 10) || 0;
    const restore = () => window.scrollTo(0, y);
    restore();
    requestAnimationFrame(restore);
    window.addEventListener('load', () => {
      restore();
      setTimeout(() => sessionStorage.removeItem(scrollKey), 250);
    });
  }
  document.querySelectorAll('form[method="post"]').forEach((form) => {
    form.addEventListener('submit', () => {
      sessionStorage.setItem(scrollKey, String(window.scrollY));
    });
  });
});

// ── 브랜드 선택 팝업 (검색 가능한 리스트 + 선택 확인) ───────────────────────
// items: [{value, label}], opts: {current, title, onConfirm}
window.openBrandPicker = function (items, opts) {
  opts = opts || {};
  const title = opts.title || '브랜드 선택';
  const onConfirm = opts.onConfirm || function () {};
  let picked = opts.current;

  const prevOverlay = document.getElementById('bpOverlay');
  if (prevOverlay) prevOverlay.remove();

  const overlay = document.createElement('div');
  overlay.id = 'bpOverlay';
  overlay.className = 'bp-overlay';
  overlay.innerHTML =
    '<div class="bp-box">' +
      '<div class="bp-header"><strong></strong><button type="button" class="bp-close-btn" aria-label="닫기">&times;</button></div>' +
      '<input type="text" class="bp-search" placeholder="브랜드명 검색…" autocomplete="off">' +
      '<div class="bp-list"></div>' +
      '<div class="bp-footer"><button type="button" class="bp-cancel-btn">취소</button><button type="button" class="bp-confirm-btn">확인</button></div>' +
    '</div>';
  overlay.querySelector('.bp-header strong').textContent = title;
  document.body.appendChild(overlay);

  const listEl = overlay.querySelector('.bp-list');
  const searchEl = overlay.querySelector('.bp-search');
  const confirmBtn = overlay.querySelector('.bp-confirm-btn');

  function renderList(filter) {
    const kw = (filter || '').trim().toLowerCase();
    listEl.innerHTML = '';
    const filtered = items.filter((it) => !kw || String(it.label).toLowerCase().includes(kw));
    if (!filtered.length) {
      listEl.innerHTML = '<div class="bp-empty">검색 결과가 없습니다.</div>';
      return;
    }
    filtered.forEach((it) => {
      const row = document.createElement('div');
      row.className = 'bp-item' + (String(it.value) === String(picked) ? ' selected' : '');
      row.textContent = it.label;
      row.addEventListener('click', () => {
        picked = it.value;
        listEl.querySelectorAll('.bp-item').forEach((el) => el.classList.remove('selected'));
        row.classList.add('selected');
      });
      listEl.appendChild(row);
    });
  }

  renderList('');
  searchEl.addEventListener('input', () => renderList(searchEl.value));

  function close() { overlay.remove(); }
  overlay.addEventListener('click', (e) => { if (e.target === overlay) close(); });
  overlay.querySelector('.bp-close-btn').addEventListener('click', close);
  overlay.querySelector('.bp-cancel-btn').addEventListener('click', close);
  confirmBtn.addEventListener('click', () => {
    if (picked === undefined || picked === null || picked === '') { close(); return; }
    close();
    onConfirm(picked);
  });

  setTimeout(() => searchEl.focus(), 30);
};

// 기존 <select id="selectId">(display:none 처리)의 옵션을 그대로 브랜드 선택 팝업 데이터로
// 사용하는 범용 초기화 헬퍼. 버튼 클릭 → 팝업에서 선택 → select.value 갱신 + change 이벤트 발생.
window.initBrandPickerFor = function (selectId, btnId, labelId, opts) {
  const select = document.getElementById(selectId);
  const btn = document.getElementById(btnId);
  const labelEl = labelId ? document.getElementById(labelId) : null;
  if (!select || !btn) return;

  function currentLabel() {
    const opt = select.options[select.selectedIndex];
    return opt ? opt.textContent : ((opts && opts.title) || '브랜드 선택');
  }
  if (labelEl) labelEl.textContent = currentLabel();

  btn.addEventListener('click', function () {
    const items = Array.from(select.options).map((o) => ({ value: o.value, label: o.textContent }));
    if (!items.length) return;
    window.openBrandPicker(items, {
      title: (opts && opts.title) || '브랜드 선택',
      current: select.value,
      onConfirm: function (value) {
        select.value = value;
        if (labelEl) labelEl.textContent = currentLabel();
        select.dispatchEvent(new Event('change', { bubbles: true }));
      },
    });
  });
};


# -*- coding: utf-8 -*-
"""
DWHF 챗봇 서버 감시 & 제어 GUI
참고 UI: 신용카드 이상거래 탐지 데모 레이아웃
"""
import tkinter as tk
from tkinter import ttk, scrolledtext
import subprocess, threading, time, json, os, sys
import urllib.request

_NO_WIN = subprocess.CREATE_NO_WINDOW  # 터미널 창 팝업 방지

# ── 설정 ──────────────────────────────────────────────────────
PYTHON     = r"e:\git-copilot\.conda\python.exe"
API_DIR    = r"e:\git-copilot\dify-practice\api"
PORT       = 8000
NGROK_DOMAIN = "perfunctorily-stumpless-leticia.ngrok-free.dev"
CHECK_SEC  = 30
LOG_FILE   = r"e:\git-copilot\dify-practice\watchdog.log"

# Databricks PAT(Personal Access Token) — 서버 시작 시 DATABRICKS_TOKEN 환경변수로 자동 주입.
# 장기 토큰이라 만료/재인증이 없어 커넥션 풀과 궁합이 좋다. (교체 시 이 값만 갱신)
DATABRICKS_TOKEN = "dapiafd0c55621ca4f4473a1ee44ebcc9076"


def _server_env():
    """서버 프로세스에 전달할 환경변수 (기존 환경 + DATABRICKS_TOKEN 주입)."""
    env = os.environ.copy()
    if DATABRICKS_TOKEN:
        env["DATABRICKS_TOKEN"] = DATABRICKS_TOKEN
    return env

# ── 상태 색상 ──────────────────────────────────────────────────
C_BG       = "#1e1e1e"
C_PANEL    = "#2d2d2d"
C_GREEN    = "#00c853"
C_RED      = "#f44336"
C_ORANGE   = "#ff9800"
C_YELLOW   = "#ffc107"
C_TEXT     = "#e0e0e0"
C_DIM      = "#888888"
C_ACCENT   = "#1976d2"
C_LOG_BG   = "#0d0d0d"
C_LOG_FG   = "#00e676"
C_BTN_BLUE = "#1565c0"
C_BTN_GREEN= "#2e7d32"
C_BTN_RED  = "#c62828"


def is_port_listening(port):
    try:
        r = subprocess.run(
            ["netstat", "-ano"],
            capture_output=True, text=True, timeout=5,
            creationflags=_NO_WIN
        )
        for line in r.stdout.splitlines():
            if f":{port}" in line and "LISTENING" in line:
                return True
        return False
    except:
        return False


def is_ngrok_alive():
    try:
        # 포트 4040 리스닝 여부
        r = subprocess.run(["netstat", "-ano"], capture_output=True, text=True, timeout=5, creationflags=_NO_WIN)
        port4040 = any(":4040" in l and "LISTENING" in l for l in r.stdout.splitlines())
        return port4040
    except:
        return False


def get_ngrok_url():
    try:
        req = urllib.request.urlopen("http://localhost:4040/api/tunnels", timeout=3)
        data = json.loads(req.read())
        tunnels = data.get("tunnels", [])
        for t in tunnels:
            if NGROK_DOMAIN in t.get("public_url", ""):
                return t["public_url"]
        if tunnels:
            return tunnels[0].get("public_url", "—")
    except:
        pass
    return "—"


def get_server_pid():
    try:
        r = subprocess.run(["netstat", "-ano"], capture_output=True, text=True, timeout=5, creationflags=_NO_WIN)
        for line in r.stdout.splitlines():
            parts = line.split()
            if len(parts) >= 4 and f":{PORT}" in parts[1] and "LISTENING" in line:
                return parts[-1]
    except:
        pass
    return "—"


class WatchdogApp:
    def __init__(self, root):
        self.root = root
        self.root.title("DWHF 챗봇 서버 모니터")
        self.root.geometry("820x620")
        self.root.configure(bg=C_BG)
        self.root.resizable(False, False)

        self.auto_watch = tk.BooleanVar(value=False)
        self._watch_thread = None
        self._stop_watch = threading.Event()
        self._server_proc = None

        self._build_ui()
        self._refresh_status()

    # ──────────────────────────────────────────────────────────
    # UI 구성
    # ──────────────────────────────────────────────────────────
    def _build_ui(self):
        # ── 타이틀 바 ──
        title_fr = tk.Frame(self.root, bg="#0d47a1", height=40)
        title_fr.pack(fill="x")
        title_fr.pack_propagate(False)
        tk.Label(title_fr, text="  🤖  DWHF 챗봇 서버 모니터",
                 font=("Malgun Gothic", 13, "bold"),
                 bg="#0d47a1", fg="white").pack(side="left", pady=6)

        # ── 메인 영역 ──
        main_fr = tk.Frame(self.root, bg=C_BG)
        main_fr.pack(fill="both", expand=True, padx=8, pady=6)

        # ── 왼쪽 패널 (컨트롤) ──
        left = tk.Frame(main_fr, bg=C_PANEL, width=180, bd=1, relief="flat")
        left.pack(side="left", fill="y", padx=(0, 6))
        left.pack_propagate(False)
        self._build_left(left)

        # ── 오른쪽 ──
        right = tk.Frame(main_fr, bg=C_BG)
        right.pack(side="left", fill="both", expand=True)
        self._build_right(right)

        # ── 상태 바 ──
        self.status_bar = tk.Label(
            self.root, text="  ⬤  준비 중...",
            font=("Malgun Gothic", 12, "bold"),
            bg="#424242", fg="white", anchor="w", height=2
        )
        self.status_bar.pack(fill="x")

        # ── 로그 ──
        log_fr = tk.LabelFrame(self.root, text=" 🖥  활동 로그",
                               font=("Malgun Gothic", 9),
                               bg=C_BG, fg=C_DIM, bd=1)
        log_fr.pack(fill="x", padx=8, pady=(0, 6))
        self.log_box = scrolledtext.ScrolledText(
            log_fr, height=7, bg=C_LOG_BG, fg=C_LOG_FG,
            font=("Consolas", 9), state="disabled",
            insertbackground=C_LOG_FG, bd=0
        )
        self.log_box.pack(fill="x", padx=4, pady=4)
        self.log_box.tag_config("ok",    foreground=C_GREEN)
        self.log_box.tag_config("warn",  foreground=C_YELLOW)
        self.log_box.tag_config("error", foreground=C_RED)
        self.log_box.tag_config("info",  foreground=C_LOG_FG)

    def _build_left(self, parent):
        tk.Label(parent, text="서버 제어",
                 font=("Malgun Gothic", 10, "bold"),
                 bg=C_PANEL, fg=C_TEXT).pack(pady=(12, 4))
        tk.Frame(parent, bg="#555", height=1).pack(fill="x", padx=10)

        btns = [
            ("▶  서버 시작",   C_BTN_GREEN, self.start_server),
            ("■  서버 중지",   C_BTN_RED,   self.stop_server),
            ("↺  서버 재시작", C_BTN_BLUE,  self.restart_server),
        ]
        for txt, col, cmd in btns:
            tk.Button(parent, text=txt, bg=col, fg="white",
                      font=("Malgun Gothic", 10, "bold"),
                      relief="flat", cursor="hand2",
                      activebackground=col, activeforeground="white",
                      command=cmd, width=16, pady=6
                      ).pack(pady=4, padx=10)

        tk.Frame(parent, bg="#555", height=1).pack(fill="x", padx=10, pady=6)
        tk.Label(parent, text="ngrok 제어",
                 font=("Malgun Gothic", 10, "bold"),
                 bg=C_PANEL, fg=C_TEXT).pack(pady=(0, 4))

        btns2 = [
            ("▶  ngrok 시작",  C_BTN_GREEN, self.start_ngrok),
            ("■  ngrok 중지",  C_BTN_RED,   self.stop_ngrok),
        ]
        for txt, col, cmd in btns2:
            tk.Button(parent, text=txt, bg=col, fg="white",
                      font=("Malgun Gothic", 10, "bold"),
                      relief="flat", cursor="hand2",
                      activebackground=col, activeforeground="white",
                      command=cmd, width=16, pady=6
                      ).pack(pady=4, padx=10)

        tk.Frame(parent, bg="#555", height=1).pack(fill="x", padx=10, pady=6)

        # 자동 감시 스위치
        tk.Label(parent, text="자동 감시",
                 font=("Malgun Gothic", 10, "bold"),
                 bg=C_PANEL, fg=C_TEXT).pack(pady=(0, 4))
        self.watch_btn = tk.Button(
            parent, text="자동 감시: OFF",
            bg="#37474f", fg=C_DIM,
            font=("Malgun Gothic", 9, "bold"),
            relief="flat", cursor="hand2",
            activebackground="#37474f",
            command=self.toggle_watchdog, width=16, pady=6
        )
        self.watch_btn.pack(pady=4, padx=10)

        tk.Frame(parent, bg="#555", height=1).pack(fill="x", padx=10, pady=6)
        tk.Button(parent, text="🔄  새로고침",
                  bg="#37474f", fg=C_TEXT,
                  font=("Malgun Gothic", 9),
                  relief="flat", cursor="hand2",
                  command=self._refresh_status, width=16, pady=5
                  ).pack(padx=10, pady=2)

    def _build_right(self, parent):
        # ── 상태 카드 ──
        card_fr = tk.Frame(parent, bg=C_PANEL, bd=1, relief="flat")
        card_fr.pack(fill="x", pady=(0, 6))

        tk.Label(card_fr, text="서버 상태",
                 font=("Malgun Gothic", 10, "bold"),
                 bg=C_PANEL, fg=C_DIM).grid(row=0, column=0, sticky="w", padx=14, pady=(10, 2))

        fields = [
            ("uvicorn 포트", "lbl_port"),
            ("서버 PID",     "lbl_pid"),
            ("ngrok 터널",   "lbl_ngrok"),
            ("공개 URL",     "lbl_url"),
            ("마지막 확인",  "lbl_checked"),
        ]
        for i, (name, attr) in enumerate(fields):
            row = i + 1
            tk.Label(card_fr, text=f"  {name}",
                     font=("Malgun Gothic", 10),
                     bg=C_PANEL, fg=C_DIM, width=14, anchor="w"
                     ).grid(row=row, column=0, sticky="w", padx=10, pady=3)
            lbl = tk.Label(card_fr, text="—",
                           font=("Malgun Gothic", 10, "bold"),
                           bg=C_PANEL, fg=C_TEXT, anchor="w")
            lbl.grid(row=row, column=1, sticky="w", padx=8, pady=3)
            setattr(self, attr, lbl)

        # 구분선
        tk.Frame(card_fr, bg="#444", height=1).grid(
            row=len(fields)+1, column=0, columnspan=2, sticky="ew", padx=10, pady=6)

        # 상태 인디케이터
        ind_fr = tk.Frame(card_fr, bg=C_PANEL)
        ind_fr.grid(row=len(fields)+2, column=0, columnspan=2, sticky="ew", padx=10, pady=(0, 10))

        self.ind_server = self._make_indicator(ind_fr, "uvicorn")
        self.ind_server.pack(side="left", padx=(0, 20))
        self.ind_ngrok = self._make_indicator(ind_fr, "ngrok")
        self.ind_ngrok.pack(side="left")

        # ── 감시 주기 바 ──
        bar_fr = tk.Frame(parent, bg=C_PANEL)
        bar_fr.pack(fill="x", pady=(0, 6))
        tk.Label(bar_fr, text="  다음 체크까지",
                 font=("Malgun Gothic", 9), bg=C_PANEL, fg=C_DIM).pack(side="left", pady=6, padx=4)
        self.progress = ttk.Progressbar(bar_fr, length=320, mode="determinate", maximum=CHECK_SEC)
        self.progress.pack(side="left", pady=6, padx=4)
        self.lbl_countdown = tk.Label(bar_fr, text=f"{CHECK_SEC}s",
                                      font=("Consolas", 9), bg=C_PANEL, fg=C_DIM)
        self.lbl_countdown.pack(side="left", padx=4)

    def _make_indicator(self, parent, label):
        fr = tk.Frame(parent, bg=C_PANEL)
        dot = tk.Label(fr, text="⬤", font=("Arial", 16), bg=C_PANEL, fg="#555")
        dot.pack(side="left")
        lbl = tk.Label(fr, text=label, font=("Malgun Gothic", 10, "bold"),
                       bg=C_PANEL, fg=C_DIM)
        lbl.pack(side="left", padx=4)
        fr.dot = dot
        fr.lbl = lbl
        return fr

    # ──────────────────────────────────────────────────────────
    # 상태 갱신
    # ──────────────────────────────────────────────────────────
    def _refresh_status(self):
        server_ok = is_port_listening(PORT)
        ngrok_ok  = is_ngrok_alive()
        pid       = get_server_pid() if server_ok else "—"
        url       = get_ngrok_url() if ngrok_ok else "—"
        now       = time.strftime("%H:%M:%S")

        self.lbl_port.config(
            text=f":{PORT}  {'● 리스닝' if server_ok else '✕ 오프라인'}",
            fg=C_GREEN if server_ok else C_RED
        )
        self.lbl_pid.config(text=pid, fg=C_TEXT)
        self.lbl_ngrok.config(
            text=f"{'● 연결됨' if ngrok_ok else '✕ 오프라인'}",
            fg=C_GREEN if ngrok_ok else C_RED
        )
        self.lbl_url.config(
            text=url if url != "—" else "—",
            fg=C_ACCENT if url != "—" else C_DIM
        )
        self.lbl_checked.config(text=now, fg=C_DIM)

        # 인디케이터
        s_col = C_GREEN if server_ok else C_RED
        n_col = C_GREEN if ngrok_ok  else C_RED
        self.ind_server.dot.config(fg=s_col)
        self.ind_server.lbl.config(fg=s_col)
        self.ind_ngrok.dot.config(fg=n_col)
        self.ind_ngrok.lbl.config(fg=n_col)

        # 상태 바
        if server_ok and ngrok_ok:
            self.status_bar.config(
                text="  ✔  모든 서비스 정상 운영 중",
                bg=C_BTN_GREEN
            )
        elif server_ok:
            self.status_bar.config(
                text="  ⚠  ngrok 터널 오프라인",
                bg=C_ORANGE
            )
        elif ngrok_ok:
            self.status_bar.config(
                text="  ⚠  uvicorn 서버 오프라인",
                bg=C_ORANGE
            )
        else:
            self.status_bar.config(
                text="  ✕  모든 서비스 오프라인",
                bg=C_BTN_RED
            )

    def _log(self, msg, level="info"):
        ts = time.strftime("%H:%M:%S")
        line = f"[{ts}] {msg}\n"
        self.log_box.config(state="normal")
        self.log_box.insert("end", line, level)
        self.log_box.see("end")
        self.log_box.config(state="disabled")
        # 파일에도 기록
        try:
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")
        except:
            pass

    # ──────────────────────────────────────────────────────────
    # 서버 제어
    # ──────────────────────────────────────────────────────────
    def start_server(self):
        if is_port_listening(PORT):
            self._log(f"uvicorn 이미 실행 중 (:{PORT})", "warn")
            return
        self._log("uvicorn 시작 중...", "info")
        def _run():
            subprocess.Popen(
                [PYTHON, "-m", "uvicorn", "main:app",
                 "--host", "0.0.0.0", "--port", str(PORT)],
                cwd=API_DIR,
                env=_server_env(),
                creationflags=subprocess.CREATE_NO_WINDOW
            )
            time.sleep(4)
            ok = is_port_listening(PORT)
            self._log(f"uvicorn {'시작 성공' if ok else '시작 실패'}", "ok" if ok else "error")
            self.root.after(0, self._refresh_status)
        threading.Thread(target=_run, daemon=True).start()

    def stop_server(self):
        self._log("uvicorn 종료 중...", "warn")
        def _run():
            r = subprocess.run(["netstat", "-ano"], capture_output=True, text=True, creationflags=_NO_WIN)
            for line in r.stdout.splitlines():
                parts = line.split()
                if len(parts) >= 4 and f":{PORT}" in parts[1] and "LISTENING" in line:
                    pid = parts[-1]
                    subprocess.run(["taskkill", "/F", "/PID", pid],
                                   capture_output=True, creationflags=_NO_WIN)
                    self._log(f"PID {pid} 종료 완료", "warn")
            time.sleep(1)
            self.root.after(0, self._refresh_status)
        threading.Thread(target=_run, daemon=True).start()

    def restart_server(self):
        self._log("uvicorn 재시작...", "warn")
        def _run():
            # 종료
            r = subprocess.run(["netstat", "-ano"], capture_output=True, text=True, creationflags=_NO_WIN)
            for line in r.stdout.splitlines():
                parts = line.split()
                if len(parts) >= 4 and f":{PORT}" in parts[1] and "LISTENING" in line:
                    subprocess.run(["taskkill", "/F", "/PID", parts[-1]], capture_output=True, creationflags=_NO_WIN)
            time.sleep(2)
            # 시작
            subprocess.Popen(
                [PYTHON, "-m", "uvicorn", "main:app",
                 "--host", "0.0.0.0", "--port", str(PORT)],
                cwd=API_DIR,
                env=_server_env(),
                creationflags=subprocess.CREATE_NO_WINDOW
            )
            time.sleep(4)
            ok = is_port_listening(PORT)
            self._log(f"uvicorn 재시작 {'성공' if ok else '실패'}", "ok" if ok else "error")
            self.root.after(0, self._refresh_status)
        threading.Thread(target=_run, daemon=True).start()

    def start_ngrok(self):
        if is_ngrok_alive():
            self._log("ngrok 이미 실행 중", "warn")
            return
        self._log("ngrok 시작 중... (클라우드 세션 해제 60초 대기)", "info")
        def _run():
            # 기존 4040 프로세스 종료
            r = subprocess.run(["netstat", "-ano"], capture_output=True, text=True, creationflags=_NO_WIN)
            for line in r.stdout.splitlines():
                parts = line.split()
                if len(parts) >= 4 and ":4040" in parts[1] and "LISTENING" in line:
                    subprocess.run(["taskkill", "/F", "/PID", parts[-1]], capture_output=True, creationflags=_NO_WIN)
            self._log("클라우드 세션 해제 대기 중 (60초)...", "warn")
            time.sleep(60)
            subprocess.Popen(
                ["ngrok", "http", str(PORT), f"--domain={NGROK_DOMAIN}"],
                creationflags=subprocess.CREATE_NO_WINDOW
            )
            time.sleep(5)
            ok = is_ngrok_alive()
            self._log(f"ngrok {'시작 성공' if ok else '시작 실패'}", "ok" if ok else "error")
            self.root.after(0, self._refresh_status)
        threading.Thread(target=_run, daemon=True).start()

    def stop_ngrok(self):
        self._log("ngrok 종료 중...", "warn")
        def _run():
            r = subprocess.run(["netstat", "-ano"], capture_output=True, text=True, creationflags=_NO_WIN)
            for line in r.stdout.splitlines():
                parts = line.split()
                if len(parts) >= 4 and ":4040" in parts[1] and "LISTENING" in line:
                    subprocess.run(["taskkill", "/F", "/PID", parts[-1]], capture_output=True, creationflags=_NO_WIN)
                    self._log(f"ngrok PID {parts[-1]} 종료", "warn")
            time.sleep(1)
            self.root.after(0, self._refresh_status)
        threading.Thread(target=_run, daemon=True).start()

    # ──────────────────────────────────────────────────────────
    # 자동 감시
    # ──────────────────────────────────────────────────────────
    def toggle_watchdog(self):
        if self.auto_watch.get():
            # 끄기
            self.auto_watch.set(False)
            self._stop_watch.set()
            self.watch_btn.config(text="자동 감시: OFF", bg="#37474f", fg=C_DIM)
            self.progress["value"] = 0
            self.lbl_countdown.config(text=f"{CHECK_SEC}s")
            self._log("자동 감시 중지", "warn")
        else:
            # 켜기
            self.auto_watch.set(True)
            self._stop_watch.clear()
            self.watch_btn.config(text="자동 감시: ON ", bg=C_BTN_GREEN, fg="white")
            self._log(f"자동 감시 시작 (주기: {CHECK_SEC}초)", "ok")
            self._watch_thread = threading.Thread(target=self._watch_loop, daemon=True)
            self._watch_thread.start()

    def _watch_loop(self):
        while not self._stop_watch.is_set():
            # 카운트다운
            for i in range(CHECK_SEC, 0, -1):
                if self._stop_watch.is_set():
                    return
                self.root.after(0, lambda v=CHECK_SEC-i: self.progress.configure(value=v))
                self.root.after(0, lambda s=i: self.lbl_countdown.config(text=f"{s}s"))
                time.sleep(1)

            if self._stop_watch.is_set():
                return

            # 상태 체크
            server_ok = is_port_listening(PORT)
            ngrok_ok  = is_ngrok_alive()

            if not server_ok:
                self._log("[감시] uvicorn 감지 안됨 → 재시작", "error")
                self.root.after(0, self.start_server)
                time.sleep(6)

            if not ngrok_ok:
                self._log("[감시] ngrok 감지 안됨 → 재시작", "error")
                self.root.after(0, self.start_ngrok)
                time.sleep(6)

            if server_ok and ngrok_ok:
                self._log("[감시] 모든 서비스 정상", "ok")

            self.root.after(0, self._refresh_status)
            self.root.after(0, lambda: self.progress.configure(value=0))


def main():
    root = tk.Tk()
    # 아이콘 (없어도 무관)
    try:
        root.iconbitmap(default="")
    except:
        pass
    app = WatchdogApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()

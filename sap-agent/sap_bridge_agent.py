# -*- coding: utf-8 -*-
"""
SAP 브릿지 에이전트 — localhost:7788 (HTTPS)
사용자 PC에서 실행. 브라우저(플랫폼)의 fetch() 요청을 받아 SAP GUI를 자동 제어.

실행: python sap_bridge_agent.py
종료: Ctrl+C 또는 트레이 아이콘 → 종료

HTTPS가 필요한 이유:
  Azure 포털은 HTTPS로 서빙되므로, 브라우저가 http://localhost 호출을
  Mixed Content로 차단합니다. 자체 서명 인증서를 자동 생성하며,
  install_sap_agent.ps1 실행 시 Windows 신뢰 저장소에 등록됩니다.
"""
from __future__ import annotations
import json
import threading
import queue
import sys
import os
import ssl
import socket
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

# Windows cp949 환경에서 UTF-8 출력 강제
if sys.stdout and hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
if sys.stderr and hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

PORT = 7788
SAP_LOGIN_URL = "http://sap.erp.dongwon.com:6060/saplogin//SapMLogon.application?ip=10.200.120.241"

ALLOWED_ORIGINS = [
    "https://dw-fsi-platform-cgg6apc4ffaxb4d5.koreacentral-01.azurewebsites.net",
    "http://localhost:8000",
    "http://localhost:3000",
    "https://localhost:8000",
]

# ── 인증서 경로 ─────────────────────────────────────────
_CERT_DIR  = os.path.join(os.path.expanduser("~"), ".dongwon_sap_bridge")
_CERT_FILE = os.path.join(_CERT_DIR, "cert.pem")
_KEY_FILE  = os.path.join(_CERT_DIR, "key.pem")


def _ensure_cert() -> tuple[str, str]:
    """자체 서명 인증서 생성 (없을 때만). cert_path, key_path 반환."""
    os.makedirs(_CERT_DIR, exist_ok=True)
    if os.path.exists(_CERT_FILE) and os.path.exists(_KEY_FILE):
        return _CERT_FILE, _KEY_FILE

    print("[SAP Bridge] 자체 서명 인증서 생성 중...")
    try:
        # cryptography 패키지 사용 (권장)
        from cryptography import x509
        from cryptography.x509.oid import NameOID
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        import datetime

        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        subject = issuer = x509.Name([
            x509.NameAttribute(NameOID.COUNTRY_NAME, "KR"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Dongwon HomeFoods SAP Bridge"),
            x509.NameAttribute(NameOID.COMMON_NAME, "localhost"),
        ])
        cert = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(issuer)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(datetime.datetime.utcnow())
            .not_valid_after(datetime.datetime.utcnow() + datetime.timedelta(days=3650))
            .add_extension(x509.SubjectAlternativeName([
                x509.DNSName("localhost"),
                x509.IPAddress(__import__("ipaddress").IPv4Address("127.0.0.1")),
            ]), critical=False)
            .sign(key, hashes.SHA256())
        )
        with open(_KEY_FILE, "wb") as f:
            f.write(key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.TraditionalOpenSSL,
                serialization.NoEncryption(),
            ))
        with open(_CERT_FILE, "wb") as f:
            f.write(cert.public_bytes(serialization.Encoding.PEM))
        print(f"[SAP Bridge] 인증서 생성 완료: {_CERT_FILE}")
        return _CERT_FILE, _KEY_FILE

    except ImportError:
        # cryptography 없으면 openssl subprocess 시도
        import subprocess
        result = subprocess.run([
            "openssl", "req", "-x509", "-newkey", "rsa:2048",
            "-keyout", _KEY_FILE, "-out", _CERT_FILE,
            "-days", "3650", "-nodes",
            "-subj", "/C=KR/O=Dongwon SAP Bridge/CN=localhost",
        ], capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(f"openssl 실패: {result.stderr}")
        print(f"[SAP Bridge] openssl 인증서 생성 완료: {_CERT_FILE}")
        return _CERT_FILE, _KEY_FILE


def _trust_cert_windows(cert_path: str) -> None:
    """Windows CurrentUser\\Root 신뢰 저장소에 인증서 등록 (최초 1회).
    PowerShell을 subprocess로 실행. 관리자 권한 불필요(CurrentUser)."""
    import subprocess, os
    flag_file = cert_path + ".trusted"
    if os.path.exists(flag_file):
        return  # 이미 등록됨

    ps_code = f"""
$cert = New-Object System.Security.Cryptography.X509Certificates.X509Certificate2('{cert_path}')
$store = New-Object System.Security.Cryptography.X509Certificates.X509Store(
    [System.Security.Cryptography.X509Certificates.StoreName]::Root,
    [System.Security.Cryptography.X509Certificates.StoreLocation]::CurrentUser
)
$store.Open([System.Security.Cryptography.X509Certificates.OpenFlags]::ReadWrite)
$store.Add($cert)
$store.Close()
Write-Output "OK"
"""
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_code],
            capture_output=True, text=True, timeout=15
        )
        if "OK" in r.stdout:
            print("[SAP Bridge] 인증서 Windows 신뢰 저장소 등록 완료 (Chrome/Edge 경고 없음)")
            # 다음 실행 때 재등록하지 않도록 flag 파일 생성
            open(flag_file, "w").close()
        else:
            print(f"[SAP Bridge] 인증서 신뢰 등록 경고: {r.stderr.strip()[:200]}")
    except Exception as e:
        print(f"[SAP Bridge] 인증서 신뢰 등록 실패 (무시): {e}")


# ── SAP 작업 큐 (HTTP 스레드 → SAP 전용 워커 스레드) ─────────────────
# COM 객체는 STA 스레드에서만 접근 가능하므로, 모든 SAP 호출을
# 단일 워커 스레드(sap_worker)에서 직렬 처리.
_sap_queue: queue.Queue = queue.Queue()


def _dispatch_sap(fn_name: str, payload: dict, timeout: int = 60) -> dict:
    """HTTP 핸들러 스레드에서 SAP 워커 스레드로 작업 위임."""
    event = threading.Event()
    result_holder: list = [None]
    _sap_queue.put((fn_name, payload, event, result_holder))
    ok = event.wait(timeout)
    if not ok:
        return {"success": False, "error": f"SAP 작업 타임아웃 ({timeout}s)"}
    return result_holder[0]


# ════════════════════════════════════════════════════════
#  SAP GUI 제어 모듈
# ════════════════════════════════════════════════════════

def _get_sap_session(skip_session_manager: bool = True):
    """SAP GUI Scripting 세션 반환. 없으면 None.
    skip_session_manager=True이면 SESSION_MANAGER(ses[0]) 건너뛰고 일반 세션 반환.
    """
    try:
        import win32com.client
        sap = win32com.client.GetObject("SAPGUI")
        app = sap.GetScriptingEngine
        if app.Children.Count == 0:
            return None, "SAP 연결 없음"
        conn = app.Children(0)
        if conn.Children.Count == 0:
            return None, "SAP 세션 없음"
        # SESSION_MANAGER는 스크립팅 탐색 불가 → 건너뜀
        if skip_session_manager:
            for i in range(conn.Children.Count):
                try:
                    s = conn.Children(i)
                    txn = s.Info.Transaction
                    if txn != 'SESSION_MANAGER':
                        return s, None
                except Exception:
                    continue
        session = conn.Children(0)
        return session, None
    except Exception as e:
        return None, str(e)


def _is_saplogon_running() -> bool:
    """saplogon.exe 프로세스 존재 여부 (빠른 방식)."""
    try:
        import ctypes
        import ctypes.wintypes
        # CreateToolhelp32Snapshot 사용 — tasklist보다 훨씬 빠름
        TH32CS_SNAPPROCESS = 0x2
        INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
        class PROCESSENTRY32(ctypes.Structure):
            _fields_ = [("dwSize", ctypes.wintypes.DWORD), ("cntUsage", ctypes.wintypes.DWORD),
                        ("th32ProcessID", ctypes.wintypes.DWORD), ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
                        ("th32ModuleID", ctypes.wintypes.DWORD), ("cntThreads", ctypes.wintypes.DWORD),
                        ("th32ParentProcessID", ctypes.wintypes.DWORD), ("pcPriClassBase", ctypes.c_long),
                        ("dwFlags", ctypes.wintypes.DWORD), ("szExeFile", ctypes.c_char * 260)]
        snap = ctypes.windll.kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if snap == INVALID_HANDLE_VALUE:
            return False
        pe = PROCESSENTRY32()
        pe.dwSize = ctypes.sizeof(PROCESSENTRY32)
        found = False
        if ctypes.windll.kernel32.Process32First(snap, ctypes.byref(pe)):
            while True:
                if pe.szExeFile.lower() == b"saplogon.exe":
                    found = True
                    break
                if not ctypes.windll.kernel32.Process32Next(snap, ctypes.byref(pe)):
                    break
        ctypes.windll.kernel32.CloseHandle(snap)
        return found
    except Exception:
        return False


def sap_status() -> dict:
    """SAP GUI 상태 확인."""
    try:
        import win32com.client
        if not _is_saplogon_running():
            return {"status": "not_running", "message": "SAP GUI가 실행되지 않았습니다."}

        session, err = _get_sap_session()
        if err:
            return {"status": "not_logged_in", "message": f"SAP 로그인 필요: {err}"}

        # 현재 트랜잭션/화면 정보
        try:
            wnd = session.FindById("wnd[0]")
            title = wnd.Text
            return {"status": "ok", "title": title}
        except Exception as e:
            return {"status": "not_logged_in", "message": f"세션 확인 실패: {e}"}

    except Exception as e:
        return {"status": "error", "message": str(e)}


def sap_apply_price(payload: dict) -> dict:
    """
    ZSDP0030 고객별 예외판가 변경 적용.

    payload: {
        "vkbur":  "1006",          # 영업부서 코드
        "vwerk":  "4120",          # 플랜트 코드
        "kunnr":  "203612",        # 고객코드 (앞 0 포함 가능)
        "date":   "2026.07.28",    # 적용일자 (P_DATE 형식 YYYY.MM.DD)
        "items":  [
            {"matnr": "100563", "price": 11500},   # 자재코드 + 변경판매가(공급가)
            ...
        ]
    }
    반환: { "success": True/False, "applied": [...], "not_found": [...], "error": "..." }
    """
    session, err = _get_sap_session()
    if err:
        return {"success": False, "error": err}

    import time

    try:
        # ── Step 1: ZSDP0030 접속 ──
        session.StartTransaction("ZSDP0030")
        time.sleep(2)

        wnd = session.FindById("wnd[0]")
        if "예외판가" not in wnd.Text and "ZSDP0030" not in wnd.Text:
            return {"success": False, "error": f"ZSDP0030 화면 진입 실패 (현재: {wnd.Text})"}

        # ── Step 2: 조회 조건 입력 ──
        vkbur = str(payload.get("vkbur", ""))
        vwerk = str(payload.get("vwerk", ""))
        kunnr = str(payload.get("kunnr", ""))
        date  = str(payload.get("date", ""))

        if vkbur:
            session.FindById("wnd[0]/usr/ctxtS_VKBUR-LOW").Text = vkbur
        if vwerk:
            session.FindById("wnd[0]/usr/ctxtS_VWERK-LOW").Text = vwerk
        if kunnr:
            session.FindById("wnd[0]/usr/ctxtS_KUNNR-LOW").Text = kunnr
        if date:
            session.FindById("wnd[0]/usr/ctxtP_DATE").Text = date

        # ── Step 3: F8 실행 ──
        session.FindById("wnd[0]").SendVKey(8)
        time.sleep(3)

        # 팝업 처리 (오류 팝업 등)
        try:
            popup = session.FindById("wnd[1]")
            popup_text = popup.Text
            popup.SendVKey(0)   # Enter로 닫기
            time.sleep(0.5)
            return {"success": False, "error": f"조회 오류 팝업: {popup_text}"}
        except Exception:
            pass  # 팝업 없음 = 정상

        # ── Step 4: 결과 그리드 접근 ──
        try:
            shell = session.FindById("wnd[0]/shellcont[1]/shell")
        except Exception as e:
            return {"success": False, "error": f"결과 그리드 없음: {e}"}

        row_count = shell.RowCount
        if row_count == 0:
            return {"success": False, "error": "조회 결과 없음. 조건을 확인하세요."}

        # ── Step 5: MATNR별 행 찾기 + SALEC0 수정 ──
        items = payload.get("items", [])
        # matnr → price 맵 (앞 0 제거하여 비교)
        item_map: dict[str, int] = {}
        for item in items:
            matnr_key = str(item.get("matnr", "")).lstrip("0")
            item_map[matnr_key] = int(item.get("price", 0))

        applied   = []   # {"matnr": ..., "row": ..., "price": ...}
        not_found = list(item_map.keys())  # 처음엔 전체 미발견으로 시작

        for r in range(row_count):
            try:
                cell_matnr = shell.GetCellValue(r, "MATNR").lstrip("0")
            except Exception:
                continue

            if cell_matnr in item_map:
                new_price = item_map[cell_matnr]
                try:
                    shell.ModifyCell(r, "SALEC0", str(new_price))
                    applied.append({"matnr": cell_matnr, "row": r, "price": new_price})
                    if cell_matnr in not_found:
                        not_found.remove(cell_matnr)
                except Exception as e:
                    applied.append({"matnr": cell_matnr, "row": r, "price": new_price, "modify_error": str(e)})

        if not applied:
            return {
                "success": False,
                "error": "변경할 자재를 그리드에서 찾지 못했습니다.",
                "not_found": not_found,
            }

        # ── Step 6: 저장 (Ctrl+S) ──
        session.FindById("wnd[0]").SendVKey(11)
        time.sleep(1.5)

        # 저장 확인 팝업 처리 (예/확인 버튼 자동 클릭)
        save_popup = ""
        for _ in range(3):
            try:
                popup = session.FindById("wnd[1]")
                save_popup = popup.Text
                popup.SendVKey(0)   # Enter (=확인/예)
                time.sleep(0.5)
            except Exception:
                break

        # 상태바 메시지 확인
        status_msg = ""
        try:
            status_msg = session.FindById("wnd[0]/sbar").Text
        except Exception:
            pass

        return {
            "success":    True,
            "applied":    applied,
            "not_found":  not_found,
            "save_popup": save_popup,
            "status_msg": status_msg,
        }

    except Exception as e:
        return {"success": False, "error": str(e)}


def sap_upload_price(payload: dict) -> dict:
    """
    ZSDP0031 고객별 예외판가 엑셀 등록 (기간 지정 프로모션 방식).

    payload: {
        "items": [
            {"vwerk": "4120", "kunnr": "203612", "matnr": "16248",
             "price": "9800", "date_from": "20260801", "date_to": "20260831"},
            ...
        ],
        "mode": "N"   # BDC 모드 N/A/E (기본 N)
    }
    """
    session, err = _get_sap_session()
    if err:
        return {"success": False, "error": err}

    import time
    import os
    import tempfile

    items = payload.get("items", [])
    if not items:
        return {"success": False, "error": "items 비어있음"}

    # ── Step 1: 업로드 텍스트 파일 생성 ──
    tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False,
                                      encoding="ansi", newline="\n")
    lines = []
    for item in items:
        vwerk     = str(item.get("vwerk", "")).strip()
        kunnr     = str(item.get("kunnr", "")).strip()
        matnr     = str(item.get("matnr", "")).strip()
        price     = str(item.get("price", "")).strip()
        date_from = str(item.get("date_from", "")).strip().replace(".", "")
        date_to   = str(item.get("date_to",   "")).strip().replace(".", "")
        lines.append(f"{vwerk}\t{kunnr}\t{matnr}\t{price}\t{date_from}\t{date_to}")
    tmp.write("\n".join(lines))
    tmp.close()
    abs_path = os.path.abspath(tmp.name)

    try:
        # ── Step 2: ZSDP0031 접속 ──
        session.StartTransaction("ZSDP0031")
        time.sleep(2)

        wnd = session.FindById("wnd[0]")
        if "예외판가" not in wnd.Text and "ZSDP0031" not in wnd.Text:
            return {"success": False, "error": f"ZSDP0031 진입 실패 (현재: {wnd.Text})"}

        # 구분: 요청/생성 (radRB_RQ — 실제 ID는 rad 접두어 사용)
        try:
            session.FindById("wnd[0]/usr/radRB_RQ").Select()
        except Exception:
            pass

        # 파일 경로 입력
        session.FindById("wnd[0]/usr/ctxtFILENAME").Text = abs_path

        # BDC 모드 N (자동 실행, 화면 없음)
        mode = str(payload.get("mode", "N"))
        session.FindById("wnd[0]/usr/ctxtP_MODE").Text = mode

        # ── Step 3: F8 실행 ──
        session.FindById("wnd[0]").SendVKey(8)
        time.sleep(4)

        # 팝업 처리
        messages = []
        for _ in range(5):
            try:
                popup = session.FindById("wnd[1]")
                messages.append(popup.Text)
                popup.SendVKey(0)
                time.sleep(0.5)
            except Exception:
                break

        # 상태바
        status_msg = ""
        try:
            status_msg = session.FindById("wnd[0]/sbar").Text
        except Exception:
            pass

        # ── 결과 화면 그리드 파싱 (고객별 예외판가 일괄 등록 화면) ──
        result_rows = []
        success_count = 0
        error_count = 0
        try:
            shell = session.FindById("wnd[0]/usr/cntlCONTAINER/shellcont/shell")
            co = shell.ColumnOrder
            col_names = [co.ElementAt(i) for i in range(co.Count)]
            for r in range(shell.RowCount):
                row = {}
                for nm in col_names:
                    try:
                        v = shell.GetCellValue(r, nm)
                        if v:
                            row[nm] = v
                    except Exception:
                        pass
                result_rows.append(row)
                icon = row.get("ICON", "")
                if "@EB@" in icon or "@AV@" in icon:  # 초록 LED = 성공
                    success_count += 1
                elif row.get("MESSAGE"):
                    error_count += 1
        except Exception:
            pass

        # ── Step 4: 판가 생성 (RUN 버튼) ──
        # @EB@ = Upload 정상, 이 상태에서 RUN을 눌러야 실제 조건레코드 저장됨
        saved_count = 0
        save_error = None
        if success_count > 0:
            try:
                shell.PressToolbarButton('RUN')
                time.sleep(3)
                # 팝업 처리
                for _ in range(3):
                    try:
                        popup = session.FindById("wnd[1]")
                        messages.append("RUN_POPUP:" + popup.Text)
                        popup.SendVKey(0)
                        time.sleep(0.5)
                    except Exception:
                        break
                # 저장 결과 재파싱
                try:
                    shell2 = session.FindById("wnd[0]/usr/cntlCONTAINER/shellcont/shell")
                    result_rows = []
                    co2 = shell2.ColumnOrder
                    col_names2 = [co2.ElementAt(i) for i in range(co2.Count)]
                    for r in range(shell2.RowCount):
                        row = {}
                        for nm in col_names2:
                            try:
                                v = shell2.GetCellValue(r, nm)
                                if v:
                                    row[nm] = v
                            except Exception:
                                pass
                        result_rows.append(row)
                        icon = row.get("ICON", "")
                        # @08@ = 저장 완료 (초록 체크)
                        if "@08@" in icon or "@AV@" in icon:
                            saved_count += 1
                except Exception as e2:
                    save_error = str(e2)
                status_msg = session.FindById("wnd[0]/sbar").Text
            except Exception as e2:
                save_error = str(e2)

        success = saved_count > 0 or (save_error is None and success_count > 0)

        return {
            "success":       success,
            "file":          abs_path,
            "item_count":    len(items),
            "success_count": success_count,
            "saved_count":   saved_count,
            "error_count":   error_count,
            "status_msg":    status_msg,
            "messages":      messages,
            "rows":          result_rows,
            "save_error":    save_error,
        }

    except Exception as e:
        return {"success": False, "error": str(e)}
    finally:
        try:
            os.unlink(abs_path)
        except Exception:
            pass


def sap_get_screen_info() -> dict:
    """현재 SAP 화면 구조 스캔 (개발용 — 필드 ID 매핑에 사용)."""
    session, err = _get_sap_session()
    if err:
        return {"error": err}
    try:
        wnd = session.FindById("wnd[0]")
        info = {
            "title": wnd.Text,
            "transaction": "",
            "fields": []
        }
        try:
            info["transaction"] = session.Info.Transaction
        except Exception:
            pass
        return info
    except Exception as e:
        return {"error": str(e)}


# ════════════════════════════════════════════════════════
#  HTTP 서버
# ════════════════════════════════════════════════════════

class SapBridgeHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        print(f"[Agent] {self.address_string()} {format % args}")

    def _cors_headers(self):
        origin = self.headers.get("Origin", "")
        if origin in ALLOWED_ORIGINS:
            self.send_header("Access-Control-Allow-Origin", origin)
        else:
            self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.send_header("Access-Control-Max-Age", "86400")

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors_headers()
        self.end_headers()

    def _json_response(self, data: dict, status: int = 200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._cors_headers()
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def do_GET(self):
        path = urlparse(self.path).path

        if path == "/":
            self._json_response({"agent": "SAP Bridge Agent", "version": "1.0", "port": PORT})

        elif path == "/sap/status":
            self._json_response(_dispatch_sap("status", {}))

        elif path == "/sap/screen":
            self._json_response(_dispatch_sap("screen", {}))

        elif path == "/sap/login-url":
            self._json_response({"url": SAP_LOGIN_URL})

        else:
            self._json_response({"error": "not found"}, 404)

    def do_POST(self):
        path = urlparse(self.path).path

        if path == "/sap/apply-price":
            payload = self._read_body()
            self._json_response(_dispatch_sap("apply_price", payload, timeout=90))

        elif path == "/sap/upload-price":
            payload = self._read_body()
            self._json_response(_dispatch_sap("upload_price", payload, timeout=90))

        elif path == "/sap/ping":
            self._json_response({"pong": True})

        else:
            self._json_response({"error": "not found"}, 404)


# ════════════════════════════════════════════════════════
#  SAP 워커 스레드 (COM STA — 모든 SAP 호출 직렬 처리)
# ════════════════════════════════════════════════════════

_SAP_FN_MAP = {
    "status":       lambda p: sap_status(),
    "apply_price":  lambda p: sap_apply_price(p),
    "upload_price": lambda p: sap_upload_price(p),
    "screen":       lambda p: sap_get_screen_info(),
}


def sap_worker_loop():
    """SAP 전용 워커 스레드. COM STA 초기화 후 큐 처리."""
    try:
        import pythoncom
        pythoncom.CoInitializeEx(pythoncom.COINIT_APARTMENTTHREADED)
    except Exception as e:
        print(f"[SAP Worker] CoInitialize 실패: {e}")

    print("[SAP Worker] 시작")
    while True:
        try:
            fn_name, payload, event, result_holder = _sap_queue.get(timeout=1)
        except queue.Empty:
            continue
        except Exception:
            break

        fn = _SAP_FN_MAP.get(fn_name)
        if fn:
            try:
                result_holder[0] = fn(payload)
            except Exception as e:
                result_holder[0] = {"success": False, "error": str(e)}
        else:
            result_holder[0] = {"error": f"unknown function: {fn_name}"}

        event.set()


# ════════════════════════════════════════════════════════
#  진입점
# ════════════════════════════════════════════════════════

def run_server():
    # SAP 전용 워커 스레드 시작 (데몬)
    worker = threading.Thread(target=sap_worker_loop, daemon=True, name="sap-worker")
    worker.start()

    # 자체 서명 인증서 준비
    cert_file, key_file = _ensure_cert()
    _trust_cert_windows(cert_file)  # 최초 실행 시 Windows 신뢰 저장소 자동 등록

    server = ThreadingHTTPServer(("127.0.0.1", PORT), SapBridgeHandler)

    # SSL 래핑
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(certfile=cert_file, keyfile=key_file)
    server.socket = ctx.wrap_socket(server.socket, server_side=True)

    print(f"[SAP Bridge Agent] START -- https://localhost:{PORT} (HTTPS, threaded)")
    print(f"[SAP Bridge Agent] 인증서: {cert_file}")
    print(f"[SAP Bridge Agent] STOP: Ctrl+C")
    print()
    print("  >> 준비 완료. 포털에서 판가 적용 DM 발송 버튼을 클릭하세요.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[SAP Bridge Agent] SHUTDOWN")
        server.shutdown()


if __name__ == "__main__":
    run_server()

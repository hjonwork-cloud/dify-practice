"""
크롤링 완료 리포트 메일 발송 모듈
환경변수:
  SMTP_HOST     - SMTP 서버 호스트 (예: smtp.dongwon.com)
  SMTP_PORT     - SMTP 포트 (기본 587)
  SMTP_USER     - 발신 계정
  SMTP_PASSWORD - 발신 계정 비밀번호
  SMTP_FROM     - 발신자 표시 주소 (기본: SMTP_USER)
  SMTP_TLS      - TLS 사용 여부 (기본 "true")
"""
from __future__ import annotations

import os
import smtplib
import logging
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import datetime

logger = logging.getLogger(__name__)

REPORT_TO = "3shark3@dongwon.com"


def _env(key: str, default: str = "") -> str:
    return os.getenv(key, default).strip()


def _smtp_configured() -> bool:
    return bool(_env("SMTP_HOST") and _env("SMTP_USER") and _env("SMTP_PASSWORD"))


def _build_html(report: dict) -> str:
    """크롤링 결과 리포트 HTML 이메일 생성."""
    crawl_date   = report.get("crawl_date", "")
    total        = report.get("total_saved", 0)
    duration_sec = report.get("duration_sec", 0)
    baemin_cnt   = report.get("baemin_count", 0)
    food_cnt     = report.get("food_count", 0)
    failed       = report.get("failed_sellers", [])
    stderr_log   = report.get("stderr", "")
    seller_rows  = report.get("seller_summary", [])  # [{platform, seller_id, count}]
    status       = "✅ 성공" if not failed else f"⚠️ 부분 실패 ({len(failed)}개 셀러)"
    status_color = "#10b981" if not failed else "#f59e0b"
    now_str      = datetime.now().strftime("%Y-%m-%d %H:%M")

    # 셀러 요약 테이블 행
    seller_html = ""
    for s in seller_rows[:20]:
        pf_badge = (
            '<span style="background:#3b82f6;color:#fff;padding:2px 8px;border-radius:4px;font-size:11px;">배민</span>'
            if s.get("platform") == "baemin" else
            '<span style="background:#10b981;color:#fff;padding:2px 8px;border-radius:4px;font-size:11px;">식봄</span>'
        )
        seller_html += f"""
        <tr style="border-bottom:1px solid #f1f5f9;">
          <td style="padding:8px 12px;">{pf_badge}</td>
          <td style="padding:8px 12px;font-size:13px;color:#374151;">{s.get('seller_name','')}</td>
          <td style="padding:8px 12px;text-align:right;font-weight:600;color:#1e40af;">{s.get('count',0):,}건</td>
        </tr>"""

    failed_html = ""
    if failed:
        for f in failed:
            failed_html += f'<li style="color:#ef4444;font-size:13px;">{f}</li>'
        failed_html = f'<ul style="margin:8px 0;padding-left:20px;">{failed_html}</ul>'

    # 바 차트 (배민 vs 식봄 비율)
    baemin_pct = round(baemin_cnt / total * 100) if total else 0
    food_pct   = 100 - baemin_pct

    return f"""<!DOCTYPE html>
<html lang="ko">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>플랫폼 가격 크롤링 리포트</title></head>
<body style="margin:0;padding:0;background:#f8fafc;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;">

<div style="max-width:600px;margin:32px auto;background:#fff;border-radius:16px;overflow:hidden;
            box-shadow:0 4px 24px rgba(0,0,0,.08);">

  <!-- 헤더 -->
  <div style="background:linear-gradient(135deg,#1e40af 0%,#3b82f6 100%);padding:32px 32px 24px;">
    <div style="font-size:22px;font-weight:700;color:#fff;letter-spacing:-.3px;">
      📊 플랫폼 가격 크롤링 리포트
    </div>
    <div style="font-size:14px;color:#bfdbfe;margin-top:6px;">{crawl_date} 수집 완료 · {now_str} 발송</div>
    <div style="display:inline-block;margin-top:14px;padding:6px 16px;border-radius:999px;
                background:rgba(255,255,255,.15);color:#fff;font-size:13px;font-weight:600;">
      {status}
    </div>
  </div>

  <!-- 요약 카드 3개 -->
  <div style="display:flex;gap:0;border-bottom:1px solid #f1f5f9;">
    <div style="flex:1;padding:24px;text-align:center;border-right:1px solid #f1f5f9;">
      <div style="font-size:28px;font-weight:700;color:#1e40af;">{total:,}</div>
      <div style="font-size:12px;color:#64748b;margin-top:4px;">총 수집 건수</div>
    </div>
    <div style="flex:1;padding:24px;text-align:center;border-right:1px solid #f1f5f9;">
      <div style="font-size:28px;font-weight:700;color:#3b82f6;">{baemin_cnt:,}</div>
      <div style="font-size:12px;color:#64748b;margin-top:4px;">배민상회</div>
    </div>
    <div style="flex:1;padding:24px;text-align:center;">
      <div style="font-size:28px;font-weight:700;color:#10b981;">{food_cnt:,}</div>
      <div style="font-size:12px;color:#64748b;margin-top:4px;">식봄</div>
    </div>
  </div>

  <div style="padding:24px 32px;">

    <!-- 플랫폼 비율 바 -->
    <div style="margin-bottom:24px;">
      <div style="font-size:13px;font-weight:600;color:#374151;margin-bottom:10px;">플랫폼별 비율</div>
      <div style="display:flex;height:10px;border-radius:999px;overflow:hidden;background:#e2e8f0;">
        <div style="width:{baemin_pct}%;background:#3b82f6;transition:width .3s;"></div>
        <div style="width:{food_pct}%;background:#10b981;"></div>
      </div>
      <div style="display:flex;gap:16px;margin-top:8px;font-size:12px;color:#64748b;">
        <span>🔵 배민상회 {baemin_pct}%</span>
        <span>🟢 식봄 {food_pct}%</span>
      </div>
    </div>

    <!-- 소요시간 -->
    <div style="background:#f8fafc;border-radius:10px;padding:14px 18px;margin-bottom:24px;
                display:flex;align-items:center;gap:12px;">
      <div style="font-size:22px;">⏱</div>
      <div>
        <div style="font-size:13px;font-weight:600;color:#374151;">소요 시간</div>
        <div style="font-size:20px;font-weight:700;color:#1e40af;">{int(duration_sec // 60)}분 {int(duration_sec % 60)}초</div>
      </div>
    </div>

    <!-- 셀러별 수집 현황 -->
    {"" if not seller_rows else f'''
    <div style="margin-bottom:24px;">
      <div style="font-size:13px;font-weight:600;color:#374151;margin-bottom:10px;">셀러별 수집 현황 (상위 {min(len(seller_rows),20)}개)</div>
      <table style="width:100%;border-collapse:collapse;font-size:13px;">
        <thead>
          <tr style="background:#f1f5f9;border-bottom:2px solid #e2e8f0;">
            <th style="padding:8px 12px;text-align:left;font-weight:600;color:#374151;font-size:12px;">플랫폼</th>
            <th style="padding:8px 12px;text-align:left;font-weight:600;color:#374151;font-size:12px;">셀러</th>
            <th style="padding:8px 12px;text-align:right;font-weight:600;color:#374151;font-size:12px;">수집건수</th>
          </tr>
        </thead>
        <tbody>{seller_html}</tbody>
      </table>
    </div>'''}

    <!-- 실패 셀러 -->
    {"" if not failed else f'''
    <div style="background:#fff7ed;border:1px solid #fed7aa;border-radius:10px;padding:14px 18px;margin-bottom:24px;">
      <div style="font-size:13px;font-weight:700;color:#c2410c;margin-bottom:6px;">⚠️ 수집 실패 셀러</div>
      {failed_html}
    </div>'''}

    <!-- stderr 로그 (오류 시에만) -->
    {"" if not stderr_log else f'''
    <div style="background:#1e1e2e;border-radius:10px;padding:14px 18px;margin-bottom:24px;">
      <div style="font-size:12px;font-weight:700;color:#f38ba8;margin-bottom:8px;">🔴 오류 로그 (stderr)</div>
      <pre style="margin:0;font-size:11px;color:#cdd6f4;font-family:monospace;white-space:pre-wrap;word-break:break-all;">{stderr_log}</pre>
    </div>'''}

  </div>

  <!-- 푸터 -->
  <div style="background:#f8fafc;padding:16px 32px;border-top:1px solid #e2e8f0;
              font-size:12px;color:#94a3b8;text-align:center;">
    동원 포털 · 플랫폼 가격 모니터링 시스템 · 자동 발송 메일입니다
  </div>
</div>
</body>
</html>"""


def send_report(report: dict) -> bool:
    """크롤링 완료 리포트 메일 발송. 성공 시 True 반환."""
    if not _smtp_configured():
        logger.warning("[mailer] SMTP 미설정 — 메일 발송 건너뜀 "
                       "(SMTP_HOST / SMTP_USER / SMTP_PASSWORD 환경변수 설정 필요)")
        return False

    host     = _env("SMTP_HOST")
    port     = int(_env("SMTP_PORT", "587"))
    user     = _env("SMTP_USER")
    password = _env("SMTP_PASSWORD")
    from_addr = _env("SMTP_FROM") or user
    use_tls  = _env("SMTP_TLS", "true").lower() != "false"

    crawl_date = report.get("crawl_date", "")
    total      = report.get("total_saved", 0)
    subject    = f"[가격모니터링] {crawl_date} 크롤링 완료 — {total:,}건 수집"

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = f"동원포털 가격모니터링 <{from_addr}>"
    msg["To"]      = REPORT_TO

    html_body = _build_html(report)
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    try:
        if use_tls:
            with smtplib.SMTP(host, port, timeout=30) as s:
                s.ehlo()
                s.starttls()
                s.login(user, password)
                s.sendmail(from_addr, [REPORT_TO], msg.as_string())
        else:
            with smtplib.SMTP_SSL(host, port, timeout=30) as s:
                s.login(user, password)
                s.sendmail(from_addr, [REPORT_TO], msg.as_string())
        logger.info(f"[mailer] 리포트 메일 발송 완료 → {REPORT_TO}")
        return True
    except Exception as e:
        logger.error(f"[mailer] 메일 발송 실패: {e}")
        return False


def _build_platform_html(report: dict, platform: str) -> str:
    """배민 or 식봄 전용 리포트 HTML 생성."""
    crawl_date   = report.get("crawl_date", "")
    total        = report.get("total_saved", 0)
    duration_sec = report.get("duration_sec", 0)
    failed       = report.get("failed_sellers", [])
    stderr_log   = report.get("stderr", "")
    seller_rows  = report.get("seller_summary", [])
    now_str      = datetime.now().strftime("%Y-%m-%d %H:%M")

    is_baemin   = (platform == "baemin")
    pf_label    = "배민상회" if is_baemin else "식봄"
    accent      = "#3b82f6" if is_baemin else "#10b981"
    accent_dark = "#1e40af" if is_baemin else "#065f46"
    pf_icon     = "🟦" if is_baemin else "🟩"
    status      = "✅ 성공" if not failed else f"⚠️ 일부 실패 ({len(failed)}개)"
    status_color = "#10b981" if not failed else "#f59e0b"

    # 셀러별 테이블
    seller_html = ""
    for s in seller_rows[:30]:
        seller_html += f"""
        <tr style="border-bottom:1px solid #f1f5f9;">
          <td style="padding:8px 14px;font-size:13px;color:#374151;font-weight:600;">{s.get('seller_name') or s.get('seller_id','')}</td>
          <td style="padding:8px 14px;font-size:12px;color:#94a3b8;">{s.get('seller_id','')}</td>
          <td style="padding:8px 14px;text-align:right;font-weight:700;color:{accent};">{s.get('count',0):,}건</td>
        </tr>"""

    failed_html = ""
    if failed:
        for f in failed:
            failed_html += f'<li style="color:#ef4444;font-size:13px;">{f}</li>'
        failed_html = f'<ul style="margin:8px 0;padding-left:20px;">{failed_html}</ul>'

    seller_section = f"""
    <div style="margin-bottom:24px;">
      <div style="font-size:13px;font-weight:700;color:#374151;margin-bottom:10px;">셀러별 수집 현황</div>
      <table style="width:100%;border-collapse:collapse;font-size:13px;">
        <thead>
          <tr style="background:#f1f5f9;border-bottom:2px solid #e2e8f0;">
            <th style="padding:8px 14px;text-align:left;font-weight:600;color:#374151;font-size:12px;">셀러명</th>
            <th style="padding:8px 14px;text-align:left;font-weight:600;color:#374151;font-size:12px;">셀러ID</th>
            <th style="padding:8px 14px;text-align:right;font-weight:600;color:#374151;font-size:12px;">수집건수</th>
          </tr>
        </thead>
        <tbody>{seller_html}</tbody>
      </table>
    </div>""" if seller_rows else ""

    return f"""<!DOCTYPE html>
<html lang="ko">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>{pf_label} 크롤링 리포트</title></head>
<body style="margin:0;padding:0;background:#f8fafc;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;">
<div style="max-width:560px;margin:32px auto;background:#fff;border-radius:16px;overflow:hidden;box-shadow:0 4px 24px rgba(0,0,0,.08);">

  <!-- 헤더 -->
  <div style="background:linear-gradient(135deg,{accent_dark} 0%,{accent} 100%);padding:28px 32px 22px;">
    <div style="font-size:20px;font-weight:700;color:#fff;">{pf_icon} {pf_label} 크롤링 완료</div>
    <div style="font-size:13px;color:rgba(255,255,255,.75);margin-top:5px;">{crawl_date} · {now_str} 발송</div>
    <div style="display:inline-block;margin-top:12px;padding:5px 14px;border-radius:999px;
                background:rgba(255,255,255,.18);color:#fff;font-size:12px;font-weight:600;">{status}</div>
  </div>

  <!-- KPI -->
  <div style="display:flex;border-bottom:1px solid #f1f5f9;">
    <div style="flex:1;padding:22px;text-align:center;border-right:1px solid #f1f5f9;">
      <div style="font-size:30px;font-weight:800;color:{accent};">{total:,}</div>
      <div style="font-size:12px;color:#64748b;margin-top:4px;">총 수집 건수</div>
    </div>
    <div style="flex:1;padding:22px;text-align:center;border-right:1px solid #f1f5f9;">
      <div style="font-size:30px;font-weight:800;color:#374151;">{len(seller_rows)}</div>
      <div style="font-size:12px;color:#64748b;margin-top:4px;">수집 셀러 수</div>
    </div>
    <div style="flex:1;padding:22px;text-align:center;">
      <div style="font-size:30px;font-weight:800;color:#374151;">{int(duration_sec//60)}분{int(duration_sec%60)}초</div>
      <div style="font-size:12px;color:#64748b;margin-top:4px;">소요 시간</div>
    </div>
  </div>

  <div style="padding:24px 32px;">
    {seller_section}

    {'<div style="background:#fff7ed;border:1px solid #fed7aa;border-radius:10px;padding:14px 18px;margin-bottom:16px;"><div style="font-size:13px;font-weight:700;color:#c2410c;margin-bottom:6px;">⚠ 실패 셀러</div>' + failed_html + '</div>' if failed else ''}
    {'<div style="background:#1e1e2e;border-radius:10px;padding:14px 18px;"><div style="font-size:12px;font-weight:700;color:#f38ba8;margin-bottom:8px;">오류 로그</div><pre style="margin:0;font-size:11px;color:#cdd6f4;font-family:monospace;white-space:pre-wrap;">' + stderr_log + '</pre></div>' if stderr_log else ''}
  </div>

  <div style="background:#f8fafc;padding:14px 32px;border-top:1px solid #e2e8f0;font-size:11px;color:#94a3b8;text-align:center;">
    동원포털 · 가격모니터링 시스템 · 자동 발송
  </div>
</div>
</body>
</html>"""


def _send(report: dict, platform: str) -> bool:
    """platform별 메일 발송 공통 헬퍼."""
    if not _smtp_configured():
        logger.warning("[mailer] SMTP 미설정 — 메일 발송 건너뜀")
        return False
    host      = _env("SMTP_HOST")
    port      = int(_env("SMTP_PORT", "587"))
    user      = _env("SMTP_USER")
    password  = _env("SMTP_PASSWORD")
    from_addr = _env("SMTP_FROM") or user
    use_tls   = _env("SMTP_TLS", "true").lower() != "false"

    pf_label  = "배민상회" if platform == "baemin" else "식봄"
    crawl_date = report.get("crawl_date", "")
    total      = report.get("total_saved", 0)
    subject    = f"[가격모니터링] {crawl_date} {pf_label} 크롤링 완료 — {total:,}건"

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = f"동원포털 가격모니터링 <{from_addr}>"
    msg["To"]      = REPORT_TO
    msg.attach(MIMEText(_build_platform_html(report, platform), "html", "utf-8"))

    try:
        if use_tls:
            with smtplib.SMTP(host, port, timeout=30) as s:
                s.ehlo(); s.starttls(); s.login(user, password)
                s.sendmail(from_addr, [REPORT_TO], msg.as_string())
        else:
            with smtplib.SMTP_SSL(host, port, timeout=30) as s:
                s.login(user, password)
                s.sendmail(from_addr, [REPORT_TO], msg.as_string())
        logger.info(f"[mailer] {pf_label} 메일 발송 완료 → {REPORT_TO}")
        return True
    except Exception as e:
        logger.error(f"[mailer] {pf_label} 메일 발송 실패: {e}")
        return False


def send_baemin_report(report: dict) -> bool:
    """배민상회 전용 크롤링 완료 메일 발송."""
    return _send(report, "baemin")


def send_foodspring_report(report: dict) -> bool:
    """식봄 전용 크롤링 완료 메일 발송."""
    return _send(report, "foodspring")

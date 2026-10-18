#!/usr/bin/env python3
"""Bind the WeChat ClawBot by QR scan (outbound HTTPS only).

  python3 login.py               # foreground
  python3 login.py --background  # detach; watch login_status.json / logs/login.log
  python3 login.py --dry-run     # offline: render a dummy QR, no network

Outputs
  qr.png              QR image to scan with phone WeChat
  login_status.json   {status, qr_png, fallback_url, ...}  (never contains tokens)
  state/credentials.json (0600) on confirm: bot_token, ilink_bot_id, baseurl, ilink_user_id
If the phone shows digits (need_verifycode), write them to verifycode.txt.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

import requests

import ilink_common as C

log = C.setup_logging("login", C.LOGS / "login.log")
BOT_TYPE = "3"
QR_POLL_TIMEOUT_S = 35
MAX_QR_REFRESH = 3


def status(state: str, **extra) -> None:
    data = {"status": state, "updated_at": C.now_iso(), "qr_png": str(C.QR_PNG),
            "verifycode_file": str(C.VERIFYCODE_FILE), "pid": os.getpid(), **extra}
    C.write_json(C.LOGIN_STATUS, data, 0o600)
    log.info("status=%s %s", state, {k: v for k, v in extra.items() if k != "fallback_url"})


def render_qr(content: str) -> None:
    try:
        import qrcode  # type: ignore
        img = qrcode.make(content, box_size=10, border=4)
        img.save(str(C.QR_PNG))
    except ImportError:
        subprocess.run(["qrencode", "-o", str(C.QR_PNG), "-s", "10", content], check=True)
    os.chmod(C.QR_PNG, 0o600)
    try:  # ASCII copy in the log for terminal use
        import io
        import qrcode  # type: ignore
        q = qrcode.QRCode(border=1)
        q.add_data(content)
        buf = io.StringIO()
        q.print_ascii(out=buf)
        log.info("QR (ascii):\n%s", buf.getvalue())
    except Exception:
        pass


def fetch_qr() -> tuple[str, str]:
    old = C.load_credentials()
    tokens = [old["bot_token"]] if old else []
    r = requests.post(C.api_url(C.FIXED_BASE_URL, f"ilink/bot/get_bot_qrcode?bot_type={BOT_TYPE}"),
                      headers=C.post_headers(), json={"local_token_list": tokens[:10]}, timeout=20)
    r.raise_for_status()
    d = r.json()
    if not d.get("qrcode") or not d.get("qrcode_img_content"):
        raise RuntimeError(f"unexpected get_bot_qrcode response keys={sorted(d)} ret={d.get('ret')}")
    C.register_secret(d["qrcode"])
    return d["qrcode"], d["qrcode_img_content"]


def poll_status(base: str, qrcode: str, verify_code: str | None) -> dict:
    params = {"qrcode": qrcode}
    if verify_code:
        params["verify_code"] = verify_code
    try:
        r = requests.get(C.api_url(base, "ilink/bot/get_qrcode_status"), params=params,
                         headers=C.common_headers(), timeout=QR_POLL_TIMEOUT_S + 1)
        r.raise_for_status()
        return r.json()
    except requests.Timeout:
        return {"status": "wait"}
    except requests.RequestException as e:  # plugin treats gateway/network errors as wait
        log.warning("poll network error (%s), treating as wait", type(e).__name__)
        time.sleep(2)
        return {"status": "wait"}


def read_verifycode() -> str | None:
    try:
        code = C.VERIFYCODE_FILE.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    C.VERIFYCODE_FILE.unlink(missing_ok=True)
    code = "".join(ch for ch in code if ch.isdigit())
    return code or None


def save_credentials(d: dict) -> None:
    creds = {"bot_token": d.get("bot_token"), "ilink_bot_id": d.get("ilink_bot_id"),
             "baseurl": d.get("baseurl") or C.FIXED_BASE_URL, "ilink_user_id": d.get("ilink_user_id"),
             "saved_at": C.now_iso()}
    C.register_secret(creds["bot_token"])
    C.STATE.mkdir(parents=True, exist_ok=True)
    os.chmod(C.STATE, 0o700)
    C.write_json(C.CREDENTIALS, creds, 0o600)
    # new session: old cursor/context/expired flag are meaningless
    for p in (C.CURSOR, C.CONTEXT, C.EXPIRED_FLAG, C.SEEN_IDS):
        p.unlink(missing_ok=True)


def new_qr(refresh: int) -> str:
    qrcode, url = fetch_qr()
    render_qr(url)
    print(f"二维码: {C.QR_PNG}\n备用链接(在微信里打开): {url}", flush=True)
    status("wait", fallback_url=url, qr_refresh=refresh, qr_created_at=C.now_iso())
    return qrcode


def run(timeout_s: int) -> int:
    deadline = time.time() + timeout_s
    refresh = 0
    try:
        qrcode = new_qr(refresh)
    except Exception as e:
        status("error", error=C.redact(f"get_bot_qrcode failed: {type(e).__name__}: {e}")[:300])
        return 3
    base = C.FIXED_BASE_URL
    pending_code: str | None = None
    last = "wait"
    fallback = C.read_json(C.LOGIN_STATUS, {}).get("fallback_url")
    while time.time() < deadline:
        d = poll_status(base, qrcode, pending_code)
        st = d.get("status") or "wait"
        if st != last and st not in ("need_verifycode", "confirmed", "binded_redirect"):
            status(st, fallback_url=fallback, qr_refresh=refresh)
        last = st
        if st == "wait":
            pass
        elif st == "scaned":
            pending_code = None
        elif st == "need_verifycode":
            wrong = pending_code is not None
            status("need_verifycode", fallback_url=fallback, wrong_code=wrong,
                   hint=f"把手机微信显示的数字写进 {C.VERIFYCODE_FILE}")
            print("需要验证码：把手机上显示的数字写入 verifycode.txt", flush=True)
            pending_code = None
            while time.time() < deadline and pending_code is None:
                pending_code = read_verifycode()
                if pending_code is None:
                    time.sleep(1)
            if pending_code:
                status("verifycode_submitted", fallback_url=fallback)
            continue
        elif st in ("expired", "verify_code_blocked"):
            refresh += 1
            pending_code = None
            if refresh > MAX_QR_REFRESH:
                status("failed", reason=f"{st} x{refresh}", fallback_url=fallback)
                return 4
            try:
                qrcode = new_qr(refresh)
                fallback = C.read_json(C.LOGIN_STATUS, {}).get("fallback_url")
                base = C.FIXED_BASE_URL
                last = "wait"
            except Exception as e:
                status("error", error=C.redact(f"refresh failed: {e}")[:300])
                return 3
        elif st == "scaned_but_redirect":
            host = d.get("redirect_host")
            if host:
                base = f"https://{host}"
                log.info("IDC redirect -> %s", host)
        elif st == "binded_redirect":
            status("already_bound", has_local_credentials=C.load_credentials() is not None)
            print("已绑定过（服务器返回 binded_redirect），沿用现有凭证。", flush=True)
            return 0 if C.load_credentials() else 5
        elif st == "confirmed":
            if not d.get("bot_token") or not d.get("ilink_bot_id") or not d.get("ilink_user_id"):
                status("error", error="confirmed but bot_token/ilink_bot_id/ilink_user_id missing")
                return 3
            save_credentials(d)
            status("confirmed", ilink_bot_id=d.get("ilink_bot_id"),
                   credentials=str(C.CREDENTIALS))
            C.QR_PNG.unlink(missing_ok=True)
            print("绑定成功。请先在 ClawBot 对话里发一句话，然后运行 start.sh。", flush=True)
            return 0
        else:
            log.warning("unknown status %r", st)
        time.sleep(1)
    status("timeout", fallback_url=fallback)
    return 6


def dry_run() -> int:
    url = "https://example.invalid/dry-run-qr"
    render_qr(url)
    status("dry_run", fallback_url=url)
    print(f"[dry-run] 已生成测试二维码 {C.QR_PNG}，未联网", flush=True)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--background", action="store_true", help="detach and log to logs/login.log")
    ap.add_argument("--timeout", type=int, default=480, help="overall wait seconds (plugin default 480)")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    if a.dry_run:
        return dry_run()
    if a.background:
        args = [sys.executable, str(Path(__file__).resolve()), "--timeout", str(a.timeout)]
        out = open(C.LOGS / "login.out", "ab")
        p = subprocess.Popen(args, stdout=out, stderr=out, stdin=subprocess.DEVNULL,
                             start_new_session=True, cwd=str(C.ROOT))
        (C.STATE / "login.pid").write_text(str(p.pid))
        print(f"login running in background pid={p.pid}; watch {C.LOGIN_STATUS}")
        return 0
    C.VERIFYCODE_FILE.unlink(missing_ok=True)
    status("starting")
    return run(a.timeout)


if __name__ == "__main__":
    sys.exit(main())

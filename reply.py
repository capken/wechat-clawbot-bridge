#!/usr/bin/env python3
"""Send a plain-text reply and/or media (image / file / video) to the bound WeChat user.

  python3 reply.py "text"                      or     echo "text" | python3 reply.py
  python3 reply.py --image /path/a.png [--caption "text"]
  python3 reply.py --file /path/report.pdf --caption "报告在这里"
  python3 reply.py --image a.png --file b.pdf  # several media, one message each
  python3 reply.py --media PATH                # auto: image/* -> image, video/* -> video, else file
  python3 reply.py --dry-run ...               # print request bodies, no network, no upload

Text (positional / stdin / --caption) is sent first as a text message, then each media item
as its own message (the iLink API takes one item per sendmessage). Markdown is stripped.
>2000 chars is split (max 5 parts). Media size cap 50MB (WECHAT_MEDIA_MAX_BYTES).
The per-user-message quota is ~10 sends (tracked in state/send_quota.json; --force ignores it).
Exit codes:
  0 sent | 1 usage | 2 no credentials/context | 3 API/network error | 4 quota | 5 media error | 14 ERRCODE -14 (rescan)
"""
from __future__ import annotations

import argparse
import json
import sys
import time

import requests

import ilink_common as C
import media as M

log = C.setup_logging("reply", C.LOGS / "reply.log", also_stderr=False)
MAX_PARTS = 5
MAX_MEDIA = 5
QUOTA = 10
QUOTA_FILE = C.STATE / "send_quota.json"
CONTEXT_TTL_S = 24 * 3600


def die(code: int, msg: str) -> int:
    print(msg, file=sys.stderr)
    log.error(msg)
    return code


def build_body(to_user: str, context_token: str, text: str) -> dict:
    return build_item_body(to_user, context_token, {"type": 1, "text_item": {"text": text}})


def build_item_body(to_user: str, context_token: str, item: dict) -> dict:
    return {"msg": {"from_user_id": "", "to_user_id": to_user, "client_id": C.new_client_id(),
                    "message_type": 2, "message_state": 2, "context_token": context_token,
                    "item_list": [item]},
            "base_info": C.BASE_INFO}


def _mask(obj):
    """Copy of a request body with secrets masked, for --dry-run output."""
    o = json.loads(json.dumps(obj))
    if isinstance(o, dict):
        for k in list(o):
            if k in ("context_token", "aeskey", "aes_key", "encrypt_query_param", "bot_token"):
                o[k] = "<redacted>"
            else:
                o[k] = _mask(o[k])
    elif isinstance(o, list):
        o = [_mask(x) for x in o]
    return o


def quota_used(msg_id: str) -> int:
    q = C.read_json(QUOTA_FILE, {}) or {}
    return int(q.get("count", 0)) if q.get("message_id") == msg_id else 0


def quota_add(msg_id: str, n: int) -> None:
    C.write_json(QUOTA_FILE, {"message_id": msg_id, "count": quota_used(msg_id) + n, "at": C.now_iso()}, 0o600)


def post_send(base: str, creds: dict, body: dict, label: str) -> int | None:
    """POST sendmessage. Returns None on success, else an exit code (message already printed)."""
    try:
        r = requests.post(C.api_url(base, "ilink/bot/sendmessage"), headers=C.post_headers(creds["bot_token"]),
                          json=body, timeout=20)
    except requests.RequestException as e:
        return die(3, f"network error sending {label}: {type(e).__name__}")
    if r.status_code >= 400:
        return die(3, f"sendmessage HTTP {r.status_code} ({label})")
    try:
        d = r.json()
    except ValueError:
        return die(3, f"sendmessage returned non-JSON (HTTP {r.status_code})")
    err = C.errcode_of(d)
    if err == C.STALE_TOKEN_ERRCODE:
        C.mark_expired("reply.sendmessage", json.dumps(d)[:200])
        return die(14, "ERRCODE -14: WeChat credentials expired. Rerun login.py and rescan the QR.")
    if err is not None:
        return die(3, f"sendmessage ERRCODE {err}: {d.get('errmsg') or ''}".strip())
    if d == {}:
        print("warning: sendmessage returned {} (community reports this can mean not delivered)", file=sys.stderr)
    log.info("sent %s message_id=%s", label, d.get("message_id"))
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description="Reply to the bound WeChat user (text, image, file, video).")
    ap.add_argument("text", nargs="*")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--raw", action="store_true", help="don't strip markdown")
    ap.add_argument("--caption", help="text sent before the media (same as positional text)")
    ap.add_argument("--image", action="append", default=[], metavar="PATH", help="send as an image (repeatable)")
    ap.add_argument("--file", action="append", default=[], metavar="PATH", help="send as a file attachment (repeatable)")
    ap.add_argument("--video", action="append", default=[], metavar="PATH", help="send as a video (repeatable)")
    ap.add_argument("--media", action="append", default=[], metavar="PATH", help="auto-detect by extension (repeatable)")
    ap.add_argument("--force", action="store_true", help="ignore the 10-sends-per-user-message quota guard")
    a = ap.parse_args()

    media_list: list[tuple[str, str]] = ([("image", p) for p in a.image] + [("file", p) for p in a.file]
                                         + [("video", p) for p in a.video]
                                         + [(M.kind_for_path(p), p) for p in a.media])
    raw = " ".join(a.text) if a.text else ("" if sys.stdin.isatty() or media_list else sys.stdin.read())
    if a.caption:
        raw = (raw + "\n" + a.caption).strip() if raw.strip() else a.caption
    text = raw if a.raw else C.strip_markdown(raw)
    if not text.strip() and not media_list:
        return die(1, "usage: reply.py \"text\" | --image PATH | --file PATH [--caption text]  (nothing to send)")
    if len(media_list) > MAX_MEDIA:
        return die(1, f"too many media items ({len(media_list)} > {MAX_MEDIA}); send fewer per call")

    if C.EXPIRED_FLAG.exists() and not a.dry_run:
        return die(14, "ERRCODE -14: WeChat credentials expired. Rerun login.py and rescan the QR.")
    creds = C.load_credentials()
    if not creds and not a.dry_run:
        return die(2, "no credentials (state/credentials.json); run login.py first")
    ctx = C.read_json(C.CONTEXT, {}) or {}
    creds = creds or {"bot_token": "DRYRUN", "ilink_user_id": ctx.get("from_user_id") or "dry@im.wechat",
                      "baseurl": C.FIXED_BASE_URL}
    bound = creds["ilink_user_id"]
    if ctx.get("context_token"):
        C.register_secret(ctx["context_token"])
    if ctx.get("from_user_id") and ctx["from_user_id"] != bound:
        return die(2, "context.json belongs to a different user than the bound one; refusing")
    token = ctx.get("context_token")
    if not token:
        if not a.dry_run:
            return die(2, "no context_token yet: send any message to the ClawBot chat in WeChat first")
        token = "DRYRUN_CONTEXT"
    age = time.time() - int(ctx.get("saved_at_epoch") or time.time())
    if age > CONTEXT_TTL_S:
        print(f"warning: context_token is {age/3600:.1f}h old (>24h window); delivery may fail. "
              "Ask the user to send a new WeChat message.", file=sys.stderr)

    parts = C.split_text(text) if text.strip() else []
    if len(parts) > MAX_PARTS:
        parts = parts[:MAX_PARTS]
        parts[-1] = parts[-1][: C.MAX_TEXT_CHARS - 20].rstrip() + "\n…(内容过长已截断)"
    if len(parts) > 1:
        parts = [f"({i}/{len(parts)}) {p}" for i, p in enumerate(parts, 1)]

    # validate media up front (before sending anything) so a bad path doesn't leave half a reply
    for kind, path in media_list:
        try:
            M.read_upload_source(path)
        except M.MediaError as e:
            return die(5, f"media error ({kind}): {e}")

    total = len(parts) + len(media_list)
    ctx_mid = str(ctx.get("message_id") or "")
    if not a.dry_run and not a.force:
        used = quota_used(ctx_mid)
        if used + total > QUOTA:
            return die(4, f"quota guard: {used} already sent for this user message, +{total} would exceed ~{QUOTA}. "
                          "Send fewer/combined messages, or wait for the user's next message (use --force to override).")

    base = creds.get("baseurl") or C.FIXED_BASE_URL
    sent = 0
    for i, part in enumerate(parts):
        body = build_body(bound, token, part)
        if a.dry_run:
            print("[dry-run] POST", C.api_url(base, "ilink/bot/sendmessage"), json.dumps(_mask(body), ensure_ascii=False))
            continue
        rc = post_send(base, creds, body, f"text part {i+1}/{len(parts)} chars={len(part)}")
        if rc is not None:
            if sent:
                quota_add(ctx_mid, sent)
            return rc
        sent += 1
        if i + 1 < len(parts) or media_list:
            time.sleep(1.5 if len(parts) > 1 else 0.5)

    for j, (kind, path) in enumerate(media_list):
        name = M.safe_name(path.replace("\\", "/").split("/")[-1], "file")
        if a.dry_run:
            plain = M.read_upload_source(path)
            import secrets as _s
            ak = _s.token_bytes(16)
            req = M.build_upload_request(kind, bound, plain, "<filekey>", ak)
            print("[dry-run] POST", C.api_url(base, "ilink/bot/getuploadurl"), json.dumps(_mask(req), ensure_ascii=False))
            print(f"[dry-run] POST <CDN>/upload  (AES-128-ECB ciphertext, {req['filesize']} bytes; skipped)")
            up = {"download_param": "<encrypt_query_param>", "aeskey": ak.hex(),
                  "size": len(plain), "size_cipher": req["filesize"]}
            body = build_item_body(bound, token, M.build_media_item(kind, up, name))
            print("[dry-run] POST", C.api_url(base, "ilink/bot/sendmessage"), json.dumps(_mask(body), ensure_ascii=False))
            continue
        try:
            up = M.upload_media(kind, path, bound, creds)
        except M.ExpiredSession:
            C.mark_expired("reply.getuploadurl")
            return die(14, "ERRCODE -14: WeChat credentials expired. Rerun login.py and rescan the QR.")
        except M.MediaError as e:
            if sent:
                quota_add(ctx_mid, sent)
            return die(5, f"media upload failed ({kind} {name}): {C.redact(str(e))}")
        body = build_item_body(bound, token, M.build_media_item(kind, up, name))
        rc = post_send(base, creds, body, f"{kind} {name} size={up['size']}")
        if rc is not None:
            if sent:
                quota_add(ctx_mid, sent)
            return rc
        sent += 1
        if j + 1 < len(media_list):
            time.sleep(1)
    if sent:
        quota_add(ctx_mid, sent)
    if not a.dry_run:
        print(f"sent {sent} message(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

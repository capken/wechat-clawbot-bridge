#!/usr/bin/env python3
"""Outbound-only WeChat -> Grok Bot routine bridge.

Long-polls iLink getupdates, keeps only messages from the bound user, stores
the latest context_token, downloads+decrypts inbound media into inbox/, and POSTs the
(batched) messages to the routine webhook.

  python3 bridge.py                       # normal (needs env + credentials)
  python3 bridge.py --dry-run             # poll WeChat but print webhook POSTs instead of sending
  python3 bridge.py --dry-run --fake-updates tests/fake_updates.json   # fully offline
  python3 bridge.py --once                # single poll cycle

Env:
  WECHAT_ROUTINE_WEBHOOK_URL   routine "POST to" URL
  WECHAT_ROUTINE_WEBHOOK_KEY   routine key (bare key; a pasted "Bearer xxx" / "Authorization: Bearer xxx" is tolerated)
  WECHAT_ROUTINE_WEBHOOK_HEADER  header name   (default "Authorization")
  WECHAT_ROUTINE_WEBHOOK_FORMAT  header value  (default "Bearer {key}")
Exit codes: 0 ok/stopped, 2 config error, 14 credentials expired (-14).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import signal
import sys
import threading
import time
from urllib.parse import urlparse

import requests

import ilink_common as C
import media as M

log = C.setup_logging("bridge", C.LOGS / "bridge.log", also_stderr=sys.stderr.isatty())
_stop = False


def _on_signal(signum, _frame):
    global _stop
    _stop = True
    log.info("signal %s received, stopping after current poll", signum)


# ---------------------------------------------------------------- webhook

def normalize_key(raw: str) -> str:
    k = (raw or "").strip()
    k = re.sub(r"^(?i:authorization)\s*:\s*", "", k)
    k = re.sub(r"^(?i:bearer)\s+", "", k)
    return k.strip()


class Webhook:
    def __init__(self, dry_run: bool):
        self.dry_run = dry_run
        self.url = os.environ.get("WECHAT_ROUTINE_WEBHOOK_URL", "").strip()
        self.key = normalize_key(os.environ.get("WECHAT_ROUTINE_WEBHOOK_KEY", ""))
        self.header = os.environ.get("WECHAT_ROUTINE_WEBHOOK_HEADER", "Authorization").strip() or "Authorization"
        self.fmt = os.environ.get("WECHAT_ROUTINE_WEBHOOK_FORMAT", "Bearer {key}")
        C.register_secret(self.url)
        C.register_secret(self.key)
        if not dry_run and (not self.url or not self.key):
            log.error("config error: WECHAT_ROUTINE_WEBHOOK_URL / WECHAT_ROUTINE_WEBHOOK_KEY not set"); print("config error: WECHAT_ROUTINE_WEBHOOK_URL / WECHAT_ROUTINE_WEBHOOK_KEY not set", file=sys.stderr); sys.exit(2)
        if self.url and urlparse(self.url).scheme != "https" and not dry_run:
            print("config error: webhook URL must be https", file=sys.stderr); sys.exit(2)

    def host(self) -> str:
        return urlparse(self.url).netloc or "(unset)"

    def headers(self) -> dict:
        return {"Content-Type": "application/json", self.header: self.fmt.format(key=self.key)}

    def post(self, payload: dict) -> bool:
        if self.dry_run:
            shown = {k: ("<redacted>" if k == self.header else v) for k, v in self.headers().items()}
            print("[dry-run] POST", self.host() if self.url else "<WECHAT_ROUTINE_WEBHOOK_URL>",
                  json.dumps({"headers": shown, "json": payload}, ensure_ascii=False), flush=True)
            return True
        delay = 2
        for attempt in range(1, 7):
            try:
                r = requests.post(self.url, headers=self.headers(), json=payload, timeout=20)
                if 200 <= r.status_code < 300:
                    log.info("webhook ok http=%s message_id=%s", r.status_code, payload.get("message_id"))
                    return True
                log.warning("webhook http=%s attempt=%d message_id=%s", r.status_code, attempt,
                            payload.get("message_id"))
                if r.status_code in (400, 401, 403, 404):
                    if r.status_code == 400 and "rate limit" in (r.text or "").lower():
                        delay = max(delay, 15)  # platform-side rate limit is transient: keep retrying
                    else:
                        break  # wrong key/URL or paused routine: retrying won't help
            except requests.RequestException as e:
                log.warning("webhook network error %s attempt=%d", type(e).__name__, attempt)
            time.sleep(delay)
            delay *= 2
        with open(C.FAILED_WEBHOOKS, "a", encoding="utf-8") as f:
            f.write(json.dumps({"at": C.now_iso(), "payload": payload}, ensure_ascii=False) + "\n")
        os.chmod(C.FAILED_WEBHOOKS, 0o600)
        log.error("webhook gave up message_id=%s (saved to %s)", payload.get("message_id"), C.FAILED_WEBHOOKS.name)
        return False


# ---------------------------------------------------------------- state

def load_cursor() -> str:
    try:
        return C.CURSOR.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return ""


def save_cursor(buf: str) -> None:
    C.write_text(C.CURSOR, buf, 0o600)


def load_seen() -> list:
    return C.read_json(C.SEEN_IDS, []) or []


def save_context(msg: dict) -> None:
    tok = msg.get("context_token")
    if not tok:
        return
    C.register_secret(tok)
    C.write_json(C.CONTEXT, {"from_user_id": msg.get("from_user_id"), "context_token": tok,
                             "message_id": str(msg.get("message_id") or ""),
                             "create_time_ms": msg.get("create_time_ms"),
                             "saved_at": C.now_iso(), "saved_at_epoch": int(time.time())}, 0o600)


_REPLY_PY = str(C.ROOT / "reply.py")
REPLY_HINT = (f"reply with: {sys.executable} {_REPLY_PY} \"<plain text>\"  "
              "(send a file/image back: reply.py --file PATH | --image PATH [--caption \"text\"]; "
              "inbound files are already saved at the absolute paths shown in text)")
BATCH_WINDOW_S = float(os.environ.get("WECHAT_BATCH_WINDOW_S", "4"))
BATCH_MAX_S = float(os.environ.get("WECHAT_BATCH_MAX_S", "20"))


def _iso(ms) -> tuple:
    try:
        ms = int(ms) if ms is not None else None
    except (TypeError, ValueError):
        ms = None
    iso = time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(ms / 1000)) if ms else None
    return iso, ms


def build_payload(msg: dict, text: str, media: list | None = None, media_errors: list | None = None) -> dict:
    iso, ms = _iso(msg.get("create_time_ms"))
    p = {"source": "wechat", "from_user_id": msg.get("from_user_id"), "text": text,
         "message_id": str(msg.get("message_id") or msg.get("seq") or ""),
         "create_time": iso, "create_time_ms": ms, "reply_hint": REPLY_HINT}
    if media:
        p["media"] = media
    if media_errors:
        p["media_errors"] = media_errors
    return p


class Batcher:
    """Merge rapid consecutive messages from the bound user into ONE webhook call.

    Entries are flushed once no new message arrived for `window` seconds (or `max_wait`
    seconds after the first one). A daemon thread does the flushing; flush_now() is
    called on shutdown so nothing waits in memory."""

    def __init__(self, hook, window: float = BATCH_WINDOW_S, max_wait: float = BATCH_MAX_S, start_thread: bool = True):
        self.hook, self.window, self.max_wait = hook, window, max_wait
        self._items: list[dict] = []
        self._first = 0.0
        self._last = 0.0
        self._lock = threading.Lock()
        self._post_lock = threading.Lock()
        self._done = False
        self._thread = None
        if start_thread and window > 0:
            self._thread = threading.Thread(target=self._run, name="batcher", daemon=True)
            self._thread.start()

    def add(self, msg: dict, text: str, media: list, errors: list) -> None:
        with self._lock:
            now = time.monotonic()
            if not self._items:
                self._first = now
            self._last = now
            self._items.append({"msg": msg, "text": text, "media": media, "errors": errors})

    def pending(self) -> int:
        with self._lock:
            return len(self._items)

    def _due(self) -> bool:
        with self._lock:
            if not self._items:
                return False
            now = time.monotonic()
            return now - self._last >= self.window or now - self._first >= self.max_wait

    def _run(self) -> None:
        while not self._done:
            time.sleep(0.25)
            try:
                if self._due():
                    self.flush_now()
            except Exception as e:  # never let the flusher die
                log.error("batch flush error %s", type(e).__name__)

    @staticmethod
    def merge(items: list[dict]) -> dict:
        last, first = items[-1]["msg"], items[0]["msg"]
        text = "\n".join(i["text"] for i in items if i["text"])
        media = [m for i in items for m in i["media"]]
        errors = [e for i in items for e in i["errors"]]
        p = build_payload(last, text, media, errors)
        if len(items) > 1:
            p["create_time"], p["create_time_ms"] = _iso(first.get("create_time_ms"))
            p["batched_messages"] = len(items)
            p["message_ids"] = [str(i["msg"].get("message_id") or i["msg"].get("seq") or "") for i in items]
        return p

    def flush_now(self) -> bool:
        with self._post_lock:
            with self._lock:
                items, self._items = self._items, []
            if not items:
                return False
            payload = self.merge(items)
            log.info("flush batch of %d message(s), media=%d", len(items), len(payload.get("media") or []))
            self.hook.post(payload)
            return True

    def close(self) -> None:
        self._done = True
        self.flush_now()


def handle_messages(msgs: list, bound_user: str, hook: Webhook, seen: list, batcher: "Batcher | None" = None,
                    fetch_media=M.fetch_for_message) -> int:
    """Returns number of messages accepted. With a Batcher they are queued (merged and posted
    later); without one each message is posted immediately. Pure enough to unit test."""
    kept = C.filter_messages(msgs, bound_user)
    dropped = len(msgs or []) - len(kept)
    if dropped:
        log.info("ignored %d message(s) not from bound user / not user DMs", dropped)
    n = 0
    for m in kept:
        mid = str(m.get("message_id") or m.get("seq") or "")
        save_context(m)  # newest token even if we already forwarded it
        if mid and mid in seen:
            continue
        got, errs = ({}, [])
        try:
            got, errs = fetch_media(m, log)
        except Exception as e:  # media must never break text forwarding
            errs = [f"media handling failed: {type(e).__name__}"]
            log.warning("media handling failed message_id=%s: %s", mid, type(e).__name__)
        text = C.extract_text(m, got)
        if not text:
            log.info("message_id=%s had no forwardable content", mid)
            continue
        media_list = [got[i] for i in sorted(got)]
        if batcher is not None:
            batcher.add(m, text, media_list, errs)
        else:
            hook.post(build_payload(m, text, media_list, errs))
        n += 1
        if mid:
            seen.append(mid)
    del seen[:-200]
    return n


# ---------------------------------------------------------------- iLink

class Expired(Exception):
    pass


def get_updates(creds: dict, buf: str, timeout_ms: int) -> dict:
    base = creds.get("baseurl") or C.FIXED_BASE_URL
    try:
        r = requests.post(C.api_url(base, "ilink/bot/getupdates"), headers=C.post_headers(creds["bot_token"]),
                          json={"get_updates_buf": buf, "base_info": C.BASE_INFO}, timeout=timeout_ms / 1000 + 5)
    except requests.Timeout:
        return {"ret": 0, "msgs": [], "get_updates_buf": buf}
    if r.status_code >= 400:
        raise requests.HTTPError(f"getupdates http {r.status_code}")
    d = r.json()
    if C.errcode_of(d) is not None:
        log.warning("getupdates http=%s raw=%s", r.status_code, _redact_body(getattr(r, "text", None) or json.dumps(d, ensure_ascii=False)))
    if C.errcode_of(d) == C.STALE_TOKEN_ERRCODE:
        raise Expired(f"getupdates ret={d.get('ret')} errcode={d.get('errcode')} errmsg={d.get('errmsg')}")
    return d


def _redact_body(text: str) -> str:
    """Raw response for debugging, with tokens/cursors masked."""
    t = C.redact(text or "")
    t = re.sub(r'("(?:get_updates_buf|context_token|typing_ticket|bot_token|aes_key|aeskey)"\s*:\s*")[^"]*', r"\1<redacted>", t)
    return t[:300]


def notify(creds: dict, which: str) -> None:
    try:
        base = creds.get("baseurl") or C.FIXED_BASE_URL
        r = requests.post(C.api_url(base, f"ilink/bot/msg/{which}"), headers=C.post_headers(creds["bot_token"]),
                          json={"base_info": C.BASE_INFO}, timeout=10)
        try:
            d = r.json()
        except ValueError:
            d = {}
        if r.status_code >= 400 or C.errcode_of(d) is not None:
            log.warning("%s http=%s raw=%s", which, r.status_code, _redact_body(getattr(r, "text", None) or json.dumps(d, ensure_ascii=False)))
        else:
            log.info("%s ok", which)
    except Exception as e:
        log.warning("%s failed (ignored): %s", which, type(e).__name__)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="print webhook POSTs instead of sending")
    ap.add_argument("--fake-updates", help="JSON file with a getupdates response (or list of them); no network")
    ap.add_argument("--bound-user", help="override bound user (testing with --fake-updates)")
    ap.add_argument("--once", action="store_true")
    a = ap.parse_args()
    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    hook = Webhook(dry_run=a.dry_run)

    if a.fake_updates:
        data = json.load(open(a.fake_updates, encoding="utf-8"))
        responses = data if isinstance(data, list) else [data]
        creds = C.load_credentials() or {}
        bound = a.bound_user or creds.get("ilink_user_id")
        if not bound:
            print("need --bound-user or credentials", file=sys.stderr)
            return 2
        seen: list = []
        # offline mode must never touch the live context_token / inbox of the real bridge
        import tempfile
        _tmp = tempfile.mkdtemp(prefix="wechat-fake-")
        C.CONTEXT = type(C.CONTEXT)(_tmp) / "context.json"
        batcher = Batcher(hook, window=0, start_thread=False)
        for resp in responses:
            if C.errcode_of(resp) == C.STALE_TOKEN_ERRCODE:
                print("ERRCODE -14 (fake) -> would mark expired and exit 14", file=sys.stderr)
                return 14
            handle_messages(resp.get("msgs") or [], bound, hook, seen, batcher)
            batcher.flush_now()
        return 0

    if C.EXPIRED_FLAG.exists():
        log.error("state/expired exists: credentials expired (-14). Rerun login.py and rescan the QR.")
        return 14
    creds = C.load_credentials()
    if not creds:
        log.error("no credentials; run login.py first")
        return 2
    bound = creds["ilink_user_id"]
    buf = load_cursor()
    seen = load_seen()
    timeout_ms = 35000
    failures = 0
    log.info("bridge start: base=%s bot=%s webhook_host=%s dry_run=%s cursor=%s",
             urlparse(creds.get("baseurl") or C.FIXED_BASE_URL).netloc, creds.get("ilink_bot_id"),
             hook.host(), a.dry_run, "yes" if buf else "empty")
    notify(creds, "notifystart")
    try:
        removed = M.cleanup_inbox()
        if removed:
            log.info("inbox cleanup: removed %d day folder(s) older than %d days", removed, M.INBOX_KEEP_DAYS)
    except Exception as e:
        log.warning("inbox cleanup failed: %s", type(e).__name__)
    batcher = Batcher(hook, window=0 if a.once else BATCH_WINDOW_S, start_thread=not a.once)
    try:
        while not _stop:
            try:
                d = get_updates(creds, buf, timeout_ms)
            except Expired as e:
                C.mark_expired("bridge.getupdates", str(e))
                log.error("ERRCODE -14: WeChat credentials expired. Polling stopped. "
                          "Rerun `python3 login.py` and rescan the QR, then start.sh.")
                return 14
            except (requests.RequestException, ValueError) as e:
                failures += 1
                wait = 2 if failures < 3 else min(30 * 2 ** (failures - 3), 300)
                log.warning("getupdates error %s (%s); failure #%d, retry in %ss",
                            type(e).__name__, C.redact(str(e))[:120], failures, wait)
                time.sleep(wait)
                continue
            err = C.errcode_of(d)
            if err is not None:
                failures += 1
                wait = 2 if failures < 3 else min(30 * 2 ** (failures - 3), 300)
                log.warning("getupdates ret=%s errcode=%s errmsg=%s; retry in %ss",
                            d.get("ret"), d.get("errcode"), d.get("errmsg"), wait)
                time.sleep(wait)
                continue
            failures = 0
            if d.get("longpolling_timeout_ms"):
                try:
                    timeout_ms = max(5000, min(int(d["longpolling_timeout_ms"]), 120000))
                except (TypeError, ValueError):
                    pass
            msgs = d.get("msgs") or []
            if msgs:
                handle_messages(msgs, bound, hook, seen, batcher)
                C.write_json(C.SEEN_IDS, seen, 0o600)
                if a.once:
                    batcher.flush_now()
            nb = d.get("get_updates_buf")
            if nb and nb != buf:
                buf = nb
                save_cursor(buf)
            if a.once:
                break
    finally:
        try:
            batcher.close()
        except Exception as e:
            log.error("final batch flush failed: %s", type(e).__name__)
        notify(creds, "notifystop")
        log.info("bridge stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())

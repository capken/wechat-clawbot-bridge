"""Shared helpers for the outbound-only WeChat iLink bridge.

Needs: requests, cryptography, qrcode[pil] (see requirements.txt / setup.sh). Every network call is an outbound
request; nothing here listens on a port.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import re
import secrets
import tempfile
import time
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(os.environ.get("WECHAT_BRIDGE_ROOT", Path(__file__).resolve().parent))
STATE = ROOT / "state"
LOGS = ROOT / "logs"
CREDENTIALS = STATE / "credentials.json"
CURSOR = STATE / "cursor"
CONTEXT = STATE / "context.json"
EXPIRED_FLAG = STATE / "expired"
SEEN_IDS = STATE / "seen_ids.json"
FAILED_WEBHOOKS = STATE / "failed_webhooks.jsonl"
LOGIN_STATUS = ROOT / "login_status.json"
QR_PNG = ROOT / "qr.png"
VERIFYCODE_FILE = ROOT / "verifycode.txt"

FIXED_BASE_URL = "https://ilinkai.weixin.qq.com"
PLUGIN_VERSION = "2.4.9"  # mimic official @tencent-weixin/openclaw-weixin
ILINK_APP_ID = "bot"
ILINK_APP_CLIENT_VERSION = (2 << 16) | (4 << 8) | 9  # 132105
BASE_INFO = {"channel_version": PLUGIN_VERSION, "bot_agent": "GrokBotBridge/0.1"}
STALE_TOKEN_ERRCODE = -14
MAX_TEXT_CHARS = 2000  # community-conservative per-message cap

# ---------------------------------------------------------------- secrets / logging

_SECRETS: set[str] = set()


def register_secret(value: str | None) -> None:
    """Remember a value that must never appear in logs."""
    if value and len(value) >= 6:
        _SECRETS.add(value)


def redact(text: str) -> str:
    for s in sorted(_SECRETS, key=len, reverse=True):
        text = text.replace(s, "***")
    # belt and braces: hide any bearer token / key-ish query params
    text = re.sub(r"(Bearer\s+)[A-Za-z0-9._\-:+/=@]+", r"\1***", text)
    text = re.sub(r"(?i)((?:token|key|secret)=)[^&\s\"']+", r"\1***", text)
    return text


class _RedactFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact(record.getMessage())
        record.args = ()
        return True


def setup_logging(name: str, logfile: Path, also_stderr: bool = True) -> logging.Logger:
    LOGS.mkdir(parents=True, exist_ok=True)
    log = logging.getLogger(name)
    log.setLevel(logging.INFO)
    log.handlers.clear()
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S%z")
    fh = logging.FileHandler(logfile, encoding="utf-8")
    fh.setFormatter(fmt)
    fh.addFilter(_RedactFilter())
    log.addHandler(fh)
    if also_stderr:
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        sh.addFilter(_RedactFilter())
        log.addHandler(sh)
    log.propagate = False
    return log


# ---------------------------------------------------------------- file helpers

def write_json(path: Path, data: Any, mode: int | None = None) -> None:
    """Atomic JSON write; mode e.g. 0o600 applied before the rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.chmod(tmp, mode if mode is not None else 0o644)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def write_text(path: Path, text: str, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def load_credentials() -> dict | None:
    c = read_json(CREDENTIALS)
    if not c or not c.get("bot_token") or not c.get("ilink_user_id"):
        return None
    register_secret(c.get("bot_token"))
    return c


def mark_expired(where: str, detail: str = "") -> None:
    """-14: flag it, clear cursor + context tokens (they belong to the dead session)."""
    write_json(EXPIRED_FLAG, {"errcode": STALE_TOKEN_ERRCODE, "where": where,
                              "detail": redact(detail)[:300], "at": now_iso()}, 0o600)
    for p in (CURSOR, CONTEXT):
        try:
            p.unlink()
        except FileNotFoundError:
            pass


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


# ---------------------------------------------------------------- iLink HTTP

def random_wechat_uin() -> str:
    return base64.b64encode(str(secrets.randbits(32)).encode()).decode()


def common_headers() -> dict:
    h = {"iLink-App-Id": ILINK_APP_ID, "iLink-App-ClientVersion": str(ILINK_APP_CLIENT_VERSION)}
    tag = os.environ.get("WECHAT_SKROUTETAG")
    if tag:
        h["SKRouteTag"] = tag
    return h


def post_headers(token: str | None = None) -> dict:
    h = {"Content-Type": "application/json", "AuthorizationType": "ilink_bot_token",
         "X-WECHAT-UIN": random_wechat_uin(), **common_headers()}
    if token and token.strip():
        h["Authorization"] = f"Bearer {token.strip()}"
    return h


def api_url(base: str, endpoint: str) -> str:
    return base.rstrip("/") + "/" + endpoint.lstrip("/")


def errcode_of(resp: dict) -> int | None:
    """Return a non-zero ret/errcode, or None on success."""
    for k in ("ret", "errcode"):
        v = resp.get(k)
        if v not in (None, 0, "0"):
            try:
                return int(v)
            except (TypeError, ValueError):
                return -1
    return None


def new_client_id() -> str:
    return f"grokbot:{int(time.time() * 1000)}-{secrets.token_hex(4)}"


# ---------------------------------------------------------------- message parsing

ITEM_TEXT, ITEM_IMAGE, ITEM_VOICE, ITEM_FILE, ITEM_VIDEO = 1, 2, 3, 4, 5
MSG_TYPE_USER = 1


def extract_text(msg: dict, media: dict | None = None) -> str:
    """Join text items; voice -> transcript; media -> short placeholder.

    `media` maps item index -> info dict (kind/path/size/filename/mime) for items already
    downloaded; those get a line with the saved path instead of the bare placeholder."""
    import media as _media  # local import: media imports this module
    parts: list[str] = []
    for idx, item in enumerate(msg.get("item_list") or []):
        t = item.get("type")
        if media and idx in media:
            vt = (item.get("voice_item") or {}).get("text") if t == ITEM_VOICE else None
            parts.append(_media.media_line(media[idx], vt))
            continue
        if t == ITEM_TEXT:
            txt = (item.get("text_item") or {}).get("text") or ""
            ref = item.get("ref_msg") or {}
            if ref:
                quoted = (ref.get("title") or "")
                ri = ref.get("message_item") or {}
                quoted = quoted or ((ri.get("text_item") or {}).get("text") or "")
                if quoted:
                    txt = f"[引用: {quoted}]\n{txt}"
            if txt:
                parts.append(txt)
        elif t == ITEM_VOICE:
            vt = (item.get("voice_item") or {}).get("text")
            parts.append(f"[语音] {vt}" if vt else "[语音，无转写]")
        elif t == ITEM_IMAGE:
            parts.append("[图片]")
        elif t == ITEM_FILE:
            name = (item.get("file_item") or {}).get("file_name")
            parts.append(f"[文件] {name}" if name else "[文件]")
        elif t == ITEM_VIDEO:
            parts.append("[视频]")
    return "\n".join(p for p in parts if p).strip()


def filter_messages(msgs: Iterable[dict], bound_user: str) -> list[dict]:
    """Keep only USER messages from the single bound WeChat user, DMs only."""
    out = []
    for m in msgs or []:
        if not isinstance(m, dict):
            continue
        if not bound_user or m.get("from_user_id") != bound_user:
            continue
        mt = m.get("message_type")
        if mt not in (None, MSG_TYPE_USER):
            continue
        if m.get("group_id"):
            continue
        out.append(m)
    return out


# ---------------------------------------------------------------- markdown -> plain text

def strip_markdown(text: str) -> str:
    s = text.replace("\r\n", "\n")
    # fenced code blocks: keep content, drop fences
    s = re.sub(r"```[^\n]*\n(.*?)```", lambda m: m.group(1).rstrip("\n"), s, flags=re.S)
    s = s.replace("```", "")
    # images ![alt](url) -> alt url ; links [label](url) -> label url
    s = re.sub(r"!\[([^\]]*)\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)", lambda m: f"{m.group(1)} {m.group(2)}".strip(), s)
    s = re.sub(r"\[([^\]]+)\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)",
               lambda m: m.group(2) if m.group(1).strip() == m.group(2) else f"{m.group(1)} {m.group(2)}", s)
    # autolinks <https://...>
    s = re.sub(r"<(https?://[^>\s]+)>", r"\1", s)
    lines_out = []
    for line in s.split("\n"):
        st = line.strip()
        # table separator rows |---|:--:|
        if re.fullmatch(r"\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?", st) and "-" in st:
            continue
        if st.startswith("|") or (st.count("|") >= 2 and re.search(r"\S\s*\|\s*\S", st)):
            cells = [c.strip() for c in st.strip("|").split("|")]
            line = "  ".join(c for c in cells if c)
        # headings
        line = re.sub(r"^\s{0,3}#{1,6}\s+", "", line)
        # blockquotes
        line = re.sub(r"^\s{0,3}>\s?", "", line)
        # horizontal rules
        if re.fullmatch(r"\s*([-*_])(\s*\1){2,}\s*", line):
            line = ""
        # bullets "* " / "+ " -> "- "  (keep '-' bullets, they read fine)
        line = re.sub(r"^(\s*)[*+]\s+", r"\1- ", line)
        # task boxes
        line = re.sub(r"^(\s*-\s+)\[( |x|X)\]\s+", lambda m: m.group(1) + ("✓ " if m.group(2) != " " else ""), line)
        lines_out.append(line)
    s = "\n".join(lines_out)
    # emphasis / strike / inline code
    s = re.sub(r"\*\*(.+?)\*\*", r"\1", s, flags=re.S)
    s = re.sub(r"__(.+?)__", r"\1", s, flags=re.S)
    s = re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])", r"\1", s)
    s = re.sub(r"(?<![\w_])_(?!\s)(.+?)(?<!\s)_(?![\w_])", r"\1", s)
    s = re.sub(r"~~(.+?)~~", r"\1", s, flags=re.S)
    s = re.sub(r"`([^`\n]+)`", r"\1", s)
    s = s.replace("`", "")
    # html line breaks / simple tags
    s = re.sub(r"<br\s*/?>", "\n", s, flags=re.I)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def split_text(text: str, limit: int = MAX_TEXT_CHARS) -> list[str]:
    """Prefer one message; otherwise split on paragraph, then line, then hard cut."""
    text = text.strip()
    if len(text) <= limit:
        return [text] if text else []
    chunks: list[str] = []
    rest = text
    while len(rest) > limit:
        window = rest[:limit]
        cut = window.rfind("\n\n")
        if cut < limit * 0.5:
            cut = window.rfind("\n")
        if cut < limit * 0.5:
            cut = max(window.rfind("。"), window.rfind(". "), window.rfind("！"), window.rfind("？"))
            cut = cut + 1 if cut >= limit * 0.5 else -1
        if cut <= 0:
            cut = limit
        chunks.append(rest[:cut].strip())
        rest = rest[cut:].strip()
    if rest:
        chunks.append(rest)
    return [c for c in chunks if c]

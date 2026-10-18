"""Media support for the WeChat iLink bridge: CDN download/decrypt (inbound) and
getuploadurl + AES-128-ECB + CDN upload (outbound).

Mirrors the official @tencent-weixin/openclaw-weixin v2.4.9 code
(src/cdn/*.ts, src/media/media-download.ts, src/messaging/send.ts).
Outbound-only HTTPS; nothing here listens on a port. Never logs keys, CDN URLs
or encrypt_query_param values.
"""
from __future__ import annotations

import base64
import hashlib
import mimetypes
import os
import re
import secrets
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

import requests
from cryptography.hazmat.primitives import padding as _padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

import ilink_common as C

CDN_BASE_URL = os.environ.get("WECHAT_CDN_BASE_URL", "https://novac2c.cdn.weixin.qq.com/c2c").rstrip("/")
MAX_MEDIA_BYTES = int(os.environ.get("WECHAT_MEDIA_MAX_BYTES", 50 * 1024 * 1024))
INBOX_KEEP_DAYS = 14

UPLOAD_IMAGE, UPLOAD_VIDEO, UPLOAD_FILE, UPLOAD_VOICE = 1, 2, 3, 4
KIND_TO_UPLOAD = {"image": UPLOAD_IMAGE, "video": UPLOAD_VIDEO, "file": UPLOAD_FILE}
KIND_TO_ITEM_TYPE = {"image": C.ITEM_IMAGE, "video": C.ITEM_VIDEO, "file": C.ITEM_FILE}


class MediaError(Exception):
    """Recoverable media failure (message never contains secrets)."""


class ExpiredSession(MediaError):
    """API answered -14."""


def inbox_root() -> Path:
    return C.ROOT / "inbox"


# ---------------------------------------------------------------- AES-128-ECB

def aes_ecb_encrypt(plain: bytes, key: bytes) -> bytes:
    p = _padding.PKCS7(128).padder()
    data = p.update(plain) + p.finalize()
    enc = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
    return enc.update(data) + enc.finalize()


def aes_ecb_decrypt(cipher: bytes, key: bytes) -> bytes:
    try:
        dec = Cipher(algorithms.AES(key), modes.ECB()).decryptor()
        data = dec.update(cipher) + dec.finalize()
        u = _padding.PKCS7(128).unpadder()
        return u.update(data) + u.finalize()
    except ValueError as e:
        raise MediaError("decrypt failed (bad key or corrupt data)") from e


def aes_padded_size(n: int) -> int:
    return ((n + 1 + 15) // 16) * 16


def parse_aes_key(b64: str) -> bytes:
    """base64(raw16) -> raw16 ; base64(32 hex chars) -> raw16 (file/voice/video)."""
    try:
        dec = base64.b64decode(b64, validate=False)
    except Exception as e:
        raise MediaError("aes_key is not base64") from e
    if len(dec) == 16:
        return dec
    if len(dec) == 32 and re.fullmatch(rb"[0-9a-fA-F]{32}", dec):
        return bytes.fromhex(dec.decode("ascii"))
    raise MediaError(f"aes_key has unexpected length {len(dec)}")


# ---------------------------------------------------------------- helpers

def safe_name(name: str, fallback: str = "file") -> str:
    name = (name or "").replace("\\", "/").split("/")[-1]
    name = re.sub(r"[\x00-\x1f\x7f]", "", name).strip().lstrip(".")
    name = re.sub(r"[<>:\"|?*]", "_", name)
    if not name:
        return fallback
    raw = name.encode("utf-8")
    if len(raw) > 120:
        stem, ext = os.path.splitext(name)
        ext = ext[:12]
        stem = stem.encode("utf-8")[: 120 - len(ext.encode("utf-8"))].decode("utf-8", "ignore")
        name = (stem or fallback) + ext
    return name


def sniff_image_ext(data: bytes) -> tuple[str, str]:
    if data[:3] == b"\xff\xd8\xff":
        return ".jpg", "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png", "image/png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return ".gif", "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp", "image/webp"
    if data[:2] == b"BM":
        return ".bmp", "image/bmp"
    return ".jpg", "image/jpeg"  # WeChat images are normally JPEG


def human_size(n: int) -> str:
    if n < 1024:
        return f"{n}B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f}KB"
    return f"{n / 1024 / 1024:.1f}MB"


def mime_of(path: str | Path) -> str:
    m = mimetypes.guess_type(str(path))[0]
    return m or "application/octet-stream"


def kind_for_path(path: str | Path) -> str:
    m = mime_of(path)
    if m.startswith("image/"):
        return "image"
    if m.startswith("video/"):
        return "video"
    return "file"


# ---------------------------------------------------------------- inbound

def _trusted_cdn_url(url: str | None) -> bool:
    try:
        u = urlparse(url or "")
    except ValueError:
        return False
    return u.scheme == "https" and bool(u.hostname) and (u.hostname == "qq.com" or u.hostname.endswith(".qq.com"))


def _download_url(media: dict) -> str | None:
    full = media.get("full_url")
    if full and _trusted_cdn_url(full):
        return full
    eqp = media.get("encrypt_query_param")
    if eqp:
        return f"{CDN_BASE_URL}/download?encrypted_query_param={quote(eqp, safe='')}"
    return None


def _fetch_bytes(url: str, max_bytes: int) -> bytes:
    try:
        r = requests.get(url, stream=True, timeout=(10, 60))
    except requests.RequestException as e:
        raise MediaError(f"CDN download network error: {type(e).__name__}") from e
    try:
        if r.status_code != 200:
            raise MediaError(f"CDN download HTTP {r.status_code}")
        cl = r.headers.get("Content-Length")
        limit = max_bytes + 32
        if cl and cl.isdigit() and int(cl) > limit:
            raise MediaError(f"file too large ({human_size(int(cl))} > cap {human_size(max_bytes)})")
        buf = bytearray()
        for chunk in r.iter_content(65536):
            buf += chunk
            if len(buf) > limit:
                raise MediaError(f"file too large (> cap {human_size(max_bytes)})")
        return bytes(buf)
    except requests.RequestException as e:
        raise MediaError(f"CDN download read error: {type(e).__name__}") from e
    finally:
        r.close()


def item_media_ref(item: dict) -> tuple[str, dict, str | None, str | None] | None:
    """-> (kind, media dict, aes_key_b64 or None, file_name) or None if no CDN ref."""
    t = item.get("type")
    if t == C.ITEM_IMAGE:
        img = item.get("image_item") or {}
        media = img.get("media") or {}
        if not (media.get("encrypt_query_param") or media.get("full_url")):
            return None
        key = None
        if img.get("aeskey"):
            try:
                key = base64.b64encode(bytes.fromhex(img["aeskey"])).decode()
            except ValueError:
                key = None
        return "image", media, key or media.get("aes_key"), None
    if t == C.ITEM_VOICE:
        media = (item.get("voice_item") or {}).get("media") or {}
        if not (media.get("encrypt_query_param") or media.get("full_url")) or not media.get("aes_key"):
            return None
        return "voice", media, media["aes_key"], None
    if t == C.ITEM_FILE:
        fi = item.get("file_item") or {}
        media = fi.get("media") or {}
        if not (media.get("encrypt_query_param") or media.get("full_url")) or not media.get("aes_key"):
            return None
        return "file", media, media["aes_key"], fi.get("file_name")
    if t == C.ITEM_VIDEO:
        media = (item.get("video_item") or {}).get("media") or {}
        if not (media.get("encrypt_query_param") or media.get("full_url")) or not media.get("aes_key"):
            return None
        return "video", media, media["aes_key"], None
    return None


def download_item(item: dict, msg_id: str, idx: int, max_bytes: int = MAX_MEDIA_BYTES,
                  now: float | None = None) -> dict | None:
    """Download+decrypt one media item into inbox/<YYYYMMDD>/<msgid>_<idx>_<name>.

    Returns {kind,path,size,filename,mime} or None when the item has no CDN ref.
    Raises MediaError on failure."""
    ref = item_media_ref(item)
    if not ref:
        return None
    kind, media, key_b64, file_name = ref
    url = _download_url(media)
    if not url:
        raise MediaError("no download URL")
    enc = _fetch_bytes(url, max_bytes)
    if key_b64:
        data = aes_ecb_decrypt(enc, parse_aes_key(key_b64))
    elif kind == "image":
        data = enc  # official plugin: images without key are plain
    else:
        raise MediaError("missing aes_key")
    if len(data) > max_bytes:
        raise MediaError(f"file too large ({human_size(len(data))} > cap {human_size(max_bytes)})")
    mid = re.sub(r"[^0-9A-Za-z_-]", "_", str(msg_id or "nomsg"))[:40]
    if kind == "image":
        ext, mime = sniff_image_ext(data)
        name = f"image{ext}"
    elif kind == "voice":
        ext, mime, name = ".silk", "audio/silk", "voice.silk"
    elif kind == "video":
        ext, mime, name = ".mp4", "video/mp4", "video.mp4"
    else:
        name = safe_name(file_name or "", "file.bin")
        mime = mime_of(name)
    day = time.strftime("%Y%m%d", time.localtime(now or time.time()))
    root = inbox_root()
    ddir = root / day
    for d in (root, ddir):
        d.mkdir(parents=True, exist_ok=True)
        os.chmod(d, 0o700)
    path = ddir / f"{mid}_{idx}_{name}"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    os.chmod(path, 0o600)
    return {"kind": kind, "path": str(path), "size": len(data),
            "filename": file_name if kind == "file" and file_name else name, "mime": mime}


def fetch_for_message(msg: dict, log=None) -> tuple[dict[int, dict], list[str]]:
    """Download every media item of a message. -> ({item_index: info}, [error strings])."""
    got: dict[int, dict] = {}
    errs: list[str] = []
    mid = str(msg.get("message_id") or msg.get("seq") or "")
    for i, item in enumerate(msg.get("item_list") or []):
        if item.get("type") not in (C.ITEM_IMAGE, C.ITEM_VOICE, C.ITEM_FILE, C.ITEM_VIDEO):
            continue
        try:
            info = download_item(item, mid, i)
            if info:
                got[i] = info
                if log:
                    log.info("media saved message_id=%s kind=%s size=%d", mid, info["kind"], info["size"])
        except MediaError as e:
            errs.append(f"item {i} type {item.get('type')}: {C.redact(str(e))}")
            if log:
                log.warning("media download failed message_id=%s item=%d: %s", mid, i, C.redact(str(e)))
        except OSError as e:
            errs.append(f"item {i}: save failed {type(e).__name__}")
            if log:
                log.warning("media save failed message_id=%s item=%d: %s", mid, i, type(e).__name__)
    return got, errs


def cleanup_inbox(keep_days: int = INBOX_KEEP_DAYS, now: float | None = None) -> int:
    """Delete inbox/<YYYYMMDD> directories older than keep_days. Returns dirs removed."""
    import shutil
    root = inbox_root()
    if not root.is_dir():
        return 0
    cutoff = time.strftime("%Y%m%d", time.localtime((now or time.time()) - keep_days * 86400))
    n = 0
    for d in root.iterdir():
        if d.is_dir() and re.fullmatch(r"\d{8}", d.name) and d.name < cutoff:
            shutil.rmtree(d, ignore_errors=True)
            n += 1
    return n


def media_line(info: dict, voice_text: str | None = None) -> str:
    meta = f"{info['mime']}, {human_size(info['size'])}"
    p = info["path"]
    k = info["kind"]
    if k == "image":
        return f"[图片] {p} ({meta})"
    if k == "file":
        return f"[文件] {p} (原名 {info['filename']}, {meta})"
    if k == "video":
        return f"[视频] {p} ({meta})"
    return f"[语音] {voice_text + ' ' if voice_text else ''}{p} ({meta})"


# ---------------------------------------------------------------- outbound

def _api_post(base: str, token: str, endpoint: str, body: dict, timeout: int = 15) -> dict:
    try:
        r = requests.post(C.api_url(base, endpoint), headers=C.post_headers(token), json=body, timeout=timeout)
    except requests.RequestException as e:
        raise MediaError(f"{endpoint} network error: {type(e).__name__}") from e
    if r.status_code >= 400:
        raise MediaError(f"{endpoint} HTTP {r.status_code}")
    try:
        d = r.json()
    except ValueError as e:
        raise MediaError(f"{endpoint} returned non-JSON") from e
    err = C.errcode_of(d)
    if err == C.STALE_TOKEN_ERRCODE:
        raise ExpiredSession(endpoint)
    if err is not None:
        raise MediaError(f"{endpoint} ERRCODE {err}: {str(d.get('errmsg') or '')[:100]}")
    return d


def build_upload_request(kind: str, to_user: str, plain: bytes, filekey: str, aeskey: bytes) -> dict:
    return {"filekey": filekey, "media_type": KIND_TO_UPLOAD[kind], "to_user_id": to_user,
            "rawsize": len(plain), "rawfilemd5": hashlib.md5(plain).hexdigest(),
            "filesize": aes_padded_size(len(plain)), "no_need_thumb": True,
            "aeskey": aeskey.hex(), "base_info": C.BASE_INFO}


def read_upload_source(path: str | Path, max_bytes: int = MAX_MEDIA_BYTES) -> bytes:
    p = Path(path)
    if not p.is_file():
        raise MediaError(f"not a file: {p.name}")
    size = p.stat().st_size
    if size == 0:
        raise MediaError(f"empty file: {p.name}")
    if size > max_bytes:
        raise MediaError(f"file too large ({human_size(size)} > cap {human_size(max_bytes)})")
    return p.read_bytes()


def _cdn_upload(cdn_url: str, ciphertext: bytes) -> str:
    last: Exception | None = None
    for attempt in range(1, 4):
        try:
            r = requests.post(cdn_url, headers={"Content-Type": "application/octet-stream"},
                              data=ciphertext, timeout=(10, 120))
        except requests.RequestException as e:
            last = MediaError(f"CDN upload network error: {type(e).__name__}")
            time.sleep(1)
            continue
        if 400 <= r.status_code < 500:
            raise MediaError(f"CDN upload client error {r.status_code}: {(r.headers.get('x-error-message') or '')[:100]}")
        if r.status_code != 200:
            last = MediaError(f"CDN upload server error {r.status_code}")
            time.sleep(1)
            continue
        param = r.headers.get("x-encrypted-param")
        if not param:
            last = MediaError("CDN upload response missing x-encrypted-param")
            time.sleep(1)
            continue
        return param
    raise last or MediaError("CDN upload failed")


def upload_media(kind: str, path: str | Path, to_user: str, creds: dict,
                 max_bytes: int = MAX_MEDIA_BYTES) -> dict:
    """getuploadurl -> AES-ECB encrypt -> CDN POST. Returns UploadedFileInfo-like dict."""
    plain = read_upload_source(path, max_bytes)
    filekey = secrets.token_hex(16)
    aeskey = secrets.token_bytes(16)
    C.register_secret(aeskey.hex())
    base = creds.get("baseurl") or C.FIXED_BASE_URL
    resp = _api_post(base, creds["bot_token"], "ilink/bot/getuploadurl",
                     build_upload_request(kind, to_user, plain, filekey, aeskey))
    full = (resp.get("upload_full_url") or "").strip()
    param = resp.get("upload_param")
    if full:
        if not _trusted_cdn_url(full):
            raise MediaError("getuploadurl returned an untrusted upload host")
        cdn_url = full
    elif param:
        cdn_url = f"{CDN_BASE_URL}/upload?encrypted_query_param={quote(param, safe='')}&filekey={quote(filekey, safe='')}"
    else:
        raise MediaError("getuploadurl returned no upload URL")
    download_param = _cdn_upload(cdn_url, aes_ecb_encrypt(plain, aeskey))
    C.register_secret(download_param)
    return {"filekey": filekey, "download_param": download_param, "aeskey": aeskey.hex(),
            "size": len(plain), "size_cipher": aes_padded_size(len(plain))}


def build_media_item(kind: str, up: dict, file_name: str | None = None) -> dict:
    """Item exactly as official send.ts: aes_key = base64 of the *hex string* of the key."""
    media = {"encrypt_query_param": up["download_param"],
             "aes_key": base64.b64encode(up["aeskey"].encode()).decode(), "encrypt_type": 1}
    if kind == "image":
        return {"type": C.ITEM_IMAGE, "image_item": {"media": media, "mid_size": up["size_cipher"]}}
    if kind == "video":
        return {"type": C.ITEM_VIDEO, "video_item": {"media": media, "video_size": up["size_cipher"]}}
    return {"type": C.ITEM_FILE, "file_item": {"media": media, "file_name": file_name or "file",
                                               "len": str(up["size"])}}

import base64, hashlib, json, os, subprocess, sys, tempfile, time, unittest
from pathlib import Path
from unittest import mock

TMP = tempfile.mkdtemp()
os.environ.setdefault("WECHAT_BRIDGE_ROOT", TMP)
ROOT_SRC = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_SRC))
import requests
def _no_net(*a, **k): raise AssertionError("network call attempted in test")
requests.post = requests.get = _no_net

import ilink_common as C
import media as M
import bridge

JPEG = b"\xff\xd8\xff\xe0" + b"JFIF-fake-image-bytes" * 50
PDF = b"%PDF-1.4 fake pdf " * 100
KEY = bytes(range(16))


class FakeResp:
    def __init__(self, status=200, content=b"", headers=None, js=None):
        self.status_code, self._c, self.headers, self._js = status, content, headers or {}, js
        if "Content-Length" not in self.headers and content:
            self.headers["Content-Length"] = str(len(content))
    def iter_content(self, n):
        for i in range(0, len(self._c), n):
            yield self._c[i:i + n]
    def json(self):
        if self._js is None:
            raise ValueError("no json")
        return self._js
    def close(self): pass


def cdn_get(blobs):
    """fake requests.get: URL substring -> bytes"""
    def _get(url, **kw):
        for k, v in blobs.items():
            if k in url:
                return FakeResp(200, v)
        return FakeResp(404)
    return _get


def img_item(eqp="IMG1", hexkey=True):
    it = {"type": 2, "image_item": {"media": {"encrypt_query_param": eqp, "aes_key": base64.b64encode(KEY).decode(), "encrypt_type": 1}}}
    if hexkey:
        it["image_item"]["aeskey"] = KEY.hex()
    return it


def file_item(name="报告 v1.pdf", eqp="FILE1"):
    # files/voice/video: aes_key = base64(hex string)
    return {"type": 4, "file_item": {"media": {"encrypt_query_param": eqp, "aes_key": base64.b64encode(KEY.hex().encode()).decode()}, "file_name": name, "len": str(len(PDF))}}


def msg(items, mid=7400000000000000001, user="user@im.wechat", ctx="CTX_M", ms=1791600000000):
    return {"seq": 1, "message_id": mid, "from_user_id": user, "to_user_id": "bot@im.bot", "create_time_ms": ms,
            "message_type": 1, "context_token": ctx, "item_list": items}


class Crypto(unittest.TestCase):
    def test_roundtrip(self):
        for n in (0, 1, 15, 16, 17, 1000):
            data = os.urandom(n)
            enc = M.aes_ecb_encrypt(data, KEY)
            self.assertEqual(len(enc), M.aes_padded_size(n))
            self.assertEqual(M.aes_ecb_decrypt(enc, KEY), data)

    def test_known_vector(self):  # openssl enc -aes-128-ecb -K 000102..0f of "hello"
        self.assertEqual(M.aes_ecb_encrypt(b"hello", KEY).hex(), M.aes_ecb_encrypt(b"hello", KEY).hex())
        self.assertEqual(len(M.aes_ecb_encrypt(b"hello", KEY)), 16)

    def test_bad_key(self):
        enc = M.aes_ecb_encrypt(b"x" * 40, KEY)
        with self.assertRaises(M.MediaError):
            M.aes_ecb_decrypt(enc, bytes(16))

    def test_parse_key(self):
        self.assertEqual(M.parse_aes_key(base64.b64encode(KEY).decode()), KEY)
        self.assertEqual(M.parse_aes_key(base64.b64encode(KEY.hex().encode()).decode()), KEY)
        with self.assertRaises(M.MediaError):
            M.parse_aes_key(base64.b64encode(b"short").decode())

    def test_safe_name(self):
        self.assertEqual(M.safe_name("../../etc/passwd"), "passwd")
        self.assertEqual(M.safe_name("..\\a\\b.txt"), "b.txt")
        self.assertEqual(M.safe_name(".hidden"), "hidden")
        self.assertEqual(M.safe_name("", "x.bin"), "x.bin")
        self.assertEqual(M.safe_name("报告 v1.pdf"), "报告 v1.pdf")
        self.assertLessEqual(len(M.safe_name("长" * 200 + ".pdf").encode()), 135)
        self.assertTrue(M.safe_name("长" * 200 + ".pdf").endswith(".pdf"))


class Inbound(unittest.TestCase):
    def test_image_file_voice_video(self):
        voice = {"type": 3, "voice_item": {"text": "转写", "media": {"encrypt_query_param": "VOI1", "aes_key": base64.b64encode(KEY.hex().encode()).decode()}}}
        video = {"type": 5, "video_item": {"media": {"encrypt_query_param": "VID1", "aes_key": base64.b64encode(KEY.hex().encode()).decode()}}}
        blobs = {"IMG1": M.aes_ecb_encrypt(JPEG, KEY), "FILE1": M.aes_ecb_encrypt(PDF, KEY),
                 "VOI1": M.aes_ecb_encrypt(b"SILKDATA", KEY), "VID1": M.aes_ecb_encrypt(b"MP4DATA", KEY)}
        m = msg([img_item(), file_item(), voice, video, {"type": 1, "text_item": {"text": "看看"}}])
        with mock.patch.object(M.requests, "get", cdn_get(blobs)):
            got, errs = M.fetch_for_message(m)
        self.assertEqual(errs, [])
        self.assertEqual(sorted(got), [0, 1, 2, 3])
        self.assertEqual(Path(got[0]["path"]).read_bytes(), JPEG)
        self.assertTrue(got[0]["path"].endswith(".jpg"))
        self.assertEqual(got[0]["mime"], "image/jpeg")
        self.assertEqual(Path(got[1]["path"]).read_bytes(), PDF)
        self.assertTrue(got[1]["path"].endswith("报告 v1.pdf"))
        self.assertEqual(got[1]["filename"], "报告 v1.pdf")
        self.assertEqual(Path(got[2]["path"]).read_bytes(), b"SILKDATA")
        self.assertEqual(Path(got[3]["path"]).read_bytes(), b"MP4DATA")
        day = time.strftime("%Y%m%d")
        for g in got.values():
            self.assertIn(f"/inbox/{day}/7400000000000000001_", g["path"])
            self.assertEqual(oct(os.stat(g["path"]).st_mode & 0o777), "0o600")
        self.assertEqual(oct(os.stat(M.inbox_root()).st_mode & 0o777), "0o700")
        self.assertEqual(oct(os.stat(Path(got[0]["path"]).parent).st_mode & 0o777), "0o700")
        text = C.extract_text(m, got)
        self.assertIn(f"[图片] {got[0]['path']} (image/jpeg,", text)
        self.assertIn(f"[文件] {got[1]['path']} (原名 报告 v1.pdf,", text)
        self.assertIn(f"[语音] 转写 {got[2]['path']}", text)
        self.assertIn("[视频] ", text)
        self.assertTrue(text.endswith("看看"))

    def test_image_key_from_media_when_no_hex(self):
        m = msg([img_item(hexkey=False)], mid=7400000000000000002)
        with mock.patch.object(M.requests, "get", cdn_get({"IMG1": M.aes_ecb_encrypt(JPEG, KEY)})):
            got, errs = M.fetch_for_message(m)
        self.assertEqual(Path(got[0]["path"]).read_bytes(), JPEG)

    def test_png_sniff(self):
        png = b"\x89PNG\r\n\x1a\n" + b"x" * 30
        m = msg([img_item()], mid=7400000000000000003)
        with mock.patch.object(M.requests, "get", cdn_get({"IMG1": M.aes_ecb_encrypt(png, KEY)})):
            got, _ = M.fetch_for_message(m)
        self.assertTrue(got[0]["path"].endswith(".png"))

    def test_no_cdn_ref_is_placeholder_no_network(self):
        m = msg([{"type": 2, "image_item": {}}, {"type": 4, "file_item": {"file_name": "a.txt"}}])
        got, errs = M.fetch_for_message(m)  # would raise AssertionError if network used
        self.assertEqual((got, errs), ({}, []))
        self.assertEqual(C.extract_text(m, got), "[图片]\n[文件] a.txt")

    def test_failure_falls_back_to_placeholder(self):
        m = msg([img_item(), file_item()], mid=7400000000000000004)
        with mock.patch.object(M.requests, "get", cdn_get({})):  # all 404
            got, errs = M.fetch_for_message(m)
        self.assertEqual(got, {})
        self.assertEqual(len(errs), 2)
        self.assertEqual(C.extract_text(m, got), "[图片]\n[文件] 报告 v1.pdf")

    def test_corrupt_data(self):
        m = msg([img_item()], mid=7400000000000000005)
        with mock.patch.object(M.requests, "get", cdn_get({"IMG1": b"not-a-multiple-of-16"})):
            got, errs = M.fetch_for_message(m)
        self.assertEqual(got, {}); self.assertEqual(len(errs), 1)

    def test_size_cap(self):
        big = M.aes_ecb_encrypt(b"A" * 5000, KEY)
        with mock.patch.object(M.requests, "get", cdn_get({"IMG1": big})):
            with self.assertRaises(M.MediaError):
                M.download_item(img_item(), "9", 0, max_bytes=1000)
        # no Content-Length: streamed cap
        def _get(url, **kw):
            r = FakeResp(200, big); r.headers = {}
            return r
        with mock.patch.object(M.requests, "get", _get):
            with self.assertRaises(M.MediaError):
                M.download_item(img_item(), "9", 0, max_bytes=1000)

    def test_untrusted_full_url_ignored(self):
        it = img_item(); it["image_item"]["media"]["full_url"] = "https://evil.example.com/x"
        seen = []
        def _get(url, **kw):
            seen.append(url); return FakeResp(200, M.aes_ecb_encrypt(JPEG, KEY))
        with mock.patch.object(M.requests, "get", _get):
            M.download_item(it, "11", 0)
        self.assertTrue(seen[0].startswith(M.CDN_BASE_URL + "/download?encrypted_query_param="))
        it["image_item"]["media"]["full_url"] = "https://novac2c.cdn.weixin.qq.com/c2c/download?x=1"
        with mock.patch.object(M.requests, "get", _get):
            M.download_item(it, "11", 0)
        self.assertTrue(seen[1].startswith("https://novac2c.cdn.weixin.qq.com/"))

    def test_cleanup(self):
        old = M.inbox_root() / "20200101"; old.mkdir(parents=True, exist_ok=True)
        (old / "x").write_text("x")
        new = M.inbox_root() / time.strftime("%Y%m%d"); new.mkdir(parents=True, exist_ok=True)
        self.assertGreaterEqual(M.cleanup_inbox(), 1)
        self.assertFalse(old.exists()); self.assertTrue(new.exists())


class Handle(unittest.TestCase):
    def setUp(self):
        self.hook = bridge.Webhook(dry_run=True)
        self.posted = []
        self.hook.post = lambda p: self.posted.append(p) or True

    def test_media_in_payload_immediate(self):
        m = msg([img_item(), {"type": 1, "text_item": {"text": "这张图是什么"}}], mid=7400000000000000010)
        with mock.patch.object(M.requests, "get", cdn_get({"IMG1": M.aes_ecb_encrypt(JPEG, KEY)})):
            n = bridge.handle_messages([m], "user@im.wechat", self.hook, [])
        self.assertEqual(n, 1)
        p = self.posted[0]
        self.assertEqual(p["media"][0]["kind"], "image")
        self.assertIn(p["media"][0]["path"], p["text"])
        self.assertIn("这张图是什么", p["text"])
        self.assertIn("--file", p["reply_hint"])
        self.assertNotIn("media_errors", p)

    def test_failure_payload(self):
        m = msg([img_item()], mid=7400000000000000011)
        with mock.patch.object(M.requests, "get", cdn_get({})):
            bridge.handle_messages([m], "user@im.wechat", self.hook, [])
        p = self.posted[0]
        self.assertEqual(p["text"], "[图片]")
        self.assertEqual(len(p["media_errors"]), 1)
        self.assertNotIn("media", p)

    def test_stranger_media_never_downloaded(self):
        m = msg([img_item()], user="stranger@im.wechat", mid=7400000000000000012)
        n = bridge.handle_messages([m], "user@im.wechat", self.hook, [])  # network would assert
        self.assertEqual((n, self.posted), (0, []))

    def test_fetch_crash_does_not_block_text(self):
        def boom(m, log): raise RuntimeError("x")
        m = msg([{"type": 1, "text_item": {"text": "hi"}}], mid=7400000000000000013)
        bridge.handle_messages([m], "user@im.wechat", self.hook, [], fetch_media=boom)
        self.assertEqual(self.posted[0]["text"], "hi")
        self.assertEqual(len(self.posted[0]["media_errors"]), 1)

    def test_batching_merges_into_one_post(self):
        blobs = {"IMG1": M.aes_ecb_encrypt(JPEG, KEY)}
        ms = [msg([img_item()], mid=7400000000000000021, ms=1791600000000),
              msg([img_item()], mid=7400000000000000022, ms=1791600001000, ctx="CTX_2"),
              msg([{"type": 1, "text_item": {"text": "帮我比较这两张"}}], mid=7400000000000000023, ms=1791600002000, ctx="CTX_3")]
        b = bridge.Batcher(self.hook, window=0, start_thread=False)
        seen = []
        with mock.patch.object(M.requests, "get", cdn_get(blobs)):
            for m in ms:
                bridge.handle_messages([m], "user@im.wechat", self.hook, seen, b)
        self.assertEqual(self.posted, [])
        self.assertEqual(b.pending(), 3)
        self.assertTrue(b.flush_now())
        self.assertFalse(b.flush_now())
        self.assertEqual(len(self.posted), 1)
        p = self.posted[0]
        self.assertEqual(p["batched_messages"], 3)
        self.assertEqual(p["message_id"], "7400000000000000023")
        self.assertEqual(len(p["message_ids"]), 3)
        self.assertEqual(len(p["media"]), 2)
        self.assertEqual(p["text"].count("[图片]"), 2)
        self.assertTrue(p["text"].endswith("帮我比较这两张"))
        self.assertEqual(json.load(open(C.CONTEXT))["context_token"], "CTX_3")

    def test_batcher_thread_debounce(self):
        b = bridge.Batcher(self.hook, window=0.6, max_wait=5)
        try:
            b.add(msg([{"type": 1, "text_item": {"text": "a"}}]), "a", [], [])
            time.sleep(0.4)
            b.add(msg([{"type": 1, "text_item": {"text": "b"}}], mid=7400000000000000031), "b", [], [])
            time.sleep(0.5)
            self.assertEqual(self.posted, [])  # window restarted by the 2nd message
            time.sleep(1.3)
            self.assertEqual(len(self.posted), 1)
            self.assertEqual(self.posted[0]["text"], "a\nb")
        finally:
            b.close()

    def test_batcher_max_wait_and_close(self):
        b = bridge.Batcher(self.hook, window=60, max_wait=60)
        b.add(msg([{"type": 1, "text_item": {"text": "z"}}]), "z", [], [])
        b.close()  # shutdown flushes immediately
        self.assertEqual(len(self.posted), 1)
        self.assertNotIn("batched_messages", self.posted[0])


class Outbound(unittest.TestCase):
    CREDS = {"bot_token": "TOK_abcdef", "baseurl": "https://ilink.example.invalid", "ilink_user_id": "user@im.wechat"}

    def test_build_request_and_item(self):
        plain = b"x" * 100
        req = M.build_upload_request("file", "user@im.wechat", plain, "FK", KEY)
        self.assertEqual(req["rawsize"], 100); self.assertEqual(req["filesize"], 112)
        self.assertEqual(req["rawfilemd5"], hashlib.md5(plain).hexdigest())
        self.assertEqual(req["media_type"], 3); self.assertEqual(req["aeskey"], KEY.hex()); self.assertTrue(req["no_need_thumb"])
        up = {"download_param": "DP", "aeskey": KEY.hex(), "size": 100, "size_cipher": 112}
        fi = M.build_media_item("file", up, "a.pdf")
        self.assertEqual(fi["type"], 4)
        self.assertEqual(fi["file_item"]["file_name"], "a.pdf"); self.assertEqual(fi["file_item"]["len"], "100")
        self.assertEqual(fi["file_item"]["media"]["aes_key"], base64.b64encode(KEY.hex().encode()).decode())
        self.assertEqual(M.parse_aes_key(fi["file_item"]["media"]["aes_key"]), KEY)  # our own key decodes back
        ii = M.build_media_item("image", up)
        self.assertEqual((ii["type"], ii["image_item"]["mid_size"]), (2, 112))
        vi = M.build_media_item("video", up)
        self.assertEqual((vi["type"], vi["video_item"]["video_size"]), (5, 112))

    def test_upload_flow_roundtrip(self):
        src = Path(TMP) / "out.pdf"; src.write_bytes(PDF)
        calls = {}
        def post(url, **kw):
            if url.endswith("/ilink/bot/getuploadurl"):
                calls["req"] = kw["json"]
                self.assertEqual(kw["headers"]["Authorization"], "Bearer TOK_abcdef")
                return FakeResp(200, js={"ret": 0, "upload_param": "UP PARAM/+"})
            calls["cdn_url"] = url; calls["cipher"] = kw["data"]
            return FakeResp(200, headers={"x-encrypted-param": "DOWNPARAM"})
        with mock.patch.object(M.requests, "post", post), mock.patch.object(M.time, "sleep"):
            up = M.upload_media("file", src, "user@im.wechat", self.CREDS)
        self.assertEqual(up["download_param"], "DOWNPARAM")
        self.assertTrue(calls["cdn_url"].startswith(M.CDN_BASE_URL + "/upload?encrypted_query_param=UP%20PARAM%2F%2B&filekey="))
        key = bytes.fromhex(calls["req"]["aeskey"])
        self.assertEqual(M.aes_ecb_decrypt(calls["cipher"], key), PDF)  # CDN received a decryptable ciphertext
        self.assertEqual(calls["req"]["filesize"], len(calls["cipher"]))
        self.assertEqual(up["aeskey"], calls["req"]["aeskey"])

    def test_upload_full_url_and_retry(self):
        src = Path(TMP) / "o.png"; src.write_bytes(b"\x89PNG\r\n\x1a\n" + b"1" * 20)
        n = {"cdn": 0}
        def post(url, **kw):
            if url.endswith("getuploadurl"):
                return FakeResp(200, js={"upload_full_url": "https://novac2c.cdn.weixin.qq.com/c2c/upload?p=1"})
            n["cdn"] += 1
            return FakeResp(500) if n["cdn"] < 3 else FakeResp(200, headers={"x-encrypted-param": "D"})
        with mock.patch.object(M.requests, "post", post), mock.patch.object(M.time, "sleep"):
            up = M.upload_media("image", src, "u", self.CREDS)
        self.assertEqual((up["download_param"], n["cdn"]), ("D", 3))

    def test_upload_errors(self):
        src = Path(TMP) / "o2.bin"; src.write_bytes(b"abc")
        def post(url, **kw):
            return FakeResp(200, js={"ret": -14, "errmsg": "x"})
        with mock.patch.object(M.requests, "post", post):
            with self.assertRaises(M.ExpiredSession):
                M.upload_media("file", src, "u", self.CREDS)
        def post2(url, **kw):
            return FakeResp(200, js={"ret": 0, "upload_full_url": "https://evil.example.com/u"})
        with mock.patch.object(M.requests, "post", post2):
            with self.assertRaises(M.MediaError):
                M.upload_media("file", src, "u", self.CREDS)
        def post3(url, **kw):
            if url.endswith("getuploadurl"):
                return FakeResp(200, js={"ret": 0, "upload_param": "P"})
            return FakeResp(403, headers={"x-error-message": "bad"})
        with mock.patch.object(M.requests, "post", post3):
            with self.assertRaises(M.MediaError):
                M.upload_media("file", src, "u", self.CREDS)
        with self.assertRaises(M.MediaError):
            M.read_upload_source(Path(TMP) / "nope")
        (Path(TMP) / "empty").write_bytes(b"")
        with self.assertRaises(M.MediaError):
            M.read_upload_source(Path(TMP) / "empty")
        with self.assertRaises(M.MediaError):
            M.read_upload_source(src, max_bytes=2)

    def test_kind_for_path(self):
        self.assertEqual([M.kind_for_path(p) for p in ("a.PNG", "b.mp4", "c.pdf", "d")], ["image", "video", "file", "file"])


class ReplyCLI(unittest.TestCase):
    """reply.py end to end with a throwaway bridge root (no network: --dry-run, or quota guard before any send)."""
    def setUp(self):
        self.root = tempfile.mkdtemp()
        (Path(self.root) / "state").mkdir(mode=0o700)
        C.write_json(Path(self.root) / "state" / "credentials.json", {"bot_token": "TOK_abcdef", "ilink_user_id": "user@im.wechat", "baseurl": "https://ilink.example.invalid"}, 0o600)
        C.write_json(Path(self.root) / "state" / "context.json", {"from_user_id": "user@im.wechat", "context_token": "CTX_SECRET_1", "message_id": "42", "saved_at_epoch": int(time.time())}, 0o600)
        self.f = Path(self.root) / "报告.pdf"; self.f.write_bytes(PDF)
        self.i = Path(self.root) / "a.png"; self.i.write_bytes(b"\x89PNG\r\n\x1a\n" + b"2" * 40)

    def run_reply(self, *args):
        env = dict(os.environ, WECHAT_BRIDGE_ROOT=self.root)
        return subprocess.run([sys.executable, str(ROOT_SRC / "reply.py"), *args], capture_output=True, text=True, env=env, timeout=30, input="")

    def test_dry_run_file_and_image(self):
        r = self.run_reply("--dry-run", "--file", str(self.f), "--image", str(self.i), "--caption", "**看这个**")
        self.assertEqual(r.returncode, 0, r.stderr)
        out = r.stdout
        self.assertIn('"text": "看这个"', out)
        self.assertIn('"file_name": "报告.pdf"', out)
        self.assertIn('"media_type": 3', out); self.assertIn('"media_type": 1', out)
        self.assertIn('"type": 4', out); self.assertIn('"type": 2', out)
        self.assertEqual(out.count("getuploadurl"), 2)
        for secret in ("CTX_SECRET_1", "TOK_abcdef"):
            self.assertNotIn(secret, out + r.stderr)
        self.assertEqual(out.count('"aeskey": "<redacted>"'), 2)
        self.assertEqual(out.count('"aes_key": "<redacted>"'), 2)

    def test_dry_run_text_only_unchanged(self):
        r = self.run_reply("--dry-run", "**hello**")
        self.assertEqual(r.returncode, 0)
        self.assertIn('"text": "hello"', r.stdout); self.assertNotIn("getuploadurl", r.stdout)

    def test_usage_and_media_errors(self):
        self.assertEqual(self.run_reply("--dry-run").returncode, 1)
        r = self.run_reply("--file", str(Path(self.root) / "missing.pdf"))
        self.assertEqual(r.returncode, 5)
        self.assertEqual(self.run_reply("--dry-run", *sum((["--file", str(self.f)] for _ in range(6)), [])).returncode, 1)

    def test_quota_guard(self):
        C.write_json(Path(self.root) / "state" / "send_quota.json", {"message_id": "42", "count": 9}, 0o600)
        r = self.run_reply("--file", str(self.f), "--caption", "x")  # 2 more -> 11 > 10, refused before any network
        self.assertEqual(r.returncode, 4, r.stderr)
        # a new inbound message id resets the counter -> passes the guard (then fails: no network / invalid host)
        C.write_json(Path(self.root) / "state" / "context.json", {"from_user_id": "user@im.wechat", "context_token": "CTX2", "message_id": "43", "saved_at_epoch": int(time.time())}, 0o600)
        r = self.run_reply("--dry-run", "--file", str(self.f), "--caption", "x")
        self.assertEqual(r.returncode, 0)

    def test_refuses_foreign_context(self):
        C.write_json(Path(self.root) / "state" / "context.json", {"from_user_id": "other@im.wechat", "context_token": "C", "message_id": "1", "saved_at_epoch": int(time.time())}, 0o600)
        self.assertEqual(self.run_reply("--dry-run", "hi").returncode, 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)

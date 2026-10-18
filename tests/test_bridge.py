import json, os, sys, tempfile, unittest
from pathlib import Path
from unittest import mock

TMP = tempfile.mkdtemp()
os.environ["WECHAT_BRIDGE_ROOT"] = TMP
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import requests
def _no_net(*a, **k): raise AssertionError("network call attempted in test")
requests.post = requests.get = _no_net

import ilink_common as C
import bridge

FAKE = json.load(open(Path(__file__).parent / "fake_updates.json"))


class Markdown(unittest.TestCase):
    def test_basic(self):
        md = "# 标题\n**粗体** 和 *斜体* 和 `code`\n- 一\n* 二\n> 引用\n[官网](https://x.ai) 和 ![图](https://a/b.png)\n---\n~~删~~"
        out = C.strip_markdown(md)
        for sym in ("**", "#", "`", "](", "~~", "> ", "---"):
            self.assertNotIn(sym, out, sym)
        self.assertIn("官网 https://x.ai", out)
        self.assertIn("- 二", out)
        self.assertTrue(out.startswith("标题"))

    def test_table(self):
        md = "| 基金 | 规模 |\n| --- | :---: |\n| GRID | $12B |\n| XLU | $23B |"
        out = C.strip_markdown(md)
        self.assertNotIn("|", out)
        self.assertNotIn("---", out)
        self.assertIn("GRID  $12B", out)

    def test_code_fence_and_snake_case(self):
        out = C.strip_markdown("```bash\ncat a_b_c.txt\n```\nuse file_name_here and 2*3*4")
        self.assertIn("cat a_b_c.txt", out)
        self.assertIn("file_name_here", out)
        self.assertIn("2*3*4", out)
        self.assertNotIn("```", out)

    def test_bare_link(self):
        self.assertEqual(C.strip_markdown("[https://a.com](https://a.com)"), "https://a.com")

    def test_split(self):
        self.assertEqual(C.split_text("短"), ["短"])
        long = ("段落" * 300 + "\n\n") * 5
        parts = C.split_text(long, 2000)
        self.assertTrue(all(len(p) <= 2000 for p in parts))
        self.assertEqual("".join(parts).replace("\n", ""), long.replace("\n", ""))


class Filtering(unittest.TestCase):
    def test_filter(self):
        kept = C.filter_messages(FAKE[0]["msgs"], "user@im.wechat")
        self.assertEqual([m["seq"] for m in kept], [1, 4])
        self.assertEqual(C.filter_messages(FAKE[0]["msgs"], ""), [])
        self.assertEqual(C.filter_messages(FAKE[1]["msgs"], "user@im.wechat")[0]["seq"], 1)  # group dropped

    def test_extract(self):
        self.assertEqual(C.extract_text(FAKE[0]["msgs"][3]), "[语音] 语音转文字内容\n[图片]")

    def test_handle_dry_run(self):
        hook = bridge.Webhook(dry_run=True)
        posted = []
        hook.post = lambda p: posted.append(p) or True
        seen = []
        n1 = bridge.handle_messages(FAKE[0]["msgs"], "user@im.wechat", hook, seen)
        n2 = bridge.handle_messages(FAKE[1]["msgs"], "user@im.wechat", hook, seen)
        self.assertEqual((n1, n2), (2, 0))  # duplicate id not re-forwarded
        self.assertEqual(posted[0]["source"], "wechat")
        self.assertEqual(posted[0]["message_id"], "7300000000000000001")
        self.assertEqual(set(posted[0]) >= {"source", "from_user_id", "text", "message_id", "create_time"}, True)
        ctx = json.load(open(C.CONTEXT))
        self.assertEqual(ctx["context_token"], "CTX_A3")  # newest token kept
        self.assertEqual(oct(os.stat(C.CONTEXT).st_mode & 0o777), "0o600")
        self.assertNotIn("stranger", json.dumps(posted))

    def test_errcode(self):
        self.assertIsNone(C.errcode_of({"ret": 0}))
        self.assertIsNone(C.errcode_of({}))
        self.assertEqual(C.errcode_of({"ret": -14}), -14)
        self.assertEqual(C.errcode_of({"ret": 0, "errcode": -14}), -14)

    def test_key_normalize(self):
        for raw in ("abc123", "Bearer abc123", "Authorization: Bearer abc123", "  bearer abc123 "):
            self.assertEqual(bridge.normalize_key(raw), "abc123")

    def test_redact(self):
        C.register_secret("supersecretkey123")
        self.assertNotIn("supersecretkey123", C.redact("key supersecretkey123 here"))
        self.assertEqual(C.redact("Authorization: Bearer abc.def"), "Authorization: Bearer ***")

    def test_mark_expired(self):
        C.write_text(C.CURSOR, "x"); C.write_json(C.CONTEXT, {"a": 1}, 0o600)
        C.mark_expired("test")
        self.assertTrue(C.EXPIRED_FLAG.exists())
        self.assertFalse(C.CURSOR.exists()); self.assertFalse(C.CONTEXT.exists())
        C.EXPIRED_FLAG.unlink()

    def test_headers(self):
        h = C.post_headers("tok")
        self.assertEqual(h["Authorization"], "Bearer tok")
        self.assertEqual(h["iLink-App-ClientVersion"], "132105")
        self.assertNotIn("Content-Length", h)

    def test_expired_get_updates(self):
        class R:
            status_code = 200
            def json(self): return {"ret": -14, "errmsg": "session timeout"}
        with mock.patch.object(bridge.requests, "post", return_value=R()):
            with self.assertRaises(bridge.Expired):
                bridge.get_updates({"bot_token": "t", "baseurl": "https://example.invalid"}, "", 1000)


if __name__ == "__main__":
    unittest.main(verbosity=2)

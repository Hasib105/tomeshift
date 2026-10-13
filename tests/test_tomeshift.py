"""Fast tests (no real LLM): limit parsing, output checks, and a full run against a mock API."""
import importlib.util, os, subprocess, sys, tempfile, threading, unittest
from datetime import datetime, timezone
from http.server import HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.argv = ["tomeshift"]
spec = importlib.util.spec_from_file_location("tomeshift", ROOT / "tomeshift.py")
ts = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ts)


class LimitParsing(unittest.TestCase):
    def test_detects_limits(self):
        for msg in ["Claude AI usage limit reached|1760000000", "You've hit your limit · resets 3pm (UTC)",
                    "429 RESOURCE_EXHAUSTED", "Error: Too Many Requests"]:
            self.assertTrue(ts.LIMIT_RE.search(msg), msg)

    def test_reset_times(self):
        now = datetime.now(timezone.utc)
        self.assertEqual(ts.parse_reset("limit reached|1760000000").year, 2025)
        t = ts.parse_reset("Try again in 2h 5m")
        self.assertAlmostEqual((t - now).total_seconds(), 7500, delta=5)
        self.assertIsNone(ts.parse_reset("please try again later"))  # "again" must not match "in"


class Checks(unittest.TestCase):
    src = "Alice ran.\n\nThe Rabbit was late.\n"

    def test_good_bangla(self):
        out = "অ্যালিস দৌড়াল। " * 4 + "\n\nখরগোশের দেরি হয়ে গেল। " * 3
        self.assertEqual(ts.check(self.src * 3, out, {}, "en", "bn"), [])

    def test_untranslated(self):
        self.assertTrue(ts.check(self.src, self.src, {}, "en", "bn"))

    def test_extra_paragraphs_from_context(self):
        out = "\n\n".join(["অ্যালিস দৌড়াল। খরগোশের দেরি হয়ে গেল।" * 2] * 6)
        self.assertTrue(any("extra paragraphs" in p for p in ts.check(self.src * 2, out, {}, "en", "bn")))

    def test_glossary(self):
        out = "অ্যালিস দৌড়াল। " * 6 + "\n\nদেরি হয়ে গেল। " * 3
        gloss = {"Alice": "এলিস", "Rabbit": "খরগোশ", "late": "দেরি!", "ran": "ছুটল"}
        self.assertTrue(any("glossary" in p for p in ts.check(self.src * 3, out, gloss, "en", "bn")))


class EndToEnd(unittest.TestCase):
    def test_mock_api_run_with_429(self):
        sys.path.insert(0, str(ROOT / "tests"))
        import mock_openai
        srv = HTTPServer(("127.0.0.1", 0), mock_openai.H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        with tempfile.TemporaryDirectory() as d:
            env = {**os.environ, "OPENAI_BASE_URL": f"http://127.0.0.1:{srv.server_port}/v1"}
            run = lambda *a: subprocess.run([sys.executable, str(ROOT / "tomeshift.py"), *a], env=env,
                                            capture_output=True, text=True, timeout=120)
            r = run("prep", str(ROOT / "samples/alice.txt"), "-p", d, "--words", "150")
            self.assertEqual(r.returncode, 0, r.stderr)
            r = run("run", "-p", d, "--agents", "openai-api", "-j", "2")
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("paused", r.stdout)  # first request got 429
            self.assertEqual(len(list(Path(d, "out").glob("*.md"))), 3)
            r = run("build", "-p", d)
            self.assertTrue(Path(d, "book_bn.epub").exists())
        srv.shutdown()


class WebUI(unittest.TestCase):
    def test_upload_and_status(self):
        import json, time, urllib.request
        sys.path.insert(0, str(ROOT))
        import tomeshift_ui
        with tempfile.TemporaryDirectory() as d:
            srv = tomeshift_ui.ThreadingHTTPServer(("127.0.0.1", 0), tomeshift_ui.make_handler(
                d, {c: v[0] for c, v in ts.LANGS.items()}, ts.BACKENDS))
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            base = f"http://127.0.0.1:{srv.server_port}"
            get = lambda path: json.loads(urllib.request.urlopen(base + path).read())
            self.assertIn("bn", get("/api/meta")["langs"])
            req = urllib.request.Request(base + "/api/prep?p=alice&from=en&lang=bn&words=150", method="POST",
                                         data=(ROOT / "samples/alice.txt").read_bytes(), headers={"X-Filename": "alice.txt"})
            urllib.request.urlopen(req)
            for _ in range(60):
                st = get("/api/status?p=alice")
                if st["job"] and st["job"]["ok"] is not None:
                    break
                time.sleep(0.5)
            self.assertTrue(st["job"]["ok"], st["job"])
            self.assertEqual(st["chunks"], 3)
            self.assertEqual(get("/api/projects")[0]["name"], "alice")
            self.assertIn("Source term", urllib.request.urlopen(base + "/").read().decode())
            srv.shutdown()


if __name__ == "__main__":
    unittest.main()

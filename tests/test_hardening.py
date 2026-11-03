"""Run: python -m unittest discover tests  (from the repository folder)"""
import io, json, os, sys, tempfile, unittest, urllib.error
from pathlib import Path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import ac_net, ac_tag_writer, ac_live


class NetTests(unittest.TestCase):
    def test_https_and_loopback_only(self):
        self.assertEqual(ac_net.check_server_url("https://bench.example.com/"), "https://bench.example.com")
        self.assertEqual(ac_net.check_server_url("http://localhost:8080"), "http://localhost:8080")
        self.assertEqual(ac_net.check_server_url("http://127.0.0.1:8080"), "http://127.0.0.1:8080")
        for bad in ("http://bench.example.com", "http://localhost.evil.com", "ftp://x.example", "https://u:p@x.example",
                    "bench.example.com", ""):
            with self.assertRaises(ValueError, msg=bad):
                ac_net.check_server_url(bad)

    def test_reply_cap(self):
        self.assertEqual(ac_net.read_capped(io.BytesIO(b"x" * 10), 10), b"x" * 10)
        with self.assertRaises(ValueError):
            ac_net.read_capped(io.BytesIO(b"x" * 11), 10)

    def test_redirect_to_other_origin_is_refused(self):
        h = ac_net._SameOriginRedirect()
        req = urllib.request.Request("https://bench.example.com/v1/x", headers={"Authorization": "Bearer k"})
        with self.assertRaises(urllib.error.HTTPError):
            h.redirect_request(req, io.BytesIO(), 302, "Found", {}, "https://evil.example/steal")
        with self.assertRaises(urllib.error.HTTPError):
            h.redirect_request(req, io.BytesIO(), 302, "Found", {}, "http://bench.example.com/v1/x")
        self.assertIsNotNone(h.redirect_request(req, io.BytesIO(), 302, "Found", {}, "https://bench.example.com/v1/y"))


class LabelTests(unittest.TestCase):
    def test_valid_labels(self):
        for ok in ("1A", "1Z", "2A", "3F", "10B"):
            self.assertTrue(ac_tag_writer.valid_label(ok), ok)
        for bad in ("", "A", "AA", "0A", "1a", "1AB", "1A\n", "../1A", "<b>1A", "1A" * 3, None, 5):
            self.assertFalse(ac_tag_writer.valid_label(bad), repr(bad))

    def test_bad_label_never_reaches_a_car_file(self):
        with tempfile.TemporaryDirectory() as d:
            cars = Path(d)
            for name in ("good", "evil"):
                (cars / name / "ui").mkdir(parents=True)
                (cars / name / "ui" / "ui_car.json").write_text(json.dumps({"tags": ["keep"]}))
            ac_tag_writer.main([str(cars), "--apply"], labels={"good": "1B", "evil": "1A\", \"x"})
            self.assertEqual(json.loads((cars / "good/ui/ui_car.json").read_text())["tags"], ["keep", "#Pride-1B"])
            self.assertEqual(json.loads((cars / "evil/ui/ui_car.json").read_text())["tags"], ["keep"])

    def test_lookup_drops_malformed_records(self):
        good = "a" * 64
        reply = {"cars": [{"car_hash": good, "class": "1A"}, {"car_hash": "../x"}, "junk", {"class": "1A"}], "tracks": []}
        orig = ac_live.ac_cloud.call
        ac_live.ac_cloud.call = lambda *a, **k: (200, reply, {})
        try:
            out = ac_live.lookup("https://bench.example.com", [good])
        finally:
            ac_live.ac_cloud.call = orig
        self.assertEqual(list(out["records"]), [good])


import urllib.request  # noqa: E402
if __name__ == "__main__":
    unittest.main()

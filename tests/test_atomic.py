"""Concurrency tests for single-use codes and rate limits. Run: python -m unittest discover tests"""
import io, json, os, sys, tempfile, threading, unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import server


def call(handler, db, body, headers=None):
    environ = {"CONTENT_LENGTH": str(len(body)), "wsgi.input": io.BytesIO(body), "REMOTE_ADDR": "203.0.113.9"}
    environ.update(headers or {})
    cfg = server.get_config(db)
    try:
        code, out, _ = handler(db, cfg, environ)
        return code
    except server.HttpError as e:
        return e.code


class AtomicTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "t.db")
        server.init_db(self.path).close()
        db = server.connect(self.path)
        cfg = server.get_config(db)
        cfg["rate_register_per_hour"] = 10000
        server.set_config(db, cfg)
        db.close()

    def tearDown(self):
        self.dir.cleanup()

    def race(self, n, fn):
        results, barrier = [], threading.Barrier(n)

        def run(i):
            db = server.connect(self.path)
            barrier.wait()
            results.append(fn(db, i))
            db.close()
        ts = [threading.Thread(target=run, args=(i,)) for i in range(n)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        return results

    def test_one_use_invite_registers_once(self):
        db = server.connect(self.path)
        code = server.new_invite_code()
        db.execute("INSERT INTO invites(code_hash, role, uses_left, created) VALUES(?,?,?,?)",
                   (server.invite_hash(code), "user", 1, server.now()))
        db.commit(); db.close()
        out = self.race(12, lambda db, i: call(server.handle_register, db,
                        json.dumps({"invite": code, "name": "racer%02d" % i}).encode()))
        self.assertEqual(out.count(201), 1, out)
        db = server.connect(self.path)
        self.assertEqual(db.execute("SELECT COUNT(*) c FROM users WHERE name LIKE 'racer%'").fetchone()["c"], 1)
        self.assertEqual(db.execute("SELECT uses_left FROM invites").fetchone()["uses_left"], 0)

    def test_limited_invite_never_exceeds_uses(self):
        db = server.connect(self.path)
        code = server.new_invite_code()
        db.execute("INSERT INTO invites(code_hash, role, uses_left, created) VALUES(?,?,?,?)",
                   (server.invite_hash(code), "user", 3, server.now()))
        db.commit(); db.close()
        out = self.race(15, lambda db, i: call(server.handle_register, db,
                        json.dumps({"invite": code, "name": "multi%02d" % i}).encode()))
        self.assertEqual(out.count(201), 3, out)

    def test_taken_name_does_not_burn_a_use(self):
        db = server.connect(self.path)
        code = server.new_invite_code()
        db.execute("INSERT INTO invites(code_hash, role, uses_left, created) VALUES(?,?,?,?)",
                   (server.invite_hash(code), "user", 2, server.now()))
        db.commit()
        body = json.dumps({"invite": code, "name": "samename"}).encode()
        self.assertEqual(call(server.handle_register, db, body), 201)
        self.assertEqual(call(server.handle_register, db, body), 409)
        self.assertEqual(db.execute("SELECT uses_left FROM invites").fetchone()["uses_left"], 1)

    def test_device_link_redeems_once(self):
        db = server.connect(self.path)
        db.execute("INSERT INTO users(name, key_hash, role, created) VALUES('owner','x','user',?)", (server.now(),))
        uid = db.execute("SELECT id FROM users").fetchone()["id"]
        code = "ABCD-EFGH-JKMN"
        db.execute("INSERT INTO device_links(code_hash, user_id, created) VALUES(?,?,?)",
                   (server.invite_hash(code), uid, server.now()))
        db.commit(); db.close()
        out = self.race(12, lambda db, i: call(server.handle_redeem, db,
                        json.dumps({"code": code, "label": "dev%d" % i}).encode()))
        self.assertEqual(out.count(201), 1, out)
        db = server.connect(self.path)
        self.assertEqual(db.execute("SELECT COUNT(*) c FROM user_keys").fetchone()["c"], 1)

    def test_rate_limit_holds_under_burst(self):
        def hit(db, i):
            try:
                server.rate_limit(db, "ip:test", "burst", 5)
                return 200
            except server.HttpError as e:
                return e.code
        out = self.race(20, hit)
        self.assertEqual(out.count(200), 5, out)


if __name__ == "__main__":
    unittest.main()

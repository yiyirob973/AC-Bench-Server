"""id_token signature and configuration checks. Run: python -m unittest discover tests"""
import os, sys, time, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import admin

try:
    import jwt
    from cryptography.hazmat.primitives.asymmetric import rsa
except ImportError:   # pragma: no cover
    jwt = None

ISS, CID = "https://id.example.com", "client-1"


def settings(**kw):
    s = {"origin": "https://bench.example.com", "issuer": ISS, "client_id": CID, "client_secret": "",
         "subs": [], "emails": ["a@example.com"], "groups": [], "ttl": 3600}
    s.update(kw)
    return s


@unittest.skipIf(jwt is None, "PyJWT with crypto not installed")
class OidcTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        jwk = jwt.algorithms.RSAAlgorithm.to_jwk(cls.key.public_key(), as_dict=True)
        jwk.update(kid="k1", alg="RS256", use="sig")
        cls.jwks = {"keys": [jwk]}

    def setUp(self):
        admin._jwks.update(t=time.time(), keys=self.jwks["keys"])
        self.d = {"jwks_uri": ISS + "/jwks"}

    def token(self, key=None, **over):
        claims = {"iss": ISS, "aud": CID, "sub": "u1", "exp": int(time.time()) + 300}
        claims.update(over)
        return jwt.encode(claims, key or self.key, algorithm="RS256", headers={"kid": "k1"})

    def test_good_token(self):
        self.assertEqual(admin._id_token_claims(self.token(), settings(), self.d)["sub"], "u1")

    def test_forged_signature_rejected(self):
        with self.assertRaises(admin.LoginError):
            admin._id_token_claims(self.token(key=self.other), settings(), self.d)

    def test_alg_none_rejected(self):
        forged = jwt.encode({"iss": ISS, "aud": CID, "sub": "u1", "exp": int(time.time()) + 300}, None, algorithm="none")
        with self.assertRaises(admin.LoginError):
            admin._id_token_claims(forged, settings(), self.d)

    def test_wrong_audience_issuer_and_expiry_rejected(self):
        for over in ({"aud": "someone-else"}, {"iss": "https://evil.example"}, {"exp": int(time.time()) - 600}):
            with self.assertRaises(admin.LoginError):
                admin._id_token_claims(self.token(**over), settings(), self.d)

    def test_email_allowlist_needs_verified_email(self):
        s = settings()
        self.assertTrue(admin.allowed(s, {"sub": "x", "email": "A@example.com", "email_verified": True}))
        self.assertFalse(admin.allowed(s, {"sub": "x", "email": "a@example.com", "email_verified": False}))
        self.assertFalse(admin.allowed(s, {"sub": "x", "email": "a@example.com"}))

    def test_http_config_stays_closed_except_loopback(self):
        self.assertIsNone(admin.config_problem(settings()))
        self.assertIn("https", admin.config_problem(settings(origin="http://bench.example.com")))
        self.assertIn("https", admin.config_problem(settings(issuer="http://id.example.com")))
        self.assertIsNone(admin.config_problem(settings(origin="http://localhost:8080", issuer="http://127.0.0.1:1411")))


if __name__ == "__main__":
    unittest.main()

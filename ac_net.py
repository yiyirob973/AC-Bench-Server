"""Shared rules for talking to the benchmark server: https only (http just for this computer), no redirects to
another address, and a cap on how much a reply may hold."""
import urllib.error, urllib.request
from urllib.parse import urlparse

LOOPBACK = ("localhost", "127.0.0.1", "::1")
MAX_REPLY = 32 * 1024 * 1024      # the full class list is the biggest thing the server sends
MAX_SMALL_REPLY = 1024 * 1024     # conditions and similar small documents
MAX_ERROR_BODY = 64 * 1024


def check_server_url(url):
    """Returns the cleaned address, or raises ValueError with a message that can be shown as is."""
    url = (url or "").strip().rstrip("/")
    u = urlparse(url)
    if not u.netloc or u.scheme not in ("http", "https"):
        raise ValueError("The server address must start with https://")
    if u.username or u.password:
        raise ValueError("The server address must not contain a user name or password.")
    if u.scheme == "http" and u.hostname not in LOOPBACK:
        raise ValueError("The server address must start with https://. Plain http is only accepted for "
                         "localhost, because it would send your API key unencrypted.")
    return url


def read_capped(resp, limit=MAX_REPLY):
    raw = resp.read(limit + 1)
    if len(raw) > limit:
        raise ValueError("The server's reply is larger than the app accepts.")
    return raw


class _SameOriginRedirect(urllib.request.HTTPRedirectHandler):
    """urllib would resend the Authorization header wherever a redirect points; only follow ones that stay put."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        a, b = urlparse(req.full_url), urlparse(newurl)
        if (a.scheme, a.hostname, a.port) != (b.scheme, b.hostname, b.port):
            raise urllib.error.HTTPError(req.full_url, code, "redirected to another address, not followed", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_opener = urllib.request.build_opener(_SameOriginRedirect)


def open_url(req, timeout):
    return _opener.open(req, timeout=timeout)

def serve_file(path, content_type):
    p = HERE / path
    if not p.is_file():
        raise HttpError(404, "not found")
    body = p.read_bytes()
    headers = [
        ("Content-Type", content_type),
        ("Content-Length", str(len(body))),
        ("X-Content-Type-Options", "nosniff"),
        ("Cache-Control", "no-store"),
    ]
    return 200, body, headers


# ... inside route(...)
        if method == "GET" and path in ("/", "/stats"):
            return serve_file("stats.html", "text/html; charset=utf-8")
        if method == "GET" and path == "/v1/health":
            return respond(200, {"ok": True, "api": API_VERSION, "time": now()})

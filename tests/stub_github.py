"""A stand-in for the parts of GitHub's API that playbooks/release/github-app-token.py calls, for its tests.

It checks what a real GitHub would refuse: the App's JWT (RS256 with the App's key, iss the App's ID, at most ten
minutes), and that the installation token is the one it made. It records every call, with its Authorization header,
so that a test can say which calls were made and that no credential reached the helper's output. How it answers is set
per test: the permissions and repositories a token is granted, and a status for any call that should fail.
"""

import base64
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding

TOKEN = "ghs_stubInstallationToken0123456789abcdef"
INSTALLATION = 77
APP_ID = "42"
REPOSITORY = "kristofk/AIDepartment"
# The repositories a token reaches when a test does not say: only the one asked for.
ONLY_IT = object()


def unb64(text):
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


class StubGitHub:
    def __init__(self, public_key, granted=None, repositories=ONLY_IT, fail=None):
        self.public_key = public_key
        # What the token is granted; GitHub adds metadata: read to every token.
        self.granted = {"contents": "write"} if granted is None else granted
        # The repositories the token reaches: None leaves the field out of the answer.
        self.repositories = [REPOSITORY] if repositories is ONLY_IT else repositories
        self.fail = fail or {}  # {"installation"|"mint"|"read"|"revoke": status}
        self.calls = []
        self.jwts = []
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()

    def jwt_valid(self, jwt):
        """The App's JWT, as GitHub checks it: signed RS256 by the App's key, iss its ID, within ten minutes."""
        header, payload, signature = jwt.split(".")
        if json.loads(unb64(header)) != {"alg": "RS256", "typ": "JWT"}:
            return False
        try:
            self.public_key.verify(unb64(signature), f"{header}.{payload}".encode(), padding.PKCS1v15(), hashes.SHA256())
        except InvalidSignature:
            return False
        claims = json.loads(unb64(payload))
        now = time.time()
        return (claims["iss"] == APP_ID and claims["iat"] <= now and claims["exp"] > now
                and claims["exp"] - claims["iat"] <= 600)

    def _handler(self):
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def answer(self, status, body=None):
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                if body is not None:
                    self.wfile.write(json.dumps(body).encode())

            def handle_call(self, method):
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length)) if length else None
                auth = self.headers.get("Authorization", "")
                credential = auth.split(" ", 1)[1] if " " in auth else ""
                stub.calls.append({"method": method, "path": self.path, "body": body, "credential": credential})
                route = (method, self.path)
                if route == ("GET", f"/repos/{REPOSITORY}/installation"):
                    stub.jwts.append(credential)
                    if "installation" in stub.fail:
                        return self.answer(stub.fail["installation"], {"message": "Not Found"})
                    if not stub.jwt_valid(credential):
                        return self.answer(401, {"message": "A JSON web token could not be decoded"})
                    return self.answer(200, {"id": INSTALLATION, "app_slug": "aid-release",
                                             "account": {"login": "kristofk"}})
                if route == ("POST", f"/app/installations/{INSTALLATION}/access_tokens"):
                    stub.jwts.append(credential)
                    if not stub.jwt_valid(credential):
                        return self.answer(401, {"message": "A JSON web token could not be decoded"})
                    if "mint" in stub.fail:
                        return self.answer(stub.fail["mint"], {"message": "Validation Failed"})
                    answer = {"token": TOKEN, "expires_at": "2026-11-14T20:00:00Z",
                              "permissions": {**stub.granted, "metadata": "read"}}
                    if stub.repositories is not None:
                        answer["repositories"] = [{"full_name": name} for name in stub.repositories]
                    return self.answer(201, answer)
                if route == ("GET", f"/repos/{REPOSITORY}"):
                    if credential != TOKEN:
                        return self.answer(401, {"message": "Bad credentials"})
                    if "read" in stub.fail:
                        return self.answer(stub.fail["read"], {"message": "Resource not accessible by integration"})
                    return self.answer(200, {"full_name": REPOSITORY})
                if route == ("DELETE", "/installation/token"):
                    if credential != TOKEN:
                        return self.answer(401, {"message": "Bad credentials"})
                    if "revoke" in stub.fail:
                        return self.answer(stub.fail["revoke"], {"message": "Server Error"})
                    return self.answer(204)
                return self.answer(404, {"message": "Not Found"})

            def do_GET(self):
                self.handle_call("GET")

            def do_POST(self):
                self.handle_call("POST")

            def do_DELETE(self):
                self.handle_call("DELETE")

        return Handler

    def routes(self):
        return [(call["method"], call["path"]) for call in self.calls]

    def revoked(self):
        return any(call["method"] == "DELETE" and call["credential"] == TOKEN for call in self.calls)

    def secrets(self):
        """Every credential the helper sent: the JWTs and the token. None may reach its output."""
        return [jwt for jwt in self.jwts if jwt] + [TOKEN]

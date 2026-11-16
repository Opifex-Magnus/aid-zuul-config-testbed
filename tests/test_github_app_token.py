"""playbooks/release/github-app-token.py against a stand-in for GitHub (stub_github.py), with a key made here.

The helper is run as the release's credential check runs it (playbooks/release/credentials.yaml), with --api pointing
it at the stand-in. Each test checks its exit status and its one JSON line, the calls it made, that a token it got was
revoked, and that neither the App's key, nor a JWT, nor the token is in anything it printed.
"""

import base64
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa

sys.path.insert(0, os.path.dirname(__file__))
import stub_github  # noqa: E402

HELPER = os.path.join(os.path.dirname(__file__), "..", "playbooks", "release", "github-app-token.py")


def new_key():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
                            serialization.NoEncryption())
    return key, pem


KEY, PEM = new_key()


def github_fingerprint(key):
    """The fingerprint GitHub shows beside an App's key: SHA-256 of the public key's DER, base64."""
    der = key.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    return "SHA256:" + base64.b64encode(hashlib.sha256(der).digest()).decode()


def run(stub, pem=PEM, app_id=stub_github.APP_ID, permissions=("contents=write",), key_file=None, keep=None):
    """Runs the helper with the key on stdin, or, with key_file, in that file and nothing on stdin."""
    args = [sys.executable, "-I", HELPER, "--app-id", app_id, "--repository", stub_github.REPOSITORY]
    for permission in permissions:
        args += ["--permission", permission]
    if key_file:
        args += ["--key-file", key_file]
    if keep:
        args += ["--keep", keep]
    args += ["--api", stub.url]
    result = subprocess.run(args, input=None if key_file else pem, stdin=subprocess.DEVNULL if key_file else None,
                            capture_output=True, timeout=60)
    return result.returncode, result.stdout.decode(), result.stderr.decode()


class HelperTest(unittest.TestCase):
    def assertNoSecret(self, stub, *texts, pem=PEM):
        key_body = b"".join(pem.splitlines()[1:-1]).decode()
        for text in texts:
            self.assertNotIn(key_body[:40], text, "the App's key is in the output")
            for secret in stub.secrets():
                self.assertNotIn(secret, text, "a JWT or the token is in the output")

    def assertRefused(self, stub, words, revoked=True):
        status, out, err = run(stub)
        self.assertEqual(status, 1, out + err)
        self.assertEqual(list(json.loads(out)), ["error"], out)
        self.assertIn(words, json.loads(out)["error"])
        self.assertEqual(stub.revoked(), revoked, stub.routes())
        self.assertNoSecret(stub, out, err)
        return json.loads(out)["error"]

    def test_the_token_is_made_checked_and_revoked(self):
        with stub_github.StubGitHub(KEY.public_key()) as stub:
            status, out, err = run(stub)
        self.assertEqual(status, 0, out + err)
        self.assertEqual(json.loads(out), {
            "account": "kristofk", "app_id": 42, "app_slug": "aid-release",
            "expires_at": "2026-11-14T20:00:00Z", "installation_id": stub_github.INSTALLATION,
            "key_fingerprint": github_fingerprint(KEY), "permissions": {"contents": "write"},
            "repository": stub_github.REPOSITORY, "revoked": True,
        })
        self.assertEqual(stub.routes(), [
            ("GET", "/repos/kristofk/AIDepartment/installation"),
            ("POST", f"/app/installations/{stub_github.INSTALLATION}/access_tokens"),
            ("GET", "/repos/kristofk/AIDepartment"),
            ("DELETE", "/installation/token"),
        ])
        # The token is asked for that one repository and those permissions only.
        self.assertEqual(stub.calls[1]["body"], {"repositories": ["AIDepartment"], "permissions": {"contents": "write"}})
        self.assertTrue(stub.revoked())
        self.assertNoSecret(stub, out, err)

    def test_the_key_may_be_a_file(self):
        # As credentials.yaml gives it (AIDepartment#1542).
        with tempfile.TemporaryDirectory() as directory:
            key_file = os.path.join(directory, "key.pem")
            with open(key_file, "wb") as handle:
                handle.write(PEM)
            with stub_github.StubGitHub(KEY.public_key()) as stub:
                status, out, err = run(stub, key_file=key_file)
        self.assertEqual(status, 0, out + err)
        self.assertEqual(json.loads(out)["key_fingerprint"], github_fingerprint(KEY))
        self.assertTrue(stub.revoked())
        self.assertNoSecret(stub, out, err)

    def test_a_missing_key_file_fails_before_any_call(self):
        with stub_github.StubGitHub(KEY.public_key()) as stub:
            status, out, err = run(stub, key_file="/nonexistent/key.pem")
        self.assertEqual(status, 1, out + err)
        self.assertIn("FileNotFoundError", json.loads(out)["error"])
        self.assertEqual(stub.routes(), [])

    def test_a_lesser_grant_is_refused_and_the_token_revoked(self):
        with stub_github.StubGitHub(KEY.public_key(), granted={"contents": "read"}) as stub:
            self.assertRefused(stub, "GitHub granted {'contents': 'read'}, not {'contents': 'write'}")
        self.assertNotIn(("GET", "/repos/kristofk/AIDepartment"), stub.routes(), "the token was used after the refusal")

    def test_a_wider_grant_is_refused_and_the_token_revoked(self):
        with stub_github.StubGitHub(KEY.public_key(), granted={"contents": "write", "issues": "write"}) as stub:
            self.assertRefused(stub, "not {'contents': 'write'}")

    def test_another_repository_is_refused_and_the_token_revoked(self):
        with stub_github.StubGitHub(KEY.public_key(), repositories=[stub_github.REPOSITORY, "kristofk/other"]) as stub:
            self.assertRefused(stub, "the token reaches ['kristofk/AIDepartment', 'kristofk/other'], not only")

    def test_no_repository_list_is_refused_and_the_token_revoked(self):
        # A token for every repository of the installation comes with no list.
        with stub_github.StubGitHub(KEY.public_key(), repositories=None) as stub:
            self.assertRefused(stub, "the token reaches [], not only kristofk/AIDepartment")

    def test_a_failed_read_is_refused_and_the_token_revoked(self):
        with stub_github.StubGitHub(KEY.public_key(), fail={"read": 403}) as stub:
            error = self.assertRefused(stub, "GET /repos/kristofk/AIDepartment: 403")
        self.assertIn("Resource not accessible by integration", error)

    def test_a_refused_token_leaves_nothing_to_revoke(self):
        with stub_github.StubGitHub(KEY.public_key(), fail={"mint": 422}) as stub:
            self.assertRefused(stub, f"POST /app/installations/{stub_github.INSTALLATION}/access_tokens: 422",
                               revoked=False)
        self.assertNotIn("DELETE", [method for method, _ in stub.routes()])

    def test_no_installation_on_the_repository_fails(self):
        with stub_github.StubGitHub(KEY.public_key(), fail={"installation": 404}) as stub:
            self.assertRefused(stub, "GET /repos/kristofk/AIDepartment/installation: 404", revoked=False)
        self.assertEqual(len(stub.routes()), 1)

    def test_a_failed_revocation_fails_the_check(self):
        with stub_github.StubGitHub(KEY.public_key(), fail={"revoke": 500}) as stub:
            status, out, err = run(stub)
        self.assertEqual(status, 1, out + err)
        self.assertIn("DELETE /installation/token: 500", json.loads(out)["error"])
        self.assertNoSecret(stub, out, err)

    def test_another_key_is_refused_by_github(self):
        other, other_pem = new_key()
        with stub_github.StubGitHub(KEY.public_key()) as stub:
            status, out, err = run(stub, pem=other_pem)
        self.assertEqual(status, 1, out + err)
        self.assertIn("GET /repos/kristofk/AIDepartment/installation: 401", json.loads(out)["error"])
        self.assertEqual(stub.routes(), [("GET", "/repos/kristofk/AIDepartment/installation")])
        self.assertNoSecret(stub, out, err, pem=other_pem)

    def test_a_key_that_is_not_rsa_is_refused_before_any_call(self):
        pem = ec.generate_private_key(ec.SECP256R1()).private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
        with stub_github.StubGitHub(KEY.public_key()) as stub:
            status, out, err = run(stub, pem=pem)
        self.assertEqual((status, json.loads(out)), (1, {"error": "Failure: the App's key is not an RSA key"}))
        self.assertEqual(stub.routes(), [])

    def test_what_is_not_a_key_is_refused_before_any_call(self):
        with stub_github.StubGitHub(KEY.public_key()) as stub:
            status, out, err = run(stub, pem=b"not a key\n")
        self.assertEqual(status, 1, out + err)
        self.assertTrue(json.loads(out)["error"].startswith("ValueError: "), out)
        self.assertEqual(stub.routes(), [])

    def test_an_app_id_that_is_not_a_number_is_refused_before_any_call(self):
        with stub_github.StubGitHub(KEY.public_key()) as stub:
            status, out, _ = run(stub, app_id="42; rm -rf /")
        self.assertEqual((status, json.loads(out)), (1, {"error": "Failure: --app-id must be the App's number"}))
        self.assertEqual(stub.routes(), [])


if __name__ == "__main__":
    unittest.main()


def revoke(stub, path, *extra):
    result = subprocess.run([sys.executable, "-I", HELPER, "--revoke", path, *extra, "--api", stub.url],
                            stdin=subprocess.DEVNULL, capture_output=True, timeout=60)
    return result.returncode, result.stdout.decode(), result.stderr.decode()


class KeepTest(unittest.TestCase):
    """--keep and --revoke (AIDepartment#1350): a token a playbook uses, then revokes."""

    assertNoSecret = HelperTest.assertNoSecret

    def test_a_kept_token_is_checked_written_for_its_owner_and_not_revoked(self):
        granted = {"contents": "write", "issues": "write"}
        with tempfile.TemporaryDirectory() as directory, stub_github.StubGitHub(KEY.public_key(), granted=granted) as stub:
            path = os.path.join(directory, "token")
            status, out, err = run(stub, permissions=("contents=write", "issues=write"), keep=path)
            self.assertEqual(status, 0, out + err)
            self.assertEqual((json.loads(out)["revoked"], json.loads(out)["kept"]), (False, True))
            self.assertEqual(stub.calls[1]["body"]["permissions"], {"contents": "write", "issues": "write"})
            self.assertFalse(stub.revoked())
            with open(path) as handle:
                self.assertEqual(handle.read(), stub_github.TOKEN)
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
            self.assertNoSecret(stub, out, err)
            status, out, err = revoke(stub, path)
            self.assertEqual((status, json.loads(out)), (0, {"revoked": True}), out + err)
            self.assertTrue(stub.revoked())
            self.assertFalse(os.path.exists(path), "the token's file is removed")
            self.assertNoSecret(stub, out, err)

    def test_a_refused_grant_writes_nothing_and_revokes(self):
        with tempfile.TemporaryDirectory() as directory, stub_github.StubGitHub(KEY.public_key(), granted={"contents": "read"}) as stub:
            path = os.path.join(directory, "token")
            status, out, _ = run(stub, keep=path)
            self.assertEqual(status, 1, out)
            self.assertTrue(stub.revoked())
            self.assertFalse(os.path.exists(path))

    def test_a_file_there_already_is_never_replaced(self):
        with tempfile.TemporaryDirectory() as directory, stub_github.StubGitHub(KEY.public_key()) as stub:
            path = os.path.join(directory, "token")
            os.symlink(os.path.join(directory, "elsewhere"), path)
            status, out, _ = run(stub, keep=path)
            self.assertEqual(status, 1, out)
            self.assertIn("FileExistsError", json.loads(out)["error"])
            self.assertTrue(stub.revoked(), "the token that could not be kept is revoked")
            self.assertFalse(os.path.exists(os.path.join(directory, "elsewhere")))

    def test_revoke_removes_the_file_also_when_the_token_is_gone(self):
        with tempfile.TemporaryDirectory() as directory, stub_github.StubGitHub(KEY.public_key()) as stub:
            path = os.path.join(directory, "token")
            with open(path, "w") as handle:
                handle.write("ghs_expired")
            status, out, err = revoke(stub, path)
            self.assertEqual(status, 0, out + err)
            self.assertFalse(os.path.exists(path))
            status, out, _ = revoke(stub, path, "--app-id", "42")
            self.assertEqual(status, 1)
            self.assertIn("--revoke takes nothing else", json.loads(out)["error"])

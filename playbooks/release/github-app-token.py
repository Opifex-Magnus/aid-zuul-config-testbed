"""A GitHub App's installation token for one repository, made, checked and revoked (AIDepartment#1351).

The release job's credential check (zuul.d/release.yaml): with the App's private key on stdin, it signs the App's JWT,
finds the App's installation on the repository, asks for a token for that repository alone with the permissions
given, checks GitHub granted exactly those, uses it once to read the repository, and revokes it. It prints one JSON
object with what it found, and never the key, the JWT or the token. On a failure it prints {"error": ...} and exits 1.

    python3 github-app-token.py --app-id ID --repository OWNER/NAME --permission contents=write < key.pem

Standard library and cryptography only (Ansible's own dependency). Reaches api.github.com through the environment's
https_proxy, as the executor does.
"""

import argparse
import base64
import hashlib
import json
import sys
import time
import urllib.error
import urllib.request

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

API = "https://api.github.com"


class Failure(Exception):
    pass


def b64url(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def call(method, path, auth, body=None, ok=(200,)):
    request = urllib.request.Request(
        f"{API}{path}", method=method, data=None if body is None else json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {auth}", "Accept": "application/vnd.github+json",
                 "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "aid-zuul-release-credentials"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            status, text = response.status, response.read().decode()
    except urllib.error.HTTPError as error:
        status, text = error.code, error.read().decode(errors="replace")
    if status not in ok:
        # GitHub's message only: an error body carries no credential.
        try:
            message = json.loads(text).get("message", "")
        except ValueError:
            message = ""
        raise Failure(f"{method} {path}: {status} {message}".strip())
    return json.loads(text) if text else None


def main():
    global API
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--app-id", required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--permission", action="append", required=True, help="NAME=read|write, as GitHub names them")
    # Only for testing it against a stand-in for GitHub.
    parser.add_argument("--api", default=API, help=argparse.SUPPRESS)
    args = parser.parse_args()
    API = args.api.rstrip("/")
    if not args.app_id.isdigit():
        raise Failure("--app-id must be the App's number")
    owner, _, name = args.repository.partition("/")
    if not owner or not name:
        raise Failure("--repository must be owner/name")
    permissions = dict(p.split("=", 1) for p in args.permission)

    key = serialization.load_pem_private_key(sys.stdin.buffer.read(), password=None)
    if not isinstance(key, rsa.RSAPrivateKey):
        raise Failure("the App's key is not an RSA key")
    # The fingerprint GitHub shows beside each of the App's keys (its docs, "Verifying private keys").
    der = key.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    fingerprint = "SHA256:" + base64.b64encode(hashlib.sha256(der).digest()).decode()

    now = int(time.time())
    claims = {"iat": now - 60, "exp": now + 540, "iss": args.app_id}
    signing_input = f"{b64url(json.dumps({'alg': 'RS256', 'typ': 'JWT'}).encode())}.{b64url(json.dumps(claims).encode())}"
    jwt = f"{signing_input}.{b64url(key.sign(signing_input.encode(), padding.PKCS1v15(), hashes.SHA256()))}"

    installation = call("GET", f"/repos/{owner}/{name}/installation", jwt)
    made = call("POST", f"/app/installations/{installation['id']}/access_tokens", jwt,
                body={"repositories": [name], "permissions": permissions}, ok=(201,))
    token = made["token"]
    revoked = False
    try:
        granted = {k: v for k, v in made.get("permissions", {}).items() if k != "metadata"}
        repositories = [r["full_name"] for r in made.get("repositories", [])]
        if granted != permissions:
            raise Failure(f"GitHub granted {granted}, not {permissions}")
        if [r.lower() for r in repositories] != [args.repository.lower()]:
            raise Failure(f"the token reaches {repositories}, not only {args.repository}")
        repository = call("GET", f"/repos/{owner}/{name}", token)
    finally:
        call("DELETE", "/installation/token", token, ok=(204,))
        revoked = True
    print(json.dumps({
        "app_id": int(args.app_id),
        "app_slug": installation.get("app_slug"),
        "key_fingerprint": fingerprint,
        "installation_id": installation["id"],
        "account": installation.get("account", {}).get("login"),
        "repository": repository["full_name"],
        "permissions": granted,
        "expires_at": made.get("expires_at"),
        "revoked": revoked,
    }, sort_keys=True))


if __name__ == "__main__":
    try:
        main()
    except (Failure, ValueError, KeyError, OSError) as error:
        # ValueError covers a key that is not PEM; none of these messages holds the key or the token.
        print(json.dumps({"error": f"{type(error).__name__}: {error}"}))
        sys.exit(1)

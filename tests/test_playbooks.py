"""The release's playbooks under real Ansible, as Zuul 14.3.0 runs them (ansible-core 2.18, requirements.txt):
playbooks/release/credentials.yaml with the real helper against a stand-in for GitHub (stub_github.py), and
playbooks/secrets/guard.yaml. Zuul's zuul_return is a stand-in (plugins/action/zuul_return.py) that records what a
playbook returns.

credentials.yaml is copied unchanged into a directory of the test's own. Beside it is a shim of the helper's name
that runs the real helper with --api pointed at the stand-in, so the trusted playbook needs no setting for it.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(__file__))
import stub_github  # noqa: E402
from test_github_app_token import KEY, PEM, github_fingerprint  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PLAYBOOK = os.path.join(ROOT, "playbooks", "release", "credentials.yaml")
HELPER = os.path.join(ROOT, "playbooks", "release", "github-app-token.py")
GUARD = os.path.join(ROOT, "playbooks", "secrets", "guard.yaml")
ANSIBLE = shutil.which("ansible-playbook", path=os.path.dirname(sys.executable)) or shutil.which("ansible-playbook")


def ansible(playbook, extra_vars, work):
    """Runs the playbook on localhost; returns its exit status, its output, and what it returned to Zuul."""
    vars_file = os.path.join(work, "vars.json")
    with open(vars_file, "w") as handle:
        json.dump(extra_vars, handle)
    returned = os.path.join(work, "returned.json")
    env = {**os.environ, "ANSIBLE_ACTION_PLUGINS": os.path.join(HERE, "plugins", "action"),
           "TEST_ZUUL_RETURN": returned, "ANSIBLE_NOCOLOR": "1", "ANSIBLE_LOCALHOST_WARNING": "False",
           "ANSIBLE_INVENTORY_UNPARSED_WARNING": "False", "ANSIBLE_RETRY_FILES_ENABLED": "False"}
    result = subprocess.run([ANSIBLE, "-i", "localhost,", "-c", "local", playbook, "-e", f"@{vars_file}",
                             "-e", f"ansible_python_interpreter={sys.executable}",
                             # As verbose as Ansible goes: what no_log must keep out shows here first.
                             "-vvv"],
                            capture_output=True, text=True, env=env, timeout=300)
    data = []
    if os.path.exists(returned):
        with open(returned) as handle:
            data = json.load(handle)
    return result.returncode, result.stdout + result.stderr, data


def setUpModule():
    if not ANSIBLE:
        raise RuntimeError("no ansible-playbook: install tests/requirements.txt (README.md, \"Tests\")")


class CredentialsPlaybookTest(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.mkdtemp(prefix="credentials-")
        self.addCleanup(shutil.rmtree, self.work)
        self.playbook = os.path.join(self.work, "credentials.yaml")
        shutil.copyfile(PLAYBOOK, self.playbook)

    def run_with(self, stub):
        with open(os.path.join(self.work, "github-app-token.py"), "w") as shim:
            shim.write("import runpy, sys\n"
                       f"sys.argv[1:] += ['--api', {stub.url!r}]\n"
                       f"sys.argv[0] = {HELPER!r}\n"
                       f"runpy.run_path({HELPER!r}, run_name='__main__')\n")
        return ansible(self.playbook, {"aid_release_github": {"app_id": stub_github.APP_ID,
                                                              "private_key": PEM.decode()}}, self.work)

    def assertNoSecret(self, stub, output):
        self.assertNotIn(b"".join(PEM.splitlines()[1:-1]).decode()[:40], output, "the App's key is in the output")
        for secret in stub.secrets():
            self.assertNotIn(secret, output, "a JWT or the token is in the output")

    def test_success_returns_what_github_granted_as_the_artifact(self):
        with stub_github.StubGitHub(KEY.public_key()) as stub:
            status, output, returned = self.run_with(stub)
        self.assertEqual(status, 0, output)
        self.assertEqual(len(returned), 1, returned)
        artifacts = returned[0]["zuul"]["artifacts"]
        self.assertEqual([(a["name"], a["url"]) for a in artifacts],
                         [("release-credential", "https://github.com/apps/aid-release")])
        metadata = artifacts[0]["metadata"]
        self.assertEqual(metadata["permissions"], {"contents": "write"})
        self.assertEqual(metadata["repository"], stub_github.REPOSITORY)
        self.assertEqual(metadata["key_fingerprint"], github_fingerprint(KEY))
        self.assertIs(metadata["revoked"], True)
        self.assertTrue(stub.revoked())
        self.assertIn("What GitHub granted", output)
        self.assertNoSecret(stub, output)

    def test_a_refused_grant_fails_the_playbook_and_returns_the_error(self):
        with stub_github.StubGitHub(KEY.public_key(), granted={"contents": "read"}) as stub:
            status, output, returned = self.run_with(stub)
        self.assertNotEqual(status, 0, output)
        self.assertIn("The release's credential works", output)
        artifact = returned[0]["zuul"]["artifacts"][0]
        self.assertEqual((artifact["name"], artifact["url"]), ("release-credential", "https://github.com/apps/aid-release"))
        self.assertIn("GitHub granted {'contents': 'read'}", artifact["metadata"]["error"])
        self.assertTrue(stub.revoked())
        self.assertNoSecret(stub, output)

    def test_a_failed_revocation_fails_the_playbook(self):
        with stub_github.StubGitHub(KEY.public_key(), fail={"revoke": 500}) as stub:
            status, output, returned = self.run_with(stub)
        self.assertNotEqual(status, 0, output)
        self.assertIn("DELETE /installation/token: 500", returned[0]["zuul"]["artifacts"][0]["metadata"]["error"])
        self.assertNoSecret(stub, output)


class GuardPlaybookTest(unittest.TestCase):
    ALLOWED = {"job": "aid-release-credentials", "pipeline": "post", "ref": "refs/heads/main",
               "project": {"canonical_name": "github.com/kristofk/AIDepartment"}}

    def guard(self, **zuul):
        work = tempfile.mkdtemp(prefix="guard-")
        self.addCleanup(shutil.rmtree, work)
        variables = {"zuul": {**self.ALLOWED, **zuul}, "secret_pipelines": ["post"],
                     "secret_projects": ["github.com/kristofk/AIDepartment"]}
        return ansible(GUARD, variables, work)

    def assertRefused(self, **zuul):
        status, output, returned = self.guard(**zuul)
        self.assertNotEqual(status, 0, output)
        self.assertIn("has a secret, and runs only on a merged ref", " ".join(output.split()))
        self.assertEqual(returned, [{"zuul": {"retry": False}}], "the refusal must not be retried")

    def test_a_merged_ref_in_its_pipeline_for_its_project_may_have_it(self):
        status, output, returned = self.guard()
        self.assertEqual(status, 0, output)
        self.assertIn("may have its secret here: refs/heads/main in post", output)
        self.assertEqual(returned, [])

    def test_a_proposed_change_is_refused(self):
        self.assertRefused(change="1489", pipeline="post")

    def test_another_pipeline_is_refused(self):
        self.assertRefused(pipeline="gate")

    def test_another_project_is_refused(self):
        self.assertRefused(project={"canonical_name": "github.com/kristofk/other"})


if __name__ == "__main__":
    unittest.main()

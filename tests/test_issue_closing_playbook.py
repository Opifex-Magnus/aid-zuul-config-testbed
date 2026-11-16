"""The closer's executor-only credentials are scoped, hidden and always removed."""
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(__file__))
import stub_github
from test_github_app_token import KEY, PEM
from test_playbooks import HELPER, ROOT, ansible, setUpModule

PERMISSIONS = {"contents": "read", "pull_requests": "read", "issues": "write"}


class CloserPlaybookTest(unittest.TestCase):
    def run_playbook(self, *, failing=False, granted=PERMISSIONS):
        with tempfile.TemporaryDirectory(prefix="issue-closer-playbook-") as work:
            issues = Path(work) / "playbooks/issues"
            shutil.copytree(Path(ROOT) / "playbooks/issues", issues)
            release = Path(work) / "playbooks/release"
            release.mkdir()
            with stub_github.StubGitHub(KEY.public_key(), granted=granted) as stub:
                (release / "github-app-token.py").write_text(
                    "import runpy, sys\n"
                    f"sys.argv[1:] += ['--api', {stub.url!r}]\n"
                    f"sys.argv[0] = {HELPER!r}\nrunpy.run_path({HELPER!r}, run_name='__main__')\n")
                # Only the API logic is replaced; Ansible and the App token helper are real.
                (issues / "close.py").write_text(
                    "import json, pathlib, stat, sys\n"
                    "args = dict(zip(sys.argv[1::2], sys.argv[2::2]))\n"
                    "token = pathlib.Path(args['--token-file'])\n"
                    f"assert token.read_text().strip() == {stub_github.TOKEN!r}\n"
                    "assert stat.S_IMODE(token.stat().st_mode) == 0o600\n"
                    f"pathlib.Path({str(Path(work) / 'seen.json')!r}).write_text(json.dumps(args))\n"
                    "print(json.dumps({'closed': [7], 'external_links_skipped': []}))\n"
                    f"sys.exit({1 if failing else 0})\n")
                status, output, _ = ansible(str(issues / "close.yaml"), {
                    "zuul": {"branch": "main", "oldrev": "1" * 40, "newrev": "2" * 40},
                    "aid_release_github": {"app_id": stub_github.APP_ID, "private_key": PEM.decode()},
                }, work)
                # The helper's own key argument is public; follow it to verify both files' cleanup.
                seen_file = Path(work) / "seen.json"
                seen = json.loads(seen_file.read_text()) if seen_file.exists() else None
                if seen:
                    self.assertFalse(Path(seen["--token-file"]).parent.exists())
                    self.assertEqual(seen["--oldrev"], "1" * 40)
                    self.assertEqual(seen["--newrev"], "2" * 40)
                    self.assertEqual(seen["--repository"], stub_github.REPOSITORY)
                for secret in stub.secrets() + [PEM.decode()]:
                    self.assertNotIn(secret, output)
                self.assertTrue(any(call["method"] == "DELETE" for call in stub.calls), output)
                return status, output, seen

    def test_closes_with_scoped_token_then_revokes_and_removes_it(self):
        status, output, seen = self.run_playbook()
        self.assertEqual(status, 0, output)
        self.assertIsNotNone(seen)

    def test_closer_failure_still_revokes_and_removes_the_token(self):
        status, output, seen = self.run_playbook(failing=True)
        self.assertNotEqual(status, 0, output)
        self.assertIsNotNone(seen)

    def test_missing_accepted_permission_never_runs_the_closer(self):
        status, output, seen = self.run_playbook(granted={"contents": "read", "issues": "write"})
        self.assertNotEqual(status, 0, output)
        self.assertIsNone(seen)


if __name__ == "__main__":
    unittest.main()

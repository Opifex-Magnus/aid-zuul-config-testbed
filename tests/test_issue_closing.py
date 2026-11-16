"""Post-merge issue closure uses GitHub's links and validates merge provenance."""
from copy import deepcopy
from pathlib import Path
import runpy
import unittest

closer = runpy.run_path(str(Path(__file__).resolve().parents[1] / "playbooks/issues/close.py"))
GitHub, close, Failure = (closer[name] for name in ("GitHub", "close", "Failure"))
OLD, NEW = "1" * 40, "2" * 40
REPO = "owner/repo"


class FakeGitHub(GitHub):
    def __init__(self):
        super().__init__(REPO, "not-a-secret")
        self.calls = []
        self.issue = {"state": "open", "number": 7}
        self.pull = {"number": 5, "merged": True, "base": {"ref": "main", "repo": {"full_name": REPO}},
                     "merge_commit_sha": NEW, "merged_by": {"login": "aid-zuul[bot]"}}
        self.links = [{"number": 7, "repository": {"nameWithOwner": REPO}},
                      {"number": 8, "repository": {"nameWithOwner": "other/repo"}}]
        self.compare = {"status": "ahead", "behind_by": 0, "commits": [{"sha": NEW}]}
        self.failure = False

    def call(self, method, path, body=None):
        self.calls.append((method, path, body))
        if path == f"/repos/{REPO}":
            return {"default_branch": "main"}
        if "/compare/" in path:
            return self.compare
        if "/commits/" in path:
            return [{"number": 5}, {"number": 5}]
        if path == f"/repos/{REPO}/pulls/5":
            return self.pull
        if path == "/graphql":
            return {"data": {"repository": {"pullRequest": {"closingIssuesReferences": {
                "nodes": self.links, "pageInfo": {"hasNextPage": False, "endCursor": None},
            }}}}}
        if path == f"/repos/{REPO}/issues/7":
            if method == "PATCH":
                if self.failure:
                    raise Failure("GitHub PATCH: HTTP 403")
                self.issue.update(body)
            return deepcopy(self.issue)
        raise AssertionError((method, path, body))


class IssueClosingTest(unittest.TestCase):
    def test_closes_only_github_closing_links_and_is_idempotent(self):
        github = FakeGitHub()
        self.assertEqual(close(github, OLD, NEW, "main"),
                         {"closed": [7], "external_links_skipped": ["other/repo#8"]})
        self.assertEqual(github.issue["state_reason"], "completed")
        self.assertEqual(close(github, OLD, NEW, "main")["closed"], [])
        self.assertEqual(len([call for call in github.calls if call[0] == "PATCH"]), 1)

    def test_wrong_branch_unmerged_other_merger_and_unrelated_merge_are_ignored(self):
        for patch in ({"merged": False}, {"merge_commit_sha": OLD}, {"merged_by": {"login": "someone"}},
                      {"base": {"ref": "feature", "repo": {"full_name": REPO}}},
                      {"base": {"ref": "main", "repo": {"full_name": "other/repo"}}}):
            with self.subTest(patch=patch):
                github = FakeGitHub()
                github.pull.update(patch)
                self.assertEqual(close(github, OLD, NEW, "main")["closed"], [])
                self.assertFalse(any(call[0] == "PATCH" for call in github.calls))

    def test_invalid_push_is_refused(self):
        for old, new, branch in [("0" * 40, NEW, "main"), (OLD, "oops", "main"), (OLD, NEW, "feature")]:
            with self.assertRaises(Failure):
                close(FakeGitHub(), old, new, branch)
        github = FakeGitHub()
        github.compare["behind_by"] = 1
        with self.assertRaises(Failure):
            close(github, OLD, NEW, "main")

    def test_api_refusal_fails_the_job(self):
        github = FakeGitHub()
        github.failure = True
        with self.assertRaisesRegex(Failure, "403"):
            close(github, OLD, NEW, "main")

    def test_compare_and_associated_pr_lists_are_paginated(self):
        github = FakeGitHub()
        seen = []
        def call(method, path, body=None):
            seen.append(path)
            items = list(range(100)) if path.endswith("page=1") else [100]
            return {"commits": items} if "/compare/" in path else items
        github.call = call
        self.assertEqual(sum(len(p["commits"]) for p in github.pages("/compare/a...b")), 101)
        self.assertEqual(sum(len(p) for p in github.pages("/commits/a/pulls")), 101)
        self.assertEqual(len(seen), 4)

    def test_closing_links_are_paginated(self):
        github = FakeGitHub()
        cursors = []
        def call(method, path, body=None):
            cursor = body["variables"]["after"]
            cursors.append(cursor)
            return {"data": {"repository": {"pullRequest": {"closingIssuesReferences": {
                "nodes": [cursor], "pageInfo": {"hasNextPage": cursor is None, "endCursor": "next"},
            }}}}}
        github.call = call
        self.assertEqual(list(github.closing_issues(5)), [None, "next"])
        self.assertEqual(cursors, [None, "next"])

    def test_link_pagination_must_advance(self):
        github = FakeGitHub()
        github.call = lambda *args, **kwargs: {"data": {"repository": {"pullRequest": {"closingIssuesReferences": {
            "nodes": [], "pageInfo": {"hasNextPage": True, "endCursor": None},
        }}}}}
        with self.assertRaisesRegex(Failure, "did not advance"):
            list(github.closing_issues(5))

    def test_waits_for_github_to_index_the_rebased_commit(self):
        github = FakeGitHub()
        original = github.call
        attempts, sleeps = [], []
        def delayed(method, path, body=None):
            if "/commits/" in path:
                attempts.append(path)
                if len(attempts) < 3:
                    return []
            return original(method, path, body)
        github.call = delayed
        self.assertEqual(close(github, OLD, NEW, "main", sleep=sleeps.append)["closed"], [7])
        self.assertEqual(sleeps, [5, 5])

    def test_direct_push_has_no_closing_issues_and_wait_is_bounded(self):
        github = FakeGitHub()
        original = github.call
        github.call = lambda method, path, body=None: [] if "/commits/" in path else original(method, path, body)
        sleeps = []
        self.assertEqual(close(github, OLD, NEW, "main", sleep=sleeps.append)["closed"], [])
        self.assertEqual(len(sleeps), 12)


if __name__ == "__main__":
    unittest.main()

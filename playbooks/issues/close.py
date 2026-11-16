"""Close GitHub's closing-issue links after Zuul rebase merges (AIDepartment#1656).

Runs only from the trusted post job, with a repository-scoped aid-release token:
contents: read, pull_requests: read, issues: write. Never parses PR prose or
executes repository code. Re-running an already completed push is a no-op.
"""

import argparse
import json
from pathlib import Path
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.github.com"


class Failure(Exception):
    pass


class GitHub:
    def __init__(self, repository, token, api=API):
        self.repository, self.token, self.api = repository, token, api.rstrip("/")

    def call(self, method, path, body=None):
        request = urllib.request.Request(self.api + path, method=method,
            data=None if body is None else json.dumps(body).encode(),
            headers={"Authorization": f"Bearer {self.token}", "Accept": "application/vnd.github+json",
                     "Content-Type": "application/json", "X-GitHub-Api-Version": "2022-11-28",
                     "User-Agent": "aid-zuul-close-issues"})
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                data = json.load(response)
        except urllib.error.HTTPError as error:
            raise Failure(f"GitHub {method} {path}: HTTP {error.code}") from None
        if isinstance(data, dict) and data.get("errors"):
            raise Failure(f"GitHub {method} {path}: GraphQL errors")
        return data

    def repo(self, path):
        return self.call("GET", f"/repos/{self.repository}{path}")

    def pages(self, path):
        for page in range(1, 1001):
            data = self.repo(f"{path}?per_page=100&page={page}")
            yield data
            items = data["commits"] if isinstance(data, dict) else data
            if len(items) < 100:
                return
        raise Failure("GitHub pagination exceeded 1000 pages")

    def closing_issues(self, number):
        owner, name = self.repository.split("/")
        query = """query($owner:String!, $name:String!, $number:Int!, $after:String) {
          repository(owner:$owner, name:$name) { pullRequest(number:$number) {
            closingIssuesReferences(first:100, after:$after) {
              nodes { number repository { nameWithOwner } }
              pageInfo { hasNextPage endCursor }
            }
          } }
        }"""
        after = None
        for _ in range(1000):
            data = self.call("POST", "/graphql", {"query": query, "variables": {
                "owner": owner, "name": name, "number": number, "after": after,
            }})["data"]["repository"]["pullRequest"]["closingIssuesReferences"]
            yield from data["nodes"]
            info = data["pageInfo"]
            if not info["hasNextPage"]:
                return
            cursor = info["endCursor"]
            if not cursor or cursor == after:
                raise Failure("GitHub closing-issue pagination did not advance")
            after = cursor
        raise Failure("GitHub closing-issue pagination exceeded 1000 pages")


def close(github, oldrev, newrev, branch, merger="aid-zuul[bot]"):
    if not all(re.fullmatch(r"[0-9a-f]{40}", rev) and rev != "0" * 40 for rev in (oldrev, newrev)):
        raise Failure("oldrev and newrev must be nonzero commit SHAs")
    repo = github.repo("")
    if branch != repo["default_branch"]:
        raise Failure("Issue closure is only allowed on the default branch")
    commits = set()
    for page in github.pages(f"/compare/{oldrev}...{newrev}"):
        if page["status"] not in ("ahead", "identical") or page["behind_by"] != 0:
            raise Failure("The push is not a forward update")
        commits.update(commit["sha"] for commit in page["commits"])
    pulls = set()
    for sha in sorted(commits):
        for page in github.pages(f"/commits/{sha}/pulls"):
            pulls.update(pull["number"] for pull in page)
    closed, skipped = set(), set()
    for number in sorted(pulls):
        pull = github.repo(f"/pulls/{number}")
        if not (pull["merged"] and pull["base"]["ref"] == branch
                and pull["base"]["repo"]["full_name"].lower() == github.repository.lower()
                and pull["merge_commit_sha"] in commits
                and (pull.get("merged_by") or {}).get("login") == merger):
            continue
        for issue in github.closing_issues(number):
            issue_repo, issue_number = issue["repository"]["nameWithOwner"], issue["number"]
            if issue_repo.lower() != github.repository.lower():
                skipped.add(f"{issue_repo}#{issue_number}")
                continue
            path = f"/repos/{github.repository}/issues/{issue_number}"
            current = github.call("GET", path)
            if "pull_request" in current:
                raise Failure("GitHub returned a pull request as a closing issue")
            if current["state"] == "closed":
                continue
            updated = github.call("PATCH", path, {"state": "closed", "state_reason": "completed"})
            if updated["state"] != "closed" or updated["state_reason"] != "completed":
                raise Failure(f"GitHub did not close issue #{issue_number} as completed")
            closed.add(issue_number)
    return {"closed": sorted(closed), "external_links_skipped": sorted(skipped)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--oldrev", required=True)
    parser.add_argument("--newrev", required=True)
    parser.add_argument("--branch", required=True)
    parser.add_argument("--token-file", required=True)
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", args.repository):
        raise Failure("repository must be owner/name")
    github = GitHub(args.repository, Path(args.token_file).read_text().strip())
    print(json.dumps(close(github, args.oldrev, args.newrev, args.branch)))


if __name__ == "__main__":
    try:
        main()
    except (Failure, urllib.error.URLError, KeyError, TypeError, ValueError) as error:
        print(json.dumps({"error": str(error)}))
        sys.exit(1)

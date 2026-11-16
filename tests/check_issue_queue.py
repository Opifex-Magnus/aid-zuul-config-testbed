"""Regression for AIDepartment#1656's skipped queued merge (review round 1).

Run in the pinned scheduler image, which has the manager's runtime dependencies:
  python3 -I -B tests/check_issue_queue.py
No GitHub or ZooKeeper connection. Queue storage and base enqueue persistence are
stand-ins; the installed manager's queue allocation, addChange override and
supercedent pruning are real. Each surviving push runs the production closer.
"""

from copy import deepcopy
import logging
from pathlib import Path
import runpy
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import yaml
from zuul import model
from zuul.manager import PipelineManager
from zuul.manager.independent import IndependentPipelineManager
from zuul.manager.supercedent import SupercedentPipelineManager

ROOT = Path(__file__).resolve().parents[1]
helper = runpy.run_path(str(ROOT / "tests/test_issue_closing.py"))
FakeGitHub, close = helper["FakeGitHub"], helper["close"]
REPO = helper["REPO"]
MANAGERS = {"independent": IndependentPipelineManager, "supercedent": SupercedentPipelineManager}


class TwoMergesGitHub(FakeGitHub):
    def __init__(self):
        super().__init__()
        self.issues = {7: {"number": 7, "state": "open"}, 8: {"number": 8, "state": "open"}}

    def call(self, method, path, body=None):
        self.calls.append((method, path, body))
        route = path.split("?", 1)[0]
        if "/compare/" in route:
            sha = route.split("...")[-1]
            return {"status": "ahead", "behind_by": 0, "commits": [{"sha": sha}]}
        if "/commits/" in route:
            sha = route.split("/commits/")[1].split("/")[0]
            return [] if sha == "1" * 40 else [{"number": int(sha[0]) + 3}]
        if "/pulls/" in route:
            number = int(route.rsplit("/", 1)[1])
            return {**self.pull, "number": number, "merge_commit_sha": str(number - 3) * 40}
        if path == "/graphql":
            number = body["variables"]["number"] + 2
            return {"data": {"repository": {"pullRequest": {"closingIssuesReferences": {
                "nodes": [{"number": number, "repository": {"nameWithOwner": REPO}}],
                "pageInfo": {"hasNextPage": False, "endCursor": None},
            }}}}}
        if "/issues/" in route:
            issue = self.issues[int(route.rsplit("/", 1)[1])]
            if method == "PATCH":
                issue.update(body)
            return deepcopy(issue)
        return super().call(method, path, body)


class QueueStorage:
    def __init__(self, manager):
        self.manager, self.queue = manager, []

    def addProject(self, project, branch):
        pass


def queued_pushes(manager_type):
    """Leave A active, enqueue B (a->b) and C (b->c), then return retained pushes."""
    manager = object.__new__(manager_type)
    manager.log = logging.getLogger("issue-queue-regression")
    manager.current_context = None
    manager.state = SimpleNamespace(queues=[])
    manager.state.addQueue = manager.state.queues.append
    manager.state.removeQueue = manager.state.queues.remove
    manager.removeItem = lambda item: item.queue.queue.remove(item)
    project = SimpleNamespace(name=REPO)

    def persist_enqueue(self, change, event):
        with self.getChangeQueue(change, event) as queue:
            queue.queue.append(SimpleNamespace(changes=[change], queue=queue))
        return True

    with patch.object(model.ChangeQueue, "new", side_effect=lambda context, manager, **kw: QueueStorage(manager)), \
            patch.object(PipelineManager, "addChange", persist_enqueue):
        previous = "a" * 40
        for number in (1, 2, 3):
            change = SimpleNamespace(project=project, branch="main", ref="refs/heads/main",
                                     oldrev=previous, newrev=str(number) * 40)
            manager.addChange(change, None)
            previous = change.newrev
    return [item.changes[0] for queue in manager.state.queues for item in queue.queue]


def run_survivors(pushes):
    github = TwoMergesGitHub()
    for push in pushes:
        close(github, push.oldrev, push.newrev, "main", sleep=lambda _: None)
    return github


class IssueQueueTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pipelines = {item["pipeline"]["name"]: item["pipeline"]
                         for item in yaml.safe_load((ROOT / "zuul.d/pipelines.yaml").read_text())}

    def test_original_post_manager_drops_a_merge_and_leaves_its_issue_open(self):
        pushes = queued_pushes(MANAGERS[self.pipelines["post"]["manager"]])
        self.assertEqual([push.newrev[0] for push in pushes], ["1", "3"])
        github = run_survivors(pushes)
        self.assertEqual(github.issues[7]["state"], "open")
        self.assertEqual(github.issues[8]["state_reason"], "completed")
        close(github, "1" * 40, "2" * 40, "main")
        self.assertEqual(github.issues[7]["state_reason"], "completed")

    def test_issue_pipeline_retains_both_merges_behind_active_buildset(self):
        pipeline = self.pipelines["post-issues"]
        self.assertTrue(pipeline["post-review"])
        pushes = queued_pushes(MANAGERS[pipeline["manager"]])
        self.assertEqual([push.newrev[0] for push in pushes], ["1", "2", "3"])
        github = run_survivors(pushes)
        self.assertEqual([github.issues[n]["state_reason"] for n in (7, 8)], ["completed", "completed"])
        self.assertEqual([call[1] for call in github.calls if call[0] == "PATCH"],
                         [f"/repos/{REPO}/issues/7", f"/repos/{REPO}/issues/8"])


if __name__ == "__main__":
    unittest.main(verbosity=2)

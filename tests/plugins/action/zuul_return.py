"""A stand-in for Zuul's zuul_return action, for the playbook tests (test_playbooks.py): it appends what a task
returns, its data, to the JSON list in the file TEST_ZUUL_RETURN names, where Zuul would keep it for the build."""

import json
import os

from ansible.plugins.action import ActionBase


class ActionModule(ActionBase):
    def run(self, tmp=None, task_vars=None):
        result = super().run(tmp, task_vars)
        path = os.environ["TEST_ZUUL_RETURN"]
        returned = []
        if os.path.exists(path):
            with open(path) as handle:
                returned = json.load(handle)
        returned.append(self._task.args.get("data", {}))
        with open(path, "w") as handle:
            json.dump(returned, handle)
        result["changed"] = False
        return result

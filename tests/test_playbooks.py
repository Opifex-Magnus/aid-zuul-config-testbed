"""The release's playbooks under real Ansible, as Zuul 14.3.0 runs them (ansible-core 2.18, requirements.txt):
playbooks/release/credentials.yaml with the real helper against a stand-in for GitHub (stub_github.py), and
playbooks/secrets/guard.yaml. Their command tasks run Zuul's own command plugin, as its executor installs it
(zuul-requirements.txt; AIDepartment#1542): it keeps none of a no_log task's output, which plain Ansible keeps. Zuul's
zuul_return is a stand-in (plugins/action/zuul_return.py) that records what a playbook returns.

credentials.yaml is copied unchanged into a directory of the test's own. Beside it is a shim of the helper's name
that runs the real helper with --api pointed at the stand-in, so the trusted playbook needs no setting for it.
"""

import glob
import importlib.util
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


# Zuul 14.3.0's plugins for Ansible 11, its default, as installed by zuul-requirements.txt.
ZUUL = importlib.util.find_spec("zuul")
ZUUL_PLUGINS = ZUUL and os.path.join(os.path.dirname(ZUUL.origin), "ansible", "11")


def zuul_plugins(work):
    """Zuul's Ansible plugins, set up as its executor sets them up (zuul/lib/ansible.py, copyAnsibleFiles): its
    zuul/ansible/11/ copied to <dir>/zuul/ansible/, with <dir> on PYTHONPATH, so that the plugins import zuul.ansible.
    Its zuul_return is replaced by the stand-in. Returns the environment that makes Ansible use them as the executor's
    ansible.cfg does (writeAnsibleConfig, runAnsible), the build's log going to <work>/job-output.txt.

    Zuul's callbacks matter: with them loaded, as on the executor, ansible.builtin.command runs Zuul's command action,
    which a no_log task's output does not get past. Without them it runs Ansible's own, and Zuul's module then fails
    for want of a zuul_log_id."""
    root = os.path.join(work, "zuul-ansible")
    plugins = os.path.join(root, "zuul", "ansible")
    shutil.copytree(ZUUL_PLUGINS, plugins, ignore=shutil.ignore_patterns("__pycache__"))
    for package in (os.path.join(root, "zuul"), plugins):
        open(os.path.join(package, "__init__.py"), "a").close()
    shutil.copyfile(os.path.join(HERE, "plugins", "action", "zuul_return.py"),
                    os.path.join(plugins, "action", "zuul_return.py"))
    sys.path.insert(0, root)
    try:
        from zuul.ansible import logconfig
    finally:
        sys.path.remove(root)
        for name in [n for n in sys.modules if n == "zuul" or n.startswith("zuul.")]:
            del sys.modules[name]
    logging_json = os.path.join(work, "logging.json")
    logconfig.JobLoggingConfig(test_log_output_text_file=os.path.join(work, "job-output.txt")).writeJson(logging_json)
    return {"PYTHONPATH": root, "ANSIBLE_ACTION_PLUGINS": os.path.join(plugins, "action"),
            "ANSIBLE_LIBRARY": os.path.join(plugins, "library"),
            "ANSIBLE_MODULE_UTILS": os.path.join(plugins, "library", "module_utils"),
            "ANSIBLE_CALLBACK_PLUGINS": os.path.join(plugins, "callback"), "ANSIBLE_STDOUT_CALLBACK": "zuul_stream",
            "ANSIBLE_FILTER_PLUGINS": os.path.join(plugins, "filter"),
            # A playbook with a secret: Zuul prints no task's arguments.
            "ANSIBLE_DISPLAY_ARGS_TO_STDOUT": "False",
            "ZUUL_JOB_LOG_CONFIG": logging_json, "ZUUL_JOBDIR": work, "ZUUL_JOB_FAILURE_OUTPUT": "[]",
            # Zuul's defaults: 1 GiB of output at most, and stdout and stderr apart only when a job asks.
            "ZUUL_OUTPUT_MAX_BYTES": str(1024 * 1024 * 1024), "ZUUL_ANSIBLE_SPLIT_STREAMS": "False"}


def ansible(playbook, extra_vars, work, inventory="localhost,"):
    """Runs the playbook on localhost with Zuul's plugins; returns its exit status, its output, and what it returned to
    Zuul. Its output is Ansible's, the build's log (job-output.txt, with zuul_json's job-output.json in it), and the
    console log Zuul's command module wrote for each command task (/tmp/console-*.log, which Zuul streams)."""
    vars_file = os.path.join(work, "vars.json")
    with open(vars_file, "w") as handle:
        json.dump(extra_vars, handle)
    returned = os.path.join(work, "returned.json")
    tmp = os.path.join(work, "tmp")
    os.makedirs(tmp, exist_ok=True)
    env = {**{k: v for k, v in os.environ.items() if not k.startswith("ZUUL_")}, **zuul_plugins(work),
           "TEST_ZUUL_RETURN": returned, "ANSIBLE_NOCOLOR": "1", "ANSIBLE_LOCALHOST_WARNING": "False",
           "ANSIBLE_INVENTORY_UNPARSED_WARNING": "False", "ANSIBLE_RETRY_FILES_ENABLED": "False",
           # The job's own temporary directory, as Zuul sets it (local_tmp, TMP).
           "ANSIBLE_LOCAL_TEMP": tmp, "TMP": tmp}
    consoles = set(glob.glob("/tmp/console-*.log"))
    result = subprocess.run([ANSIBLE, "-i", inventory, "-c", "local", playbook, "-e", f"@{vars_file}",
                             "-e", f"ansible_python_interpreter={sys.executable}",
                             # As verbose as Ansible goes: what no_log must keep out shows here first.
                             "-vvv"],
                            capture_output=True, text=True, env=env, timeout=300)
    logs = ""
    for path in [os.path.join(work, "job-output.txt")] + sorted(set(glob.glob("/tmp/console-*.log")) - consoles):
        if os.path.exists(path):
            with open(path) as handle:
                logs += handle.read()
            if path.startswith("/tmp/console-"):
                os.unlink(path)
    data = []
    if os.path.exists(returned):
        with open(returned) as handle:
            data = json.load(handle)
    return result.returncode, result.stdout + result.stderr + logs, data


TOKEN_TASK = "A token of the release's App, made, checked and revoked"


def build_log(output):
    """The build's log (job-output.txt) in the output, by task: {task name: its lines}, in order."""
    tasks = {}
    lines = None
    for line in output.splitlines():
        # "<date> <time> | <text>", as Zuul's zuul_stream writes it.
        if line[:4].isdigit() and " | " in line:
            text = line.split(" | ", 1)[1]
            if text.startswith("TASK ["):
                lines = tasks.setdefault(text[len("TASK ["):].rstrip("]"), [])
            elif lines is not None:
                lines.append(text)
    return tasks


def failed_task(output):
    """The first task the build's log shows failing."""
    for task, lines in build_log(output).items():
        if any(line.startswith(("localhost | ERROR", "localhost | FAILED")) for line in lines):
            return task
    return None


def setUpModule():
    if not ANSIBLE:
        raise RuntimeError("no ansible-playbook: install tests/requirements.txt (README.md, \"Tests\")")
    if not ZUUL_PLUGINS or not os.path.isdir(ZUUL_PLUGINS):
        raise RuntimeError("no Zuul: install tests/zuul-requirements.txt (README.md, \"Tests\")")


class CredentialsPlaybookTest(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.mkdtemp(prefix="credentials-")
        self.addCleanup(shutil.rmtree, self.work)
        self.playbook = os.path.join(self.work, "credentials.yaml")
        shutil.copyfile(PLAYBOOK, self.playbook)

    def run_with(self, stub):
        with open(os.path.join(self.work, "github-app-token.py"), "w") as shim:
            # The shim also records how the key file it is given is kept: key-file.json.
            shim.write("import json, os, runpy, stat, sys\n"
                       "key = sys.argv[sys.argv.index('--key-file') + 1]\n"
                       f"with open({os.path.join(self.work, 'key-file.json')!r}, 'w') as handle:\n"
                       "    json.dump({'path': key, 'mode': stat.S_IMODE(os.stat(key).st_mode),\n"
                       "               'directory_mode': stat.S_IMODE(os.stat(os.path.dirname(key)).st_mode)}, handle)\n"
                       f"sys.argv[1:] += ['--api', {stub.url!r}]\n"
                       f"sys.argv[0] = {HELPER!r}\n"
                       f"runpy.run_path({HELPER!r}, run_name='__main__')\n")
        return ansible(self.playbook, {"aid_release_github": {"app_id": stub_github.APP_ID,
                                                              "private_key": PEM.decode()}}, self.work)

    def assertNoSecret(self, stub, output):
        self.assertNotIn(b"".join(PEM.splitlines()[1:-1]).decode()[:40], output, "the App's key is in the output")
        for secret in stub.secrets():
            self.assertNotIn(secret, output, "a JWT or the token is in the output")

    def assertKeyRemoved(self):
        """The key reached the helper as a file only the job reads, in a directory of its own in the job's TMP, and
        the directory is gone."""
        with open(os.path.join(self.work, "key-file.json")) as handle:
            key = json.load(handle)
        self.assertEqual(os.path.dirname(os.path.dirname(key["path"])), os.path.join(self.work, "tmp"), key)
        self.assertEqual((key["mode"], key["directory_mode"]), (0o600, 0o700), key)
        self.assertFalse(os.path.exists(os.path.dirname(key["path"])), "the App's key was left behind")
        self.assertEqual(glob.glob(os.path.join(self.work, "tmp", "*-release-key")), [])

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
        # The helper's own task in the build's log: its JSON, not Zuul's "Output suppressed".
        token_task = "\n".join(build_log(output)[TOKEN_TASK])
        self.assertIn(github_fingerprint(KEY), token_task, output)
        self.assertNotIn("Output suppressed", token_task)
        self.assertNoSecret(stub, output)
        self.assertKeyRemoved()

    def test_a_refused_grant_fails_the_playbook_and_returns_the_error(self):
        with stub_github.StubGitHub(KEY.public_key(), granted={"contents": "read"}) as stub:
            status, output, returned = self.run_with(stub)
        self.assertNotEqual(status, 0, output)
        # It fails at the helper's task, whose output in the log names what GitHub granted.
        self.assertEqual(failed_task(output), TOKEN_TASK, output)
        self.assertIn("GitHub granted {'contents': 'read'}", output)
        artifact = returned[0]["zuul"]["artifacts"][0]
        self.assertEqual((artifact["name"], artifact["url"]), ("release-credential", "https://github.com/apps/aid-release"))
        self.assertIn("GitHub granted {'contents': 'read'}", artifact["metadata"]["error"])
        self.assertTrue(stub.revoked())
        self.assertNoSecret(stub, output)
        self.assertKeyRemoved()

    def test_a_failed_revocation_fails_the_playbook(self):
        with stub_github.StubGitHub(KEY.public_key(), fail={"revoke": 500}) as stub:
            status, output, returned = self.run_with(stub)
        self.assertNotEqual(status, 0, output)
        self.assertEqual(failed_task(output), TOKEN_TASK, output)
        self.assertIn("DELETE /installation/token: 500", returned[0]["zuul"]["artifacts"][0]["metadata"]["error"])
        self.assertNoSecret(stub, output)
        self.assertKeyRemoved()

    def test_a_key_github_does_not_know_fails_at_the_helper_with_its_answer(self):
        # What the first runs on main would have said (AIDepartment#1542): GitHub's 401, in the log.
        with stub_github.StubGitHub(KEY.public_key(), fail={"installation": 401}) as stub:
            status, output, returned = self.run_with(stub)
        self.assertNotEqual(status, 0, output)
        self.assertEqual(failed_task(output), TOKEN_TASK, output)
        self.assertIn("/installation: 401", output)
        self.assertIn("/installation: 401", returned[0]["zuul"]["artifacts"][0]["metadata"]["error"])
        self.assertNoSecret(stub, output)
        self.assertKeyRemoved()


class ZuulCommandTest(unittest.TestCase):
    """The tests run Zuul's command module, as the executor does: a no_log task registers no output (AIDepartment#1542),
    which plain Ansible would keep. If this fails, the playbook tests no longer show what Zuul would do."""

    def test_a_no_log_command_task_registers_no_output_and_another_does(self):
        work = tempfile.mkdtemp(prefix="zuul-command-")
        self.addCleanup(shutil.rmtree, work)
        playbook = os.path.join(work, "command.yaml")
        with open(playbook, "w") as handle:
            handle.write("- hosts: localhost\n"
                         "  gather_facts: false\n"
                         "  tasks:\n"
                         "    - ansible.builtin.command: {argv: [echo, hidden]}\n"
                         "      register: hidden\n"
                         "      no_log: true\n"
                         "    - ansible.builtin.command: {argv: [echo, shown]}\n"
                         "      register: shown\n"
                         "    - ansible.builtin.assert:\n"
                         "        that: [hidden.rc == 0, hidden.stdout == '', shown.stdout == 'shown']\n")
        status, output, _ = ansible(playbook, {}, work)
        self.assertEqual(status, 0, output)
        self.assertIn("Output suppressed because no_log was given", output)


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


STAGING_RESET = os.path.join(ROOT, "playbooks", "base", "staging-reset.yaml")
STAGING_CLEANUP = os.path.join(ROOT, "playbooks", "base", "cleanup.yaml")
STAGING_NODE = "staging-node.zuul-staging.svc.cluster.local"


class StagingResetTest(unittest.TestCase):
    """staging-reset.yaml and cleanup.yaml (AIDepartment#1655): the staging node's reset, posted to a stand-in for the
    VM's container. Each playbook is copied with the reset's address pointed at the stand-in, the node's name kept."""

    def setUp(self):
        import http.server
        import threading
        self.posts = []
        posts = self.posts

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                posts.append(self.path)
                self.send_response(200)
                self.send_header("content-length", "2")
                self.end_headers()
                self.wfile.write(b"{}")

            def log_message(self, *args):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.shutdown)
        self.work = tempfile.mkdtemp(prefix="staging-")
        self.addCleanup(shutil.rmtree, self.work)

    def run_playbook(self, playbook, hosts):
        with open(playbook) as handle:
            text = handle.read()
        url = f"http://{STAGING_NODE}:8080/reset"
        self.assertEqual(text.count(url), 1, "one reset, at the node's own name")
        copy = os.path.join(self.work, os.path.basename(playbook))
        with open(copy, "w") as handle:
            handle.write(text.replace(url, f"http://127.0.0.1:{self.server.server_port}/reset"))
        inventory = os.path.join(self.work, "inventory.ini")
        with open(inventory, "w") as handle:
            handle.write("localhost ansible_connection=local\n[all]\n")
            for name, host in hosts.items():
                handle.write(f"{name} ansible_host={host} ansible_connection=local\n")
        run = tempfile.mkdtemp(prefix="run-", dir=self.work)
        return ansible(copy, {}, run, inventory=inventory)

    def test_a_job_on_the_staging_node_resets_it_and_waits_for_it(self):
        status, output, _ = self.run_playbook(STAGING_RESET, {"node": STAGING_NODE})
        self.assertEqual(status, 0, output)
        self.assertEqual(self.posts, ["/reset"])
        self.assertIn("Wait for the staging node's new VM", output)

    def test_a_job_on_other_nodes_resets_nothing(self):
        status, output, _ = self.run_playbook(STAGING_RESET, {"node": "node-0.node.zuul-nodes.svc.cluster.local"})
        self.assertEqual(status, 0, output)
        self.assertEqual(self.posts, [])

    def test_cleanup_resets_the_staging_node_only(self):
        status, output, _ = self.run_playbook(STAGING_CLEANUP, {"node": STAGING_NODE, "other": "node-0.node.zuul-nodes.svc.cluster.local"})
        self.assertEqual(status, 0, output)
        self.assertEqual(self.posts, ["/reset"])
        status, output, _ = self.run_playbook(STAGING_CLEANUP, {"other": "node-0.node.zuul-nodes.svc.cluster.local"})
        self.assertEqual(status, 0, output)
        self.assertEqual(self.posts, ["/reset"], "no second reset without the staging node")

    def test_the_base_job_resets_before_the_checkout_and_after_every_job(self):
        with open(os.path.join(ROOT, "playbooks", "base", "pre.yaml")) as handle:
            pre = handle.read()
        self.assertLess(pre.index("ansible.builtin.assert"), pre.index("import_playbook: staging-reset.yaml"))
        self.assertLess(pre.index("import_playbook: staging-reset.yaml"), pre.index("prepare-workspace-git"))
        with open(os.path.join(ROOT, "zuul.d", "jobs.yaml")) as handle:
            jobs = handle.read()
        self.assertIn("      - name: playbooks/base/cleanup.yaml\n        cleanup: true\n", jobs)
        self.assertNotIn("cleanup-run", jobs, "deprecated in Zuul 14.3: a config warning")


if __name__ == "__main__":
    unittest.main()

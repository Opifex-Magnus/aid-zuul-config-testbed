# aid-zuul-config

Zuul's trusted config project for AIDepartment (AIDepartment#1341, #1344, #1346): the pipelines, the base job, and
the jobs with secrets and their secrets (AIDepartment#1351). AIDepartment's own jobs are not here: they are in AIDepartment's
`ci/zuul/`, which says how the two fit together (its `ci/zuul/README.md`).

| Path | What |
| --- | --- |
| [`zuul.d/pipelines.yaml`](zuul.d/pipelines.yaml) | `check`, `check-external` and `gate`, reported as check runs of the App `aid-zuul`, and `hold`, which takes a pull request out of `gate` and reports nothing. AIDepartment's `ci/zuul/gate.md` says how `gate` and `hold` do what Mergify does. `post` after a merge, and `periodic-0200` and `periodic-0300` at 02:00 and 03:00 UTC, all post-review (AIDepartment's `ci/zuul/publish.md`) |
| [`zuul.d/jobs.yaml`](zuul.d/jobs.yaml) | `base`, every job's parent: one build node, a 2-hour timeout, the checkout, and the build's logs kept (AIDepartment deploy/zuul/web.md) |
| [`playbooks/base/`](playbooks/base) | the base job's playbooks, the staging node's reset among them |
| [`zuul.d/project.yaml`](zuul.d/project.yaml) | this repository's own `check`, which validates a pull request here and runs nothing |
| [`zuul.d/release.yaml`](zuul.d/release.yaml) | `aid-release-credentials`, the release's credential check, and where it runs: `post` for AIDepartment |
| `zuul.d/secrets.yaml` | the secrets, each encrypted for this project's key by AIDepartment's `deploy/zuul/encrypt-secret.sh` |
| [`playbooks/secrets/guard.yaml`](playbooks/secrets/guard.yaml) | the pre-run of every job with a secret |
| [`playbooks/release/`](playbooks/release) | the release's credential check: a token of the App `aid-release`, made, checked and revoked |
| [`zuul.d/publish.yaml`](zuul.d/publish.yaml) | what publishes from AIDepartment's `main` (AIDepartment#1350): `aid-publish-apps`, `aid-release` and `aid-publish-images` for `post`, on the post node, and `aid-nightly-report` for the periodic pipelines. They run for AIDepartment from step 4 of AIDepartment's `ci/zuul/publish.md`, "Turning it on", and publish nothing while its `ci/publisher` names Actions |
| [`playbooks/publish/`](playbooks/publish) | their stages: AIDepartment's script for the job, one stage per playbook, each with only the credentials it uses (`stage.yaml`) |
| [`playbooks/nightly/`](playbooks/nightly) | the nightly's result, to AIDepartment's `ci-nightly` issue |
| [`tests/`](tests) | the tests of the release's credential check, of the guard and of the staging node's reset ([Tests](#tests)) |

## Trusted

This repository is the security boundary of AID's CI (Kristóf's decision on AIDepartment#1346): what is in it decides
what every job may do and see. Only a config project may define pipelines, a job with no parent and the providers of
build nodes; its playbooks run outside the sandbox that untrusted playbooks get, and see the secrets it defines. So
Zuul never runs a proposed change to it: a pull request here is checked for configuration errors, and takes effect
once it is merged, when Zuul loads `main` again.

AIDepartment is an untrusted project: a pull request there that adds or changes a job runs it on itself, but it can
define no pipeline and no base job, and Zuul refuses either with a configuration error.

## Changing it

- **Never merge here yourself, and never push to `main`.** `main` is protected: a change is a pull request, reviewed
  by the other bot, and merged by the orchestrator. Nothing merges into it automatically, Zuul included: an approved
  pull request here enters `gate`, which has no jobs for it, so `aid/gate` goes from queued to skipped and nothing
  merges (AIDepartment#1660).
- **Test it first on AIDepartment's test bed** (its `ci/zuul/README.md`, "The test bed"): it runs Zuul with a copy of
  this repository's branch as the config project and AIDepartment's jobs as the untrusted project, and checks that the
  layout loads and that AIDepartment's pull requests still run speculatively. Link the run in the pull request.
- Run the tests ([Tests](#tests)) for a change to a playbook or the helper beside it, and put their output in the pull
  request. Nothing runs them for you: Zuul's `check` here only loads the configuration.
- Zuul's `check` on the pull request reports a configuration error, if any, before the merge.
- After the merge, `curl <zuul-web>/api/tenant/aid/config-errors` is empty if it loaded.

## The build node

`zuul.d/providers.yaml` allocates disposable Kubernetes pods for `aid-check-pod-node`, `aid-gate-pod-node` and `aid-post-pod-node`
(AIDepartment#1698). Each provider has one pod slot, `reuse: false` and a separate connection/namespace:
`aid-check-pods` / `zuul-nodes`, `aid-gate-pods` / `zuul-gate`, and `aid-post-pods` / `zuul-post`. Provider and label names are distinct from the retired static nodes (AIDepartment#1754). Post-review publishing stays on the post identity and never uses the PR node's farm credential.

AIDepartment's pinned launcher adds fixed-namespace support to Zuul 14.3's Kubernetes driver. The admin-owned
`PodTemplate/job-node` supplies the checked image, resource budget, security, mounts and service account. The
provider's `spec: {}` satisfies the upstream label schema; the launcher never uses it as a job-controlled pod spec.
The launcher can create/delete pods and read the template in the three bounded namespaces, but cannot create
namespaces, modify policy/RBAC/templates, or read Secrets. The namespace quotas admit one pod each; jobs queue in
Zuul. Pods and their homes/processes are deleted after every job. Shared caches are read-only to jobs.

The Docker staging VM stays on `aid-nodes`' static provider as `aid-staging-node`: the base job resets its snapshot
before and after each job (AIDepartment#1655). It does not share Kubernetes nodes or identities.

Coordinate deployment with AIDepartment's `ci/zuul/disposable-nodes.md`; only clean, merged main commits may be
applied to live infrastructure. Do not activate these providers until the launcher, fixed connections, templates
and RBAC are deployed. Before a driver switch, pause admissions, drain jobs and retire retained static
requests/nodes under Zuul coordination locks, using the static lifecycle after confirming no owner and no static pod.
Scaling a controller to zero is insufficient. Never alias the retired labels or send a StaticProviderNode through
Kubernetes cleanup. AID's runbook records the exact #1754 incident IDs, ownership checks and original-range retry.
Keep config #31 and AID #1756 held for the orchestrator: merge config first, then admit AID's updated gate nodeset;
old AID gate-image jobs must remain drained between merges. Capture green check/gate and post publication plus
fresh pod UID/deletion evidence on #1754/#1698.

`check-external` is blocked by the base job's trusted pre-run until a separate credential-free execution boundary
exists. It fails before checkout and before any untrusted pre/run/post/cleanup playbook, and tells Zuul not to retry.
The shared pod's raw projected token is readable by its job user; a conditional credential helper cannot secure that
pipeline. Do not remove the guard while external jobs could use `aid-check-pod-node`. The standard Compose test bed seeds a
fake readable farm token and proves that all four malicious hooks run in `check` and none run in `check-external`.

The executor's existing SSH key remains in its Secret; node templates mount each namespace's authorized key
and SSH host key. Only staging keeps its static provider host-key pin. The coordinated launcher overrides the Kubernetes label's kubectl default to enable SSH host-key checking.
Its real nodescan must connect and populate the executor's known_hosts before the job starts; inventory never
sets `StrictHostKeyChecking=false` for these nodes. Private keys remain in AID's keychain/Kubernetes Secrets.

## AID events (#1349)

`aid-events` is the native MQTT connection in AIDepartment’s common Zuul configuration. Each active pipeline reports
enqueue, start, terminal and dequeue actions to `aid/zuul/reports` at QoS 1, alongside its existing forge reporters.
It sends no returned job data. Provision the private broker, scheduler mTLS route and the AID subscriber first
(AIDepartment’s `deploy/zuul/events/README.md`); then merge this configuration and full-reconfigure. There is no job
secret involved. The adapter confirms a gate success with the finalized SQL record before announcing a merge: the
MQTT success report alone precedes Zuul’s merge confirmation.

AIDepartment#1526: the first merge of these reporters (#8) preceded the connection, and the running scheduler
unloaded `check`, `check-external` and `gate`. `aid/check` is the running scheduler's own load of a pull request's
configuration, so it must pass on the pull request before it merges. AIDepartment's `deploy/zuul/protect-config.sh`
makes `main` require it (pending: ThunderBox's step 11a).

## Jobs with secrets

AIDepartment's `deploy/zuul/job-secrets.md` has the model: each secret's plaintext is in AID's keychain, and jobs get
it only as a secret here, encrypted for this project's key. A secret is defined only here, never in AIDepartment, and a
job that names one:

- is defined here, in a file of `zuul.d/` with only such jobs and where they run (the test bed leaves those files out);
- is `final: true` and `post-review: true`, with `allowed-projects`;
- runs with `nodeset: {nodes: []}`, or on a node of its own, never `aid-check-pod-node`: the publishing jobs run on `aid-post-pod-node`
  (AIDepartment's `deploy/zuul/post-node/`), which only they name;
- starts with `pre-run: playbooks/secrets/guard.yaml`, and sets `secret_pipelines` and `secret_projects` in its `vars`.

To add or rotate a secret, encrypt it from the keychain with `deploy/zuul/encrypt-secret.sh --expect-key <the key in
job-secrets.md>`, and replace its stanza in `zuul.d/secrets.yaml` by a pull request. Zuul's `check` here decrypts it
as it loads the proposed configuration, so a stanza for another key is reported there. After the merge, the job's next
run uses it.

## Tests

[`tests/`](tests) holds the tests of the trusted code that handles a secret. They run under real Ansible, as Zuul
14.3.0 runs it (ansible-core 2.18), with Zuul's own Ansible plugins set up as its executor sets them up: its `command`
module and its callbacks, which write the build's log. They run against a stand-in for GitHub's API, with a key made
for the run. No credential is needed and nothing reaches GitHub.

Zuul's `command` module keeps none of a `no_log` task's output, not even for `register` (AIDepartment#1542): a task
whose output a playbook reads must have no secret in its arguments, and so no `no_log`. Plain Ansible keeps it, which
is why the tests run Zuul's plugins.

- `test_github_app_token.py` runs `playbooks/release/github-app-token.py`, with the key on stdin or in a file
  (`--key-file`, as the playbook gives it). It checks:
  - the JWT (RS256, the App's ID, at most ten minutes);
  - the token asked for: AIDepartment alone, `contents: write`;
  - a refusal of a lesser or wider grant, another repository or none listed, a failed read, a refused token, no
    installation, another key, a key that is not RSA, a missing key file, and an App ID that is not a number;
  - revocation after a success and after each refusal, and a failed revocation failing the check;
  - that no key, JWT or token reaches its output.
- `test_playbooks.py` runs `playbooks/release/credentials.yaml` at Ansible's highest verbosity. It checks the build's
  artifact `release-credential`; the helper's output in the build's log; the playbook's failure at the helper's task,
  with GitHub's answer in the log, when the check fails; the key's file, `0600` in a directory of its own in the job's
  `TMP`, removed afterwards; and that the key, a JWT or the token reaches neither Ansible's output, nor the build's log,
  nor the console log. It also runs `playbooks/secrets/guard.yaml`: a merged ref in its pipeline and for its project
  passes, and a change, another pipeline or another project fails without a retry. And it checks that its Ansible is
  Zuul's: a `no_log` command task registers no output. And it runs `playbooks/base/staging-reset.yaml` and `cleanup.yaml`
  against a stand-in for the staging node's reset: a job with the staging node posts one reset and waits for the node, a
  job without it posts none, and the base job resets after the guard, before the checkout, and in its last post-run, marked `cleanup`. `zuul_return` is a stand-in (`tests/plugins/action/`) that
  records what a playbook returns.

- `test_publish.py` runs each stage of `playbooks/publish/` the same way, with a stand-in for AIDepartment's stage
  script that records what reached it: each stage's App token with only its permissions, or none; Zuul's pusher token
  in `push` alone; both `0600` on the node, and gone, the App's revoked, when the stage ends, after a failure too; a
  `publish_endpoints` without a value failing before anything; and `report` only after a failure. It runs
  `playbooks/nightly/report.py`'s steps against stand-ins for Zuul's API and GitHub's issues: `decide` lets through
  only the pipeline whose time is 04:00 in Madrid, across both changes of the clock and after a wait past the other
  pipeline's time, and only while AIDepartment's `ci/publisher` names Zuul; `wait` reads Zuul's buildset API (the
  stand-in refuses the builds API's filters, as Zuul 14.3.0 does), for over an hour and to its deadline, with no
  credential; `report` closes the open `ci-nightly` issue on a pass, and opens it or comments on it otherwise. And
  `playbooks/nightly/report.yaml` under Zuul's Ansible: its App token is made only after `wait`, and revoked.

ansible-core 2.18 runs on Python 3.11 to 3.13:

```sh
python3.12 -m venv /tmp/cfg-tests && /tmp/cfg-tests/bin/pip install -r tests/requirements.txt
/tmp/cfg-tests/bin/pip install --no-deps -r tests/zuul-requirements.txt
PATH=/tmp/cfg-tests/bin:$PATH python -I -m unittest discover -s tests -v
```

For the disposable providers, run the schema and real SSH verification regression together, using the coordinated
AID checkout and a Python environment with the full `zuul==14.3.0` runtime:

```sh
python tests/check_providers.py --launcher /path/to/AIDepartment/deploy/zuul/server/launcher
```

This constructs actual provider labels from this YAML, negotiates SSH against a local fixture, and checks the real
launcher and executor inventory paths. No cluster or publishing credentials are used. Deploy this launcher fix
before activating the new provider connections; a schema-only pass cannot establish server authentication.

The same checker runs `provider_identity.py`: retained real `StaticProviderNode` records cannot satisfy the
new requests in Zuul's actual launcher filter or resolve to the Kubernetes provider by canonical name. It
isolates the provider and label barriers, verifies fresh pod allocations still match, and exercises drained
static cleanup with the upstream static driver. It also checks base/post/gate nodesets and the Compose fixture
against the coordinated AID checkout. An old provider name or label makes the regression fail.

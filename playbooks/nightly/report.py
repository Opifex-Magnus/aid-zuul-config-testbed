"""The nightly's result, to AIDepartment's ci-nightly issue (AIDepartment#1350; its ci/zuul/publish.md, "The nightly").

aid-nightly-report runs beside AIDepartment's aid-nightly in periodic-0200 and periodic-0300, on the executor. It does
what nightly.yml's report job does on GitHub Actions (AIDepartment's ci/nightly.mjs report), for the same issue: closes
it when the nightly passed, otherwise comments on it or opens it. In three steps, of which only the last has a
credential, made just before it (playbooks/nightly/report.yaml), since an App's token lasts an hour and the wait two:

    python3 report.py decide --pipeline P --publisher-file F
        whether this nightly is reported here: its pipeline's time is 04:00 in Madrid, as aid-nightly's own script
        lets through (one pipeline per time, so a run that waited past the other time keeps its decision), and
        AIDepartment's ci/publisher names Zuul. Prints {"report": true|false, ...}.
    python3 report.py wait --zuul URL --tenant T --buildset UUID [--wait SECONDS]
        aid-nightly's result in this buildset, from Zuul's buildset API. Prints {"result": "SUCCESS"|...|null}.
    python3 report.py report --result R --build-url URL --token-file F
        the result to the issue. An empty R is a nightly that had no result in time.

Standard library only. Reaches api.github.com through the environment's https_proxy, as the executor does.
"""

import argparse
import datetime
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.github.com"
REPOSITORY = "kristofk/AIDepartment"
LABEL = "ci-nightly"
TITLE = "Nightly CI failed"
JOB = "aid-nightly"
# The periodic pipelines (zuul.d/pipelines.yaml), each at one hour, UTC: Zuul's timer has no time zone.
PIPELINES = {"periodic-0200": 2, "periodic-0300": 3}


class Failure(Exception):
    pass


def last_sunday(year, month):
    day = datetime.datetime(year, month + 1, 1, tzinfo=datetime.timezone.utc) - datetime.timedelta(days=1)
    return day - datetime.timedelta(days=(day.weekday() + 1) % 7)


def madrid_offset(instant):
    """Madrid's offset from UTC, in hours: CEST (2) from the last Sunday of March to the last Sunday of October, at
    01:00 UTC both, as the EU's rule has it since 1996; CET (1) otherwise. No tz database needed on the executor."""
    start = last_sunday(instant.year, 3).replace(hour=1)
    end = last_sunday(instant.year, 10).replace(hour=1)
    return 2 if start <= instant < end else 1


def fired(pipeline, now):
    """When the pipeline's timer fired: its hour, the latest at or before now (a run waits hours at most, not a day)."""
    if pipeline not in PIPELINES:
        raise Failure(f"pipeline {pipeline!r} is none of {', '.join(PIPELINES)}")
    today = now.replace(hour=PIPELINES[pipeline], minute=0, second=0, microsecond=0)
    return today if today <= now else today - datetime.timedelta(days=1)


def is_madrid_four(instant):
    return (instant + datetime.timedelta(hours=madrid_offset(instant))).hour == 4


def call(method, url, token=None, body=None, ok=(200, 201)):
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "aid-zuul-nightly-report"}
    if token:
        headers |= {"Authorization": f"Bearer {token}", "X-GitHub-Api-Version": "2022-11-28"}
    request = urllib.request.Request(url, method=method, headers=headers,
                                     data=None if body is None else json.dumps(body).encode())
    if body is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            status, text = response.status, response.read().decode()
    except urllib.error.HTTPError as error:
        status, text = error.code, ""
    except urllib.error.URLError as error:
        raise Failure(f"{method} {urllib.parse.urlsplit(url).path} failed: {error.reason}")
    if status not in ok:
        raise Failure(f"{method} {urllib.parse.urlsplit(url).path}: {status}")
    return json.loads(text) if text else None


def nightly_result(zuul, tenant, buildset, deadline, interval=60, sleep=time.sleep, clock=time.time):
    """aid-nightly's result in the buildset (Zuul's GET /api/tenant/<tenant>/buildset/<uuid>, whose builds each have
    job_name and result); None when it has none by the deadline. A buildset Zuul has not stored yet is waited for."""
    url = f"{zuul.rstrip('/')}/api/tenant/{urllib.parse.quote(tenant)}/buildset/{urllib.parse.quote(buildset)}"
    while True:
        try:
            found = call("GET", url) or {}
        except Failure as error:
            if not str(error).endswith(": 404"):
                raise
            found = {}
        done = [build for build in found.get("builds", []) if build.get("job_name") == JOB and build.get("result")]
        if done:
            return done[-1]["result"]
        if clock() + interval > deadline:
            return None
        sleep(interval)


def report(result, link, token, api=API, today=None):
    """Closes the open ci-nightly issues on SUCCESS, else comments on the newest or opens one. Returns what it did."""
    repo = f"{api}/repos/{REPOSITORY}"
    issues = call("GET", f"{repo}/issues?" + urllib.parse.urlencode({"labels": LABEL, "state": "open", "per_page": 100}), token)
    issues = sorted((issue for issue in issues if "pull_request" not in issue), key=lambda issue: -issue["number"])
    if result == "SUCCESS":
        for issue in issues:
            call("POST", f"{repo}/issues/{issue['number']}/comments", token, {"body": f"Passed in {link}"})
            call("PATCH", f"{repo}/issues/{issue['number']}", token, {"state": "closed", "state_reason": "completed"})
        return "closed" if issues else "none", [issue["number"] for issue in issues]
    date = (today or datetime.datetime.now(datetime.timezone.utc)).strftime("%Y-%m-%d")
    body = "\n".join([
        f"The nightly full run on Zuul of {date} (UTC) {f'ended with `{result}`' if result else 'has no result after the job allowed for it'}: {link}",
        "",
        "Its log says which target failed. The nightly on Zuul is Bazel's build and test of everything; the rest of CI "
        "runs on GitHub Actions until Zuul has it (AIDepartment's ci/zuul/publish.md, \"The nightly\").",
        "",
        "This issue closes when a nightly run passes (aid-zuul-config's aid-nightly-report).",
    ])
    if issues:
        call("POST", f"{repo}/issues/{issues[0]['number']}/comments", token, {"body": body})
        return "commented", [issues[0]["number"]]
    created = call("POST", f"{repo}/issues", token, {"title": TITLE, "body": body, "labels": [LABEL]})
    return "created", [created["number"]]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("step", choices=["decide", "wait", "report"])
    parser.add_argument("--pipeline", help="decide: the pipeline this runs in")
    parser.add_argument("--publisher-file", help="decide: AIDepartment's ci/publisher, in the job's checkout")
    parser.add_argument("--zuul", help="wait: zuul-web's address, from the executor")
    parser.add_argument("--tenant", help="wait")
    parser.add_argument("--buildset", help="wait")
    parser.add_argument("--wait", type=int, default=6600, help="wait: seconds to wait for aid-nightly's result")
    parser.add_argument("--result", help="report: aid-nightly's result, empty for none")
    parser.add_argument("--build-url", help="report: the nightly's buildset page, for the issue")
    parser.add_argument("--token-file", help="report: the App's token, with issues: write")
    parser.add_argument("--now", help=argparse.SUPPRESS)
    parser.add_argument("--api", default=API, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.step == "decide":
        now = datetime.datetime.fromisoformat(args.now) if args.now else datetime.datetime.now(datetime.timezone.utc)
        instant = fired(args.pipeline, now)
        if not is_madrid_four(instant):
            return {"report": False, "why": f"{args.pipeline}'s timer of {instant:%Y-%m-%dT%H:%MZ} is not 04:00 in Madrid"}
        with open(args.publisher_file) as handle:
            publisher = handle.read().strip()
        if publisher != "zuul":
            return {"report": False, "why": f"ci/publisher names {publisher!r}: {publisher} reports the nightly"}
        return {"report": True, "why": f"{args.pipeline}'s timer of {instant:%Y-%m-%dT%H:%MZ} is 04:00 in Madrid"}
    if args.step == "wait":
        return {"result": nightly_result(args.zuul, args.tenant, args.buildset, time.time() + args.wait)}
    with open(args.token_file) as handle:
        token = handle.read().strip()
    action, issues = report(args.result or None, args.build_url, token, api=args.api.rstrip("/"))
    return {"result": args.result or None, "action": action, "issues": issues}


if __name__ == "__main__":
    try:
        print(json.dumps(main(), sort_keys=True))
    except (Failure, OSError, ValueError, KeyError) as error:
        print(json.dumps({"error": f"{type(error).__name__}: {error}"}))
        sys.exit(1)

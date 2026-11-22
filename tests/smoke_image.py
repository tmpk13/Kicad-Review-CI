#!/usr/bin/env python3
"""Smoke test of the built image (mise run image-smoke): one review job through the relay
in workerd, the real runner and podman, the real container and Claude Code.

The Anthropic API is a stand-in that answers with a single Write call creating
/out/report.md, so the run costs nothing; Apps Script is a stand-in too. Everything
else is real: the clone from GitHub, the KiCad library clone, the kicad-cli checks,
Claude Code reaching the API through the relay with its job token, the .docx copy.

    smoke_image.py [--repo oresat/oresat-batteries] [--sha SHA] [--path oresat-battery-card]
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def get(url: str, token: str | None = None) -> bytes:
    req = urllib.request.Request(url, headers={"authorization": f"Bearer {token}"} if token else {})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default="oresat/oresat-batteries")
    ap.add_argument("--sha", default="1808a28fe4fbd7c2274a3bf5320b765e37510282")
    ap.add_argument("--ref", default="refs/heads/review")
    ap.add_argument("--path", default="oresat-battery-card")
    ap.add_argument("--image", default="localhost/hw-review:latest")
    args = ap.parse_args()

    bun = shutil.which("bun")
    if not bun:
        raise SystemExit("bun is needed (mise install)")
    serve = subprocess.Popen([bun, str(ROOT / "relay/test/serve.ts"), "--write-report", "/out/report.md"],
                             cwd=ROOT / "relay", stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, text=True)
    out, stdin = serve.stdout, serve.stdin
    assert out and stdin
    info = json.loads(out.readline())
    relay, upstream = info["relay"], info["upstream"]
    relay_log: list[str] = []
    threading.Thread(target=lambda: relay_log.extend(out), daemon=True).start()
    work = Path(tempfile.mkdtemp(prefix="hw-review-smoke-"))
    failures: list[str] = []

    def check(ok: bool, what: str) -> None:
        print(("ok   " if ok else "FAIL ") + what, flush=True)
        if not ok:
            failures.append(what)

    try:
        claims = {"repository": args.repo, "repository_owner": args.repo.split("/")[0], "sha": args.sha,
                  "ref": args.ref, "run_id": str(int(time.time()))}
        token_url = f"{upstream}/oidc?" + urllib.parse.urlencode({"audience": "hw-review-relay", **claims})
        token = json.loads(get(token_url))["value"]
        req = urllib.request.Request(f"{relay}/v1/jobs", method="POST",
                                     data=json.dumps({"path": args.path, "budget_usd": 1}).encode(),
                                     headers={"authorization": f"Bearer {token}", "content-type": "application/json"})
        job = json.loads(urllib.request.urlopen(req, timeout=30).read())["job"]
        print(f"job {job['id']} queued; work dir {work}", flush=True)

        cfg = work / "runner.toml"
        cfg.write_text(f'relay_url = "{relay}"\nrunner_name = "smoke"\nimage = "{args.image}"\n'
                       f'work_root = "{work / "jobs"}"\nheartbeat_seconds = 5\nmax_runtime_minutes = 30\n'
                       '[podman]\nnetwork = "host"\nmemory = "6g"\n')
        env = dict(os.environ, HW_REVIEW_RUNNER_TOKEN="runner-secret-token")
        started = time.time()
        r = subprocess.run([sys.executable, str(ROOT / "runner/review_runner.py"), "--config", str(cfg), "--once"],
                           env=env, text=True, timeout=40 * 60)
        print(f"runner exit {r.returncode} after {time.time() - started:.0f} s", flush=True)

        token = json.loads(get(token_url))["value"]
        done = json.loads(get(f"{relay}/v1/jobs/{job['id']}", token))["job"]
        jobdir = work / "jobs" / job["id"]
        summary = done.get("summary") or {}
        check(done["status"] == "done", f"job status is done (got {done['status']}: {done.get('error')})")
        report = get(f"{relay}/v1/jobs/{job['id']}/report", token).decode() if done.get("has_report") else ""
        check("Smoke-test review" in report, "report written by Claude Code's Write tool reached the relay")
        check("## Review run" in report and args.sha in report, "run record appended to the report")
        docx = jobdir / "out/report.docx"
        check(docx.exists() and docx.read_bytes()[:2] == b"PK", "pandoc made report.docx")
        claude = summary.get("claude") or {}
        check(claude.get("subtype") == "success", f"Claude Code finished with success (got {claude})")
        libs = summary.get("libraries") or {}
        check(all("path" in v for v in libs.values()) and bool(libs), f"KiCad libraries cloned: {sorted(libs)}")
        for c in summary.get("checks") or []:
            bad = [s["step"] for s in c["steps"] if s["exit"] != 0]
            check(not bad, f"checks for {c['project']} ran clean (failed steps: {bad})")
            print(f"     ERC {c['erc'] and c['erc']['violations']['by_severity']}, "
                  f"DRC {c['drc'] and c['drc']['violations']['by_severity']}", flush=True)
        check(bool(summary.get("checks")), "at least one KiCad project was checked")
        seen = json.loads(get(f"{upstream}/_requests"))
        calls = [x for x in seen if x["path"].startswith("/v1/")]
        check(bool(calls) and all(x["headers"].get("x-api-key") == "sk-ant-real-key" for x in calls),
              f"{len(calls)} API calls reached the upstream with the relay's key")
        check(not any("hwr_" in json.dumps(x["headers"]) for x in calls), "the job token never left the relay")
        print("     API paths: " + ", ".join(sorted({x['path'] for x in calls})), flush=True)
        uploads = [x for x in seen if x["query"].get("action") == "upload"]
        check(len(uploads) == 1 and uploads[0]["query"]["name"].endswith(".docx"), "runner uploaded the .docx")
        task = (jobdir / "work/task.md").read_text() if (jobdir / "work/task.md").exists() else ""
        check("context-oresat.md" in task or not args.repo.startswith("oresat/"), "OreSat context offered in the task")
    finally:
        stdin.close()
        try:
            serve.wait(timeout=20)
        except subprocess.TimeoutExpired:
            serve.kill()
    print(f"{'PASS' if not failures else 'FAIL'}: work dir kept at {work}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())

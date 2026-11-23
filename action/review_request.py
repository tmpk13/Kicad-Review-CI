#!/usr/bin/env python3
"""GitHub Action step: ask the hw-review relay for a design review of this commit and
wait for the report.

action.yml passes the inputs as INPUT_* environment variables. The job needs
`permissions: id-token: write`: the relay identifies the repository, commit and run from
the GitHub OIDC token, so no secret is stored in the repository. Standard library only.
"""

import base64
import json
import os
import signal
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

TERMINAL = {"done", "failed", "canceled", "superseded", "expired"}
SUMMARY_LIMIT = 900_000  # GitHub caps a step summary at 1 MiB


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def annotate(level: str, message: str) -> None:
    print(f"::{level}::{message.replace(chr(10), ' ')}", flush=True)


def set_output(name: str, value: str) -> None:
    path = env("GITHUB_OUTPUT")
    if path:
        with open(path, "a") as f:
            f.write(f"{name}={value}\n")


class Oidc:
    """GitHub OIDC tokens for the relay, refreshed shortly before they expire."""

    def __init__(self, audience: str):
        self.url = env("ACTIONS_ID_TOKEN_REQUEST_URL")
        self.request_token = env("ACTIONS_ID_TOKEN_REQUEST_TOKEN")
        if not self.url or not self.request_token:
            raise SystemExit("::error::No OIDC token available. Give the job `permissions: id-token: write`.")
        self.audience = audience
        self.token, self.expires = "", 0.0

    def get(self) -> str:
        if self.token and time.time() < self.expires - 60:
            return self.token
        sep = "&" if "?" in self.url else "?"
        req = urllib.request.Request(f"{self.url}{sep}audience={urllib.parse.quote(self.audience)}",
                                     headers={"authorization": f"bearer {self.request_token}"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            self.token = json.loads(resp.read())["value"]
        payload = self.token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        self.expires = float(claims.get("exp", time.time() + 240))
        return self.token


class Relay:
    def __init__(self, base: str, oidc: Oidc):
        self.base, self.oidc = base.rstrip("/"), oidc

    def call(self, method: str, path: str, body: object = None, timeout: float = 60) -> tuple[int, bytes]:
        data = json.dumps(body).encode() if body is not None else None
        headers = {"authorization": f"Bearer {self.oidc.get()}", "user-agent": "hw-review-action"}
        if data is not None:
            headers["content-type"] = "application/json"
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            return 0, json.dumps({"error": str(e)}).encode()

    def json(self, method: str, path: str, body: object = None) -> tuple[int, dict]:
        status, raw = self.call(method, path, body)
        try:
            value = json.loads(raw) if raw else {}
        except ValueError:
            value = {"error": raw[:300].decode(errors="replace")}
        return status, value if isinstance(value, dict) else {}

    def retry(self, method: str, path: str, body: object = None, attempts: int = 5) -> tuple[int, dict]:
        """Retry network errors and 5xx answers; 4xx answers are final."""
        status, got = 0, {}
        for i in range(attempts):
            status, got = self.json(method, path, body)
            if 0 < status < 500:
                break
            time.sleep(min(60, 5 * 2**i))
        return status, got


def check_relay_url(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    local = parsed.hostname in ("127.0.0.1", "localhost", "::1")
    if parsed.scheme != "https" and not (parsed.scheme == "http" and local):
        raise SystemExit("::error::relay-url must be an https:// URL (set the HW_REVIEW_RELAY_URL repository variable)")
    return url.rstrip("/")


def describe(job: dict) -> str:
    if job["status"] == "queued":
        return f"queued, position {job.get('queue_position')}"
    return f"{job['status']}: {job.get('progress') or ''}".strip(": ")


def write_report(job: dict, report: str) -> Path:
    summary = job.get("summary") or {}
    name = summary.get("report_name") or f"review-{job['sha'][:7]}"
    folder = Path(env("RUNNER_TEMP", "/tmp")) / "hw-review"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{name}.md"
    path.write_text(report)
    return path


def step_summary(job: dict, report: str | None) -> None:
    path = env("GITHUB_STEP_SUMMARY")
    if not path:
        return
    s = job.get("summary") or {}
    claude = s.get("claude") or {}
    o = job["options"]
    lines = [f"### Hardware design review: {job['status']}", "",
             f"- Job `{job['id']}`: `{o['path']}` at `{job['sha'][:12]}`, {o['model']} at effort {o['effort']}"]
    if isinstance(claude.get("total_cost_usd"), (int, float)):
        lines.append(f"- Spend ${claude['total_cost_usd']:.2f} of ${o['budget_usd']:.2f}, "
                     f"{claude.get('num_turns')} turns, {(claude.get('duration_ms') or 0) / 60000:.0f} min")
    drive = s.get("drive_report") or {}
    if drive.get("url"):
        lines.append(f"- Shared-drive copy: [{drive['name']}]({drive['url']})")
    elif drive.get("error"):
        lines.append(f"- Shared-drive upload failed: {drive['error']}")
    if job.get("error"):
        lines.append(f"- Note: {job['error']}")
    text = "\n".join(lines) + "\n\n"
    if report:
        text += report if len(text) + len(report) < SUMMARY_LIMIT else (
            report[: SUMMARY_LIMIT - len(text) - 200] + "\n\n[Report truncated here; the artifact has all of it.]\n")
    with open(path, "a") as f:
        f.write(text + "\n")


def main() -> int:
    relay_url = check_relay_url(env("INPUT_RELAY_URL"))
    relay = Relay(relay_url, Oidc(env("INPUT_AUDIENCE", "hw-review-relay")))
    wait_s = float(env("INPUT_WAIT_MINUTES", "300")) * 60
    # The floor keeps a mistyped input from hammering the relay; tests lower it.
    poll_s = max(float(env("HW_REVIEW_MIN_POLL_SECONDS", "5")), float(env("INPUT_POLL_SECONDS", "60")))
    body = {"path": env("INPUT_PATH", ".") or "."}
    for key in ("model", "effort", "budget_usd"):
        value = env(f"INPUT_{key.upper()}")
        if value and value != "default":
            body[key] = value

    status, got = relay.retry("POST", "/v1/jobs", body)
    if status not in (200, 201):
        annotate("error", f"The relay refused the review request (HTTP {status}): {got.get('error')}")
        return 1
    job = got["job"]
    set_output("job-id", job["id"])
    print(f"Review job {job['id']}: {describe(job)}", flush=True)
    for old in got.get("superseded") or []:
        print(f"Superseded the queued job {old}.", flush=True)

    def cancel(signum, _frame):
        print(f"Workflow canceled; canceling review job {job['id']}.", flush=True)
        relay.json("POST", f"/v1/jobs/{job['id']}/cancel")
        sys.exit(128 + signum)

    signal.signal(signal.SIGTERM, cancel)
    signal.signal(signal.SIGINT, cancel)

    deadline = time.time() + wait_s
    last = ""
    while job["status"] not in TERMINAL:
        if time.time() > deadline:
            relay.json("POST", f"/v1/jobs/{job['id']}/cancel")
            annotate("error", f"No result within {wait_s / 60:.0f} minutes; the review job was canceled.")
            return 1
        time.sleep(poll_s)
        status, got = relay.retry("GET", f"/v1/jobs/{job['id']}")
        if status != 200:
            annotate("warning", f"Polling the relay failed (HTTP {status}): {got.get('error')}")
            continue
        job = got["job"]
        line = describe(job)
        if line != last:
            print(f"{time.strftime('%H:%M:%S')} {line}", flush=True)
            last = line

    set_output("status", job["status"])
    report = None
    if job.get("has_report"):
        status, raw = relay.call("GET", f"/v1/jobs/{job['id']}/report")
        if status == 200:
            report = raw.decode(errors="replace")
            path = write_report(job, report)
            set_output("report-path", str(path))
            print(f"Report saved to {path}", flush=True)
    drive = (job.get("summary") or {}).get("drive_report") or {}
    if drive.get("url"):
        set_output("drive-url", drive["url"])
    step_summary(job, report)

    if job["status"] == "done":
        if not (job.get("summary") or {}).get("complete", True):
            annotate("warning", f"The review is incomplete: {job.get('error') or 'see the report'}")
        return 0
    if job["status"] == "superseded":
        annotate("notice", f"A newer push superseded this review: {job.get('error')}")
        return 0
    annotate("error", f"Review {job['status']}: {job.get('error') or 'no detail from the relay'}")
    return 1


if __name__ == "__main__":
    sys.exit(main())

import base64
import json
import os
import subprocess
import sys
import time

import pytest

from conftest import ROOT, FakeServer

SCRIPT = ROOT / "action/review_request.py"
JOB_ID = "0e7c0d5e-1111-4222-8333-944455556666"


def fake_jwt() -> str:
    seg = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")
    return f"{seg({'alg': 'none'})}.{seg({'exp': time.time() + 300})}.sig"


class Relay:
    """Fake relay and OIDC issuer. `final` is the job status the polls reach."""

    def __init__(self, final: str, report: str | None = "# Review\n\n**Revision reviewed** - `review` @ `abc`\n",
                 create_status: int = 201):
        self.final, self.report, self.create_status = final, report, create_status
        self.polls = 0
        self.server = FakeServer(self.handle)

    def job(self, status: str) -> dict:
        return {"id": JOB_ID, "repo": "oresat/oresat-batteries", "ref": "refs/heads/review", "sha": "abc1234def",
                "status": status, "progress": "review: Bash: kicad-cli" if status == "running" else None,
                "queue_position": 1 if status == "queued" else None, "error": "budget" if status == "failed" else None,
                "has_report": status == "done" and self.report is not None,
                "options": {"path": "board", "model": "claude-opus-5-5", "effort": "high", "budget_usd": 20.0},
                "summary": {"report_name": "board-review-abc1234", "complete": True,
                            "claude": {"total_cost_usd": 4.2, "num_turns": 31, "duration_ms": 1_800_000},
                            "drive_report": {"name": "board-review-abc1234.docx", "url": "https://drive.example/f"}}}

    def handle(self, method, path, query, headers, body):
        if path == "/oidc":
            assert headers["authorization"] == "bearer request-token"
            assert query["audience"] == "hw-review-relay"
            return 200, {"value": fake_jwt()}
        assert headers["authorization"].startswith("Bearer ")
        if path == "/v1/jobs" and method == "POST":
            if self.create_status != 201:
                return self.create_status, {"error": "repository oresat/x is not on this relay's ALLOWED_REPOS list"}
            return 201, {"job": self.job("queued"), "created": True, "superseded": []}
        if path == f"/v1/jobs/{JOB_ID}":
            self.polls += 1
            return 200, {"job": self.job("running" if self.polls < 2 else self.final)}
        if path == f"/v1/jobs/{JOB_ID}/report":
            return 200, (self.report or "").encode()
        if path == f"/v1/jobs/{JOB_ID}/cancel":
            return 200, {"ok": True, "status": "canceled"}
        return 404, {"error": "not found"}


@pytest.fixture
def run_action(tmp_path):
    servers = []

    def go(relay: Relay, **inputs):
        servers.append(relay.server)
        env = dict(os.environ, INPUT_RELAY_URL=relay.server.url, INPUT_PATH="board", INPUT_POLL_SECONDS="0.2", HW_REVIEW_MIN_POLL_SECONDS="0.1",
                   INPUT_EFFORT="default", INPUT_BUDGET_USD="", ACTIONS_ID_TOKEN_REQUEST_URL=f"{relay.server.url}/oidc?x=1",
                   ACTIONS_ID_TOKEN_REQUEST_TOKEN="request-token", GITHUB_OUTPUT=str(tmp_path / "out.txt"),
                   GITHUB_STEP_SUMMARY=str(tmp_path / "summary.md"), RUNNER_TEMP=str(tmp_path / "rt"),
                   **{f"INPUT_{k.upper()}": v for k, v in inputs.items()})
        p = subprocess.run([sys.executable, str(SCRIPT)], env=env, capture_output=True, text=True, timeout=60)
        outputs = dict(line.split("=", 1) for line in (tmp_path / "out.txt").read_text().splitlines()) \
            if (tmp_path / "out.txt").exists() else {}
        summary = (tmp_path / "summary.md").read_text() if (tmp_path / "summary.md").exists() else ""
        return p, outputs, summary

    yield go
    for s in servers:
        s.stop()


def test_done_saves_report_and_summary(run_action):
    relay = Relay("done")
    p, outputs, summary = run_action(relay)
    assert p.returncode == 0, p.stdout + p.stderr
    assert outputs["job-id"] == JOB_ID and outputs["status"] == "done"
    assert outputs["drive-url"] == "https://drive.example/f"
    assert outputs["report-path"].endswith("board-review-abc1234.md")
    with open(outputs["report-path"]) as f:
        assert "Revision reviewed" in f.read()
    assert "### Hardware design review: done" in summary
    assert "[board-review-abc1234.docx](https://drive.example/f)" in summary
    assert "Spend $4.20 of $20.00, 31 turns, 30 min" in summary
    create = json.loads(relay.server.find("/v1/jobs")[0]["body"])
    assert create == {"path": "board"}  # "default" effort and empty budget leave the relay's defaults
    assert "running: review: Bash: kicad-cli" in p.stdout


def test_superseded_is_a_notice_not_a_failure(run_action):
    p, outputs, _ = run_action(Relay("superseded", report=None))
    assert p.returncode == 0
    assert "::notice::A newer push superseded this review" in p.stdout
    assert "report-path" not in outputs


def test_failed_job_fails_the_step(run_action):
    p, outputs, summary = run_action(Relay("failed", report=None))
    assert p.returncode == 1
    assert "::error::Review failed: budget" in p.stdout
    assert outputs["status"] == "failed"
    assert "### Hardware design review: failed" in summary


def test_refused_request_reports_the_relay_error(run_action):
    p, _, _ = run_action(Relay("done", create_status=403))
    assert p.returncode == 1
    assert "HTTP 403" in p.stdout and "ALLOWED_REPOS" in p.stdout


def test_plain_http_relay_url_is_refused(tmp_path):
    env = dict(os.environ, INPUT_RELAY_URL="http://relay.example.com", ACTIONS_ID_TOKEN_REQUEST_URL="x",
               ACTIONS_ID_TOKEN_REQUEST_TOKEN="y")
    p = subprocess.run([sys.executable, str(SCRIPT)], env=env, capture_output=True, text=True, timeout=30)
    assert p.returncode != 0 and "https://" in (p.stdout + p.stderr)


def test_missing_id_token_permission_is_explained(tmp_path):
    env = {k: v for k, v in os.environ.items() if not k.startswith("ACTIONS_ID_TOKEN")}
    env["INPUT_RELAY_URL"] = "https://relay.example.com"
    p = subprocess.run([sys.executable, str(SCRIPT)], env=env, capture_output=True, text=True, timeout=30)
    assert p.returncode != 0 and "id-token: write" in (p.stdout + p.stderr)

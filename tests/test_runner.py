import base64
import json
import sys

import pytest

from conftest import ROOT, FakeServer, load

runner = load("review_runner", "runner/review_runner.py")

JOB_ID = "0e7c0d5e-1111-4222-8333-944455556666"
JOB = {
    "id": JOB_ID, "repo": "oresat/oresat-batteries", "ref": "refs/heads/review",
    "sha": "1808a28aa1c2b3c4d5e6f708192a3b4c5d6e7f80", "event": "push", "actor": "octocat",
    "run_id": "42", "run_attempt": "1", "status": "running",
    "options": {"path": "oresat-battery-card", "model": "claude-opus-5-5", "effort": "high", "budget_usd": 20,
                "max_runtime_minutes": 180, "drive_folders": 1, "drive_upload": True},
}


class Relay:
    """Fake relay for one job: Drive with one file, a heartbeat that can cancel."""

    def __init__(self, cancel_after: int | None = None):
        self.cancel_after = cancel_after
        self.claimed = False
        self.beats = 0
        self.server = FakeServer(self.handle)

    def handle(self, method, path, query, headers, body):
        if headers.get("authorization") != "Bearer runner-token":
            return 401, {"error": "bad runner token"}
        base = f"/v1/runner/jobs/{JOB_ID}"
        if path == "/v1/runner/claim":
            if self.claimed:
                return 204, None
            self.claimed = True
            return 200, {"job": JOB, "api_token": "hwr_job_token", "anthropic_base_url": "http://relay.invalid/anthropic"}
        if path == f"{base}/heartbeat":
            self.beats += 1
            return 200, {"ok": True, "cancel": self.cancel_after is not None and self.beats > self.cancel_after}
        if path == f"{base}/drive/list":
            return 200, {"ok": True, "files": [
                {"id": "DOCFILE00001", "path": "Battery card/Power budget", "mimeType": "application/vnd.google-apps.document", "size": None},
                {"id": "BIGFILE00001", "path": "Battery card/huge.pdf", "mimeType": "application/pdf", "size": 10**9},
                {"id": "EVILFILE0001", "path": "../../etc/passwd", "mimeType": "text/plain", "size": 3},
            ]}
        if path == f"{base}/drive/file/DOCFILE00001":
            return 200, {"ok": True, "name": "Power budget.md", "exportedAs": "text/markdown",
                         "data": base64.b64encode(b"# Power budget\n").decode()}
        if path == f"{base}/drive/upload":
            return 200, {"ok": True, "id": "NEWFILE00001", "url": "https://drive.example/NEWFILE00001"}
        if path == f"{base}/finish":
            return 200, {"ok": True}
        return 404, {"error": "not found"}

    def finished(self) -> dict:
        return json.loads(self.server.find(f"/v1/runner/jobs/{JOB_ID}/finish")[-1]["body"])


@pytest.fixture
def setup(tmp_path, monkeypatch):
    servers = []

    def make(mode="ok", cancel_after=None):
        relay = Relay(cancel_after)
        servers.append(relay.server)
        state = tmp_path / "podman-state"
        monkeypatch.setenv("FAKE_PODMAN_STATE", str(state))
        monkeypatch.setenv("FAKE_PODMAN_MODE", mode)
        cfg_path = tmp_path / "runner.toml"
        cfg_path.write_text(
            f'relay_url = "{relay.server.url}"\n'
            f'token_file = "{tmp_path / "token"}"\n'
            f'runner_name = "bench"\nwork_root = "{tmp_path / "jobs"}"\n'
            "poll_seconds = 0.2\nheartbeat_seconds = 0.2\n"
            f"[podman]\ncommand = [{json.dumps(sys.executable)}, {json.dumps(str(ROOT / 'tests/fake_podman.py'))}]\n"
            'network = "host"\n')
        (tmp_path / "token").write_text("runner-token\n")
        return relay, cfg_path, state

    yield make
    for s in servers:
        s.stop()


def test_job_runs_end_to_end(setup, tmp_path):
    relay, cfg_path, state = setup("ok")
    assert runner.main(["--config", str(cfg_path), "--once"]) == 0
    done = relay.finished()
    assert done["status"] == "done", done
    assert "Revision reviewed" in done["report"]
    s = done["summary"]
    # Drive files were in place before the container started; the bad ones were skipped.
    assert s["drive_seen"] == ["Battery card/Power budget.md", "_drive-manifest.json"]
    assert s["runner"]["drive"] == {"files": 1, "skipped": 2}
    manifest = json.loads((tmp_path / "jobs" / JOB_ID / "work/drive/_drive-manifest.json").read_text())
    assert [m["status"] for m in manifest] == ["ok", "skipped: over 25 MB", "skipped: unusable path"]
    assert s["drive_report"] == {"name": f"board-review-{JOB['sha'][:7]}.docx", "id": "NEWFILE00001",
                                 "url": "https://drive.example/NEWFILE00001"}
    upload = relay.server.find(f"/v1/runner/jobs/{JOB_ID}/drive/upload")[0]
    assert upload["query"]["name"] == f"board-review-{JOB['sha'][:7]}.docx"
    assert base64.b64decode(upload["body"]) == b"PK\x03\x04fake-docx"
    assert "fake container: review written" in done["log_tail"]
    # The container got the job's token through the environment, never on the command line.
    args = json.loads((state / f"hwr-{JOB_ID[:8]}.args").read_text())
    assert "hwr_job_token" not in " ".join(args)
    for flag in ("--read-only", "--cap-drop=all", "--security-opt=no-new-privileges", "--init"):
        assert flag in args
    job_json = json.loads((tmp_path / "jobs" / JOB_ID / "work/job.json").read_text())
    assert job_json["deadline_epoch"] > 0 and "api_token" not in job_json
    assert not (tmp_path / "jobs" / JOB_ID / "finish-pending.json").exists()


def test_container_failure_is_reported(setup):
    relay, cfg_path, _ = setup("fail")
    runner.main(["--config", str(cfg_path), "--once"])
    done = relay.finished()
    assert done["status"] == "failed"
    assert done["error"] == "kicad-cli crashed"
    assert done["report"] is None
    assert "failing on purpose" in done["log_tail"]


def test_cancel_from_github_stops_the_container(setup):
    relay, cfg_path, state = setup("hang", cancel_after=2)
    runner.main(["--config", str(cfg_path), "--once"])
    done = relay.finished()
    assert done["status"] == "canceled"
    assert (state / f"hwr-{JOB_ID[:8]}.stop").exists()
    assert done["summary"]["runner"]["canceled"] is True


def test_progress_line_comes_from_the_container(tmp_path):
    (tmp_path / "progress.json").write_text(json.dumps({"phase": "review", "message": "Read: /work/x", "turns": 4, "minutes": 2.5}))
    assert runner.read_progress(tmp_path) == "review: Read: /work/x (turns 4, minutes 2.5)"
    assert runner.read_progress(tmp_path / "missing") is None


@pytest.mark.parametrize("raw,expected", [
    ("Battery card/Tests/run.csv", "Battery card/Tests/run.csv"),
    ("./a//b", "a/b"),
    ("../x", None),
    ("a/../../x", None),
    ("", None),
    ("bad\x07name.pdf", "badname.pdf"),
])
def test_safe_relpath(raw, expected):
    got = runner.safe_relpath(raw)
    assert (str(got) if got else None) == expected


def test_config_rejects_unknown_keys(tmp_path, monkeypatch):
    monkeypatch.setenv("HW_REVIEW_RUNNER_TOKEN", "t")
    p = tmp_path / "c.toml"
    p.write_text('relay_url = "https://r"\nbogus = 1\n')
    with pytest.raises(SystemExit, match="unknown config key"):
        runner.load_config(p)
    p.write_text('relay_url = "https://r/"\n[podman]\ncommand = ["x"]\nmemory = "2g"\n')
    cfg = runner.load_config(p)
    assert (cfg.relay_url, cfg.token, cfg.podman, cfg.memory) == ("https://r", "t", ["x"], "2g")


def test_example_config_loads(monkeypatch):
    monkeypatch.setenv("HW_REVIEW_RUNNER_TOKEN", "t")
    cfg = runner.load_config(ROOT / "runner/runner.example.toml")
    assert cfg.relay_url.startswith("https://") and cfg.userns.startswith("keep-id")
    assert "~" not in str(cfg.work_root)

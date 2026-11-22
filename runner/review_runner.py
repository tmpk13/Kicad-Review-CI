#!/usr/bin/env python3
"""Review runner: takes jobs from the relay and runs each in a podman container.

One job at a time. For each job the runner
1. claims it from the relay, which returns the job and a per-job Anthropic API token,
2. downloads the job's shared-drive files through the relay into the job's work dir,
3. runs the review image with podman, sending the relay a heartbeat with progress,
4. uploads the .docx report to the shared drive through the relay,
5. sends the Markdown report, the summary and the log tail to the relay.

The container gets the API token and nothing else: Drive access and the runner token
stay on this host. Standard library only; needs python3 >= 3.11 and podman.

    review_runner.py --config ~/.config/hw-review/runner.toml [--once]
"""

import argparse
import base64
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
import tomllib
import traceback
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

VERSION = "0.1.0"
REPORT_LIMIT = 4_000_000


@dataclass
class Config:
    relay_url: str
    token: str
    runner_name: str = "runner"
    image: str = "localhost/hw-review:latest"
    work_root: Path = Path("~/.local/share/hw-review/jobs")
    poll_seconds: float = 20
    heartbeat_seconds: float = 30
    max_runtime_minutes: float = 180
    keep_jobs: int = 10
    drive_max_file_mb: float = 25
    podman: list = field(default_factory=lambda: ["podman"])
    memory: str = "8g"
    cpus: str = "4"
    pids_limit: int = 4096
    network: str = ""
    userns: str = "keep-id:uid=1000,gid=1000"
    extra_args: list = field(default_factory=list)


def load_config(path: Path) -> Config:
    raw = tomllib.loads(path.read_text())
    podman = raw.pop("podman", {})
    token = os.environ.get("HW_REVIEW_RUNNER_TOKEN", "")
    token_file = raw.pop("token_file", None)
    if not token and token_file:
        token = Path(token_file).expanduser().read_text().strip()
    if not token:
        raise SystemExit("no runner token: set token_file in the config or HW_REVIEW_RUNNER_TOKEN")
    cfg = Config(relay_url=raw.pop("relay_url").rstrip("/"), token=token)
    for key, value in raw.items():
        if not hasattr(cfg, key):
            raise SystemExit(f"unknown config key: {key}")
        setattr(cfg, key, value)
    for key, value in podman.items():
        if key == "command":
            key = "podman"
        if not hasattr(cfg, key):
            raise SystemExit(f"unknown [podman] key: {key}")
        setattr(cfg, key, value)
    cfg.work_root = Path(cfg.work_root).expanduser()
    return cfg


def log(message: str) -> None:
    print(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} {message}", flush=True)


class Relay:
    def __init__(self, cfg: Config):
        self.base = cfg.relay_url
        self.headers = {
            "authorization": f"Bearer {cfg.token}",
            "x-runner-name": cfg.runner_name,
            "user-agent": f"hw-review-runner/{VERSION}",
        }

    def request(self, method: str, path: str, *, body: object = None, data: bytes | None = None,
                content_type: str = "application/json", timeout: float = 60) -> tuple[int, bytes]:
        """One HTTP call. Network failures come back as status 0."""
        headers = dict(self.headers)
        if body is not None:
            data = json.dumps(body).encode()
        if data is not None:
            headers["content-type"] = content_type
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            return 0, str(e).encode()

    def json(self, method: str, path: str, **kw) -> tuple[int, dict]:
        status, raw = self.request(method, path, **kw)
        try:
            value = json.loads(raw) if raw else {}
        except ValueError:
            value = {"error": raw[:300].decode(errors="replace")}
        return status, value if isinstance(value, dict) else {"value": value}


# --- shared drive -------------------------------------------------------------------

def safe_relpath(path: str) -> Path | None:
    """A Drive path as a relative path that cannot leave its directory."""
    parts = []
    for part in path.replace("\\", "/").split("/"):
        part = "".join(c for c in part if c.isprintable()).strip()
        if part in ("", "."):
            continue
        if part == "..":
            return None
        parts.append(part[:150])
    return Path(*parts) if parts else None


def fetch_drive(relay: Relay, job_id: str, dest: Path, max_bytes: int) -> dict:
    dest.mkdir(parents=True, exist_ok=True)
    status, listing = relay.json("GET", f"/v1/runner/jobs/{job_id}/drive/list", timeout=420)
    if status != 200 or not listing.get("ok"):
        return {"files": 0, "error": listing.get("error") or f"drive list failed (HTTP {status})"}
    manifest, fetched, skipped = [], 0, 0
    for f in listing.get("files", []):
        entry = {k: f.get(k) for k in ("id", "path", "mimeType", "size", "modified")}
        rel = safe_relpath(str(f.get("path", "")))
        if rel is None or not f.get("id"):
            entry["status"] = "skipped: unusable path"
            skipped += 1
        elif (f.get("size") or 0) > max_bytes:
            entry["status"] = f"skipped: over {max_bytes // 1_000_000} MB"
            skipped += 1
        else:
            status, got = 0, {}
            for attempt in range(3):
                status, got = relay.json("GET", f"/v1/runner/jobs/{job_id}/drive/file/{f['id']}", timeout=420)
                if status == 200 and got.get("ok"):
                    break
                time.sleep(2 * (attempt + 1))
            if status == 200 and got.get("ok") and isinstance(got.get("data"), str):
                # An exported Google file comes back under its export name (Doc -> .md).
                name = safe_relpath(str(got.get("name") or rel.name)) or Path(rel.name)
                target = dest / rel.parent / name.name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(base64.b64decode(got["data"]))
                entry.update(status="ok", saved_as=str(target.relative_to(dest)), exported_as=got.get("exportedAs"))
                fetched += 1
            else:
                entry["status"] = f"failed: {got.get('error') or f'HTTP {status}'}"
                skipped += 1
        manifest.append(entry)
    (dest / "_drive-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return {"files": fetched, "skipped": skipped}


def upload_docx(relay: Relay, job_id: str, docx: Path, name: str) -> dict:
    query = urllib.parse.urlencode({"name": name})
    status, got = relay.json("POST", f"/v1/runner/jobs/{job_id}/drive/upload?{query}",
                             data=base64.b64encode(docx.read_bytes()), content_type="text/plain", timeout=420)
    if status == 200 and got.get("ok"):
        return {"name": name, "id": got.get("id"), "url": got.get("url")}
    return {"name": name, "error": got.get("error") or f"HTTP {status}"}


# --- container ------------------------------------------------------------------------

def container_name(job_id: str) -> str:
    return f"hwr-{job_id[:8]}"


def podman_command(cfg: Config, job_id: str, work: Path, out: Path) -> list[str]:
    cmd = [*cfg.podman, "run", "--rm", "--name", container_name(job_id), "--init", "--pull=never",
           "--read-only", "--tmpfs", "/tmp:rw,size=2g,mode=1777",
           "--cap-drop=all", "--security-opt=no-new-privileges",
           "--memory", str(cfg.memory), "--cpus", str(cfg.cpus), "--pids-limit", str(cfg.pids_limit),
           "-v", f"{work}:/work:Z", "-v", f"{out}:/out:Z",
           # Values come from podman's own environment, so they never show in `ps`.
           "-e", "ANTHROPIC_BASE_URL", "-e", "ANTHROPIC_API_KEY"]
    if cfg.userns:
        cmd += ["--userns", cfg.userns]
    if cfg.network:
        cmd += ["--network", cfg.network]
    return [*cmd, *cfg.extra_args, cfg.image]


def read_progress(out: Path) -> str | None:
    try:
        p = json.loads((out / "progress.json").read_text())
    except (OSError, ValueError):
        return None
    extra = ", ".join(f"{k} {p[k]}" for k in ("turns", "minutes") if k in p)
    return f"{p.get('phase', '?')}: {p.get('message', '')}" + (f" ({extra})" if extra else "")


class Stopping:
    """Set by SIGTERM or SIGINT; the runner finishes its job record and exits."""
    event = threading.Event()


def run_container(cfg: Config, relay: Relay, job: dict, token: str, base_url: str, jobdir: Path,
                  deadline: float) -> dict:
    work, out = jobdir / "work", jobdir / "out"
    env = dict(os.environ, ANTHROPIC_BASE_URL=base_url, ANTHROPIC_API_KEY=token)
    cmd = podman_command(cfg, job["id"], work, out)
    (jobdir / "podman-command.txt").write_text(" ".join(cmd) + "\n")
    outcome = {"exit": None, "canceled": False, "timed_out": False, "stopped": False}
    with open(jobdir / "container.log", "ab") as logf:
        proc = subprocess.Popen(cmd, env=env, stdout=logf, stderr=subprocess.STDOUT)
        next_beat = 0.0
        while proc.poll() is None:
            time.sleep(1)
            now = time.time()
            reason = None
            if now > deadline + 60:
                reason, outcome["timed_out"] = "time limit", True
            elif Stopping.event.is_set():
                reason, outcome["stopped"] = "runner stopping", True
            elif now >= next_beat:
                next_beat = now + cfg.heartbeat_seconds
                status, beat = relay.json("POST", f"/v1/runner/jobs/{job['id']}/heartbeat",
                                          body={"progress": read_progress(out)}, timeout=30)
                if status == 200 and beat.get("cancel"):
                    reason, outcome["canceled"] = "canceled from GitHub", True
            if reason:
                log(f"job {job['id']}: stopping the container ({reason})")
                stop_container(cfg, job["id"])
                try:
                    proc.wait(timeout=90)
                except subprocess.TimeoutExpired:
                    subprocess.run([*cfg.podman, "kill", container_name(job["id"])], capture_output=True)
                    proc.wait(timeout=30)
        outcome["exit"] = proc.returncode
    return outcome


def stop_container(cfg: Config, job_id: str) -> None:
    subprocess.run([*cfg.podman, "stop", "-t", "45", container_name(job_id)], capture_output=True, timeout=120)


def tail(path: Path, max_bytes: int = 32_000) -> str:
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - max_bytes))
            return f.read().decode(errors="replace")
    except OSError:
        return ""


# --- one job --------------------------------------------------------------------------

def send_finish(relay: Relay, jobdir: Path, job_id: str, payload: dict, patience_s: float = 1800) -> bool:
    """Deliver the result, retrying through relay outages; park it on disk if that fails."""
    pending = jobdir / "finish-pending.json"
    pending.write_text(json.dumps(payload))
    delay, waited = 5.0, 0.0
    while True:
        status, got = relay.json("POST", f"/v1/runner/jobs/{job_id}/finish", body=payload, timeout=120)
        if status == 200 or status == 409:
            # 409: the relay no longer holds the job for us (expired or finished); nothing to retry.
            pending.unlink(missing_ok=True)
            if status == 409:
                log(f"job {job_id}: relay refused the result: {got.get('error')}")
            return status == 200
        if waited >= patience_s:
            log(f"job {job_id}: could not deliver the result (HTTP {status}); kept in {pending}")
            return False
        time.sleep(delay)
        waited += delay
        delay = min(delay * 2, 120)


def run_job(cfg: Config, relay: Relay, claimed: dict) -> str:
    job, token, base_url = claimed["job"], claimed["api_token"], claimed["anthropic_base_url"]
    job_id = job["id"]
    jobdir = cfg.work_root / job_id
    work, out = jobdir / "work", jobdir / "out"
    for d in (jobdir, work, out):
        d.mkdir(parents=True, exist_ok=True, mode=0o700)
    minutes = min(float(cfg.max_runtime_minutes), float(job["options"]["max_runtime_minutes"]))
    deadline = time.time() + minutes * 60
    (work / "job.json").write_text(json.dumps({**job, "deadline_epoch": deadline}, indent=2) + "\n")
    log(f"job {job_id}: {job['repo']} {job['ref']} @ {job['sha'][:12]}, {minutes:.0f} min limit")

    status, error, report = "failed", None, None
    summary: dict = {}
    try:
        relay.json("POST", f"/v1/runner/jobs/{job_id}/heartbeat", body={"progress": "downloading shared-drive files"})
        drive = fetch_drive(relay, job_id, work / "drive", int(cfg.drive_max_file_mb * 1_000_000))
        log(f"job {job_id}: drive {drive}")
        relay.json("POST", f"/v1/runner/jobs/{job_id}/heartbeat", body={"progress": "starting the review container"})
        outcome = run_container(cfg, relay, job, token, base_url, jobdir, deadline)
        try:
            summary = json.loads((out / "summary.json").read_text())
        except (OSError, ValueError):
            summary = {"error": "the container wrote no summary.json"}
        if not isinstance(summary, dict):
            summary = {"error": "the container's summary.json is not a JSON object"}
        summary["runner"] = {"name": cfg.runner_name, "version": VERSION, "drive": drive, **outcome}
        report_path = out / "report.md"
        if report_path.exists():
            report = report_path.read_text(errors="replace")[:REPORT_LIMIT]
        docx = out / "report.docx"
        if docx.exists() and summary.get("report_name"):
            summary["drive_report"] = upload_docx(relay, job_id, docx, f"{summary['report_name']}.docx")
            log(f"job {job_id}: docx upload {summary['drive_report']}")
        if outcome["canceled"]:
            status, error = "canceled", "canceled from GitHub"
        elif report and summary.get("ok"):
            status, error = "done", summary.get("error")
        else:
            error = summary.get("error") or f"the container exited with status {outcome['exit']}"
            if outcome["timed_out"]:
                error = "the job hit its time limit; " + error
            if outcome["stopped"]:
                error = "the runner was stopped; " + error
    except Exception as exc:  # report any failure to GitHub rather than dying silently
        error = f"runner error: {exc}"
        (jobdir / "runner-error.txt").write_text(traceback.format_exc())
        log(f"job {job_id}: {error}")
    payload = {"status": status, "report": report, "summary": summary, "error": error,
               "log_tail": tail(jobdir / "container.log")}
    send_finish(relay, jobdir, job_id, payload)
    log(f"job {job_id}: {status}" + (f" ({error})" if error else ""))
    return status


# --- housekeeping -------------------------------------------------------------------

def resend_pending(relay: Relay, work_root: Path) -> None:
    for pending in work_root.glob("*/finish-pending.json"):
        try:
            payload = json.loads(pending.read_text())
        except (OSError, ValueError):
            continue
        send_finish(relay, pending.parent, pending.parent.name, payload, patience_s=0)


def remove_leftover_containers(cfg: Config) -> None:
    got = subprocess.run([*cfg.podman, "ps", "-a", "--filter", "name=^hwr-", "--format", "{{.Names}}"],
                         capture_output=True, text=True)
    for name in got.stdout.split():
        log(f"removing leftover container {name}")
        subprocess.run([*cfg.podman, "rm", "-f", name], capture_output=True)


def prune_jobs(work_root: Path, keep: int) -> None:
    dirs = sorted((d for d in work_root.iterdir() if d.is_dir() and not (d / "finish-pending.json").exists()),
                  key=lambda d: d.stat().st_mtime, reverse=True)
    for d in dirs[keep:]:
        shutil.rmtree(d, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Run hw-review jobs from the relay in podman containers.")
    ap.add_argument("--config", type=Path, required=True)
    ap.add_argument("--once", action="store_true", help="run at most one job, then exit")
    args = ap.parse_args(argv)
    cfg = load_config(args.config)
    cfg.work_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    relay = Relay(cfg)
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: Stopping.event.set())
    log(f"runner {cfg.runner_name} {VERSION}: relay {cfg.relay_url}, image {cfg.image}")
    remove_leftover_containers(cfg)
    resend_pending(relay, cfg.work_root)
    while not Stopping.event.is_set():
        status, claimed = relay.json("POST", "/v1/runner/claim", body={"version": VERSION})
        if status == 200 and claimed.get("job"):
            run_job(cfg, relay, claimed)
            prune_jobs(cfg.work_root, cfg.keep_jobs)
            if args.once:
                return 0
            continue
        if status not in (200, 204):
            log(f"claim failed (HTTP {status}): {claimed.get('error')}")
        if args.once:
            return 0
        Stopping.event.wait(cfg.poll_seconds)
    log("stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())

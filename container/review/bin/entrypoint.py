#!/usr/bin/env python3
"""Review container entrypoint: prepare the workspace, run Claude Code, write the report.

The runner writes /work/job.json and the shared-drive files under /work/drive, then
starts the container. This script:

1. clones the repository at the job's commit into /work/repo (anonymous HTTPS),
2. clones the KiCad libraries named by the project's library path variables,
3. runs ERC, DRC, netlist, BOM and PDF exports into /work/checks,
4. runs Claude Code headless on the review task, streaming its events to
   /out/transcript.jsonl and its progress to /out/progress.json,
5. appends a run record to /out/report.md and converts it to /out/report.docx,
6. writes /out/summary.json for the runner.

Exit status 0 means /out/report.md exists; summary.json says whether it is complete.
"""

import importlib.util
import json
import os
import re
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

WORK = Path(os.environ.get("HW_REVIEW_WORK", "/work"))
OUT = Path(os.environ.get("HW_REVIEW_OUT", "/out"))
OPT = Path(os.environ.get("HW_REVIEW_OPT", "/opt/review"))

# Leave this much of the time limit for the run record, the .docx and the summary.
FINISH_RESERVE_S = 300
STEP_TIMEOUT_S = 900
KICAD_BUILTIN_VARS = re.compile(r"^(KICAD\d*_|KIPRJMOD$|KICAD_)")


def log(message: str) -> None:
    print(f"{datetime.now(timezone.utc).strftime('%H:%M:%S')} {message}", flush=True)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def write_json(path: Path, value: object) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, indent=2) + "\n")
    tmp.replace(path)


def progress(phase: str, message: str, **extra: object) -> None:
    write_json(OUT / "progress.json", {"phase": phase, "message": message[:300], "updated": now_iso(), **extra})


def run(cmd: list, *, cwd: Path | None = None, env: dict | None = None, timeout: float = STEP_TIMEOUT_S,
        log_path: Path | None = None) -> tuple[int, float]:
    """Run a command, its output going to log_path (or this process's output)."""
    start = time.monotonic()
    out = open(log_path, "w") if log_path else None
    try:
        rc = subprocess.run([str(c) for c in cmd], cwd=cwd, env=env, timeout=timeout,
                            stdout=out or None, stderr=subprocess.STDOUT if out else None).returncode
    except subprocess.TimeoutExpired:
        rc = 124
        if out:
            out.write(f"\n[timed out after {timeout:.0f} s]\n")
    finally:
        if out:
            out.close()
    return rc, time.monotonic() - start


def git_out(args: list, cwd: Path) -> str:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=120).stdout.strip()


def ref_name(ref: str) -> str:
    for prefix in ("refs/heads/", "refs/tags/"):
        if ref.startswith(prefix):
            return ref[len(prefix):]
    return ref


def safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-") or "board"


# --- repository and libraries -------------------------------------------------------

def clone_repo(job: dict, dest: Path) -> None:
    repo, sha = job["repo"], job["sha"]
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo) or not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise RuntimeError("job.json has a malformed repository or commit")
    url = f"https://github.com/{repo}.git"
    rc, _ = run(["git", "clone", "--quiet", "--filter=blob:none", "--no-checkout", url, dest], timeout=900)
    if rc != 0:
        raise RuntimeError(f"git clone of {url} failed (exit {rc}); is the repository public?")
    if run(["git", "checkout", "--quiet", "--detach", sha], cwd=dest)[0] != 0:
        # The commit may be on no branch any more (a force push): fetch it by name.
        run(["git", "fetch", "--quiet", "--filter=blob:none", "origin", sha], cwd=dest, timeout=900)
        if run(["git", "checkout", "--quiet", "--detach", sha], cwd=dest)[0] != 0:
            raise RuntimeError(f"commit {sha} is not in {url}")


def library_vars(project_dir: Path) -> set[str]:
    """Path variables the project's library tables use, minus KiCad's own."""
    found: set[str] = set()
    for table in [*project_dir.rglob("sym-lib-table"), *project_dir.rglob("fp-lib-table")]:
        for var in re.findall(r"\$\{([A-Za-z0-9_]+)\}", table.read_text(errors="replace")):
            if not KICAD_BUILTIN_VARS.match(var):
                found.add(var)
    return found


def clone_libraries(names: set[str], known: dict, dest: Path) -> dict:
    result = {}
    dest.mkdir(parents=True, exist_ok=True)
    for name in sorted(names):
        entry = known.get(name)
        if not entry:
            result[name] = {"error": "no repository is known for this variable (container/review/libraries.json)"}
            continue
        path = dest / name
        rc, _ = run(["git", "clone", "--quiet", "--depth", "1", entry["url"], path], timeout=900,
                    log_path=dest / f"{name}.log")
        if rc != 0:
            result[name] = {"url": entry["url"], "error": f"git clone failed (exit {rc})"}
            continue
        result[name] = {"url": entry["url"], "path": str(path), "commit": git_out(["rev-parse", "--short=12", "HEAD"], path)}
    return result


def setup_kicad_home(home: Path) -> dict:
    """Fresh KiCad config: default global library tables and the stock library paths."""
    cfg = home / ".config/kicad/10.0"
    cfg.mkdir(parents=True, exist_ok=True)
    template = Path("/usr/share/kicad/template")
    for table in ("sym-lib-table", "fp-lib-table"):
        if (template / table).exists() and not (cfg / table).exists():
            (cfg / table).write_text((template / table).read_text())
    share = Path("/usr/share/kicad")
    return {
        "KICAD10_SYMBOL_DIR": str(share / "symbols"),
        "KICAD10_FOOTPRINT_DIR": str(share / "footprints"),
        "KICAD10_3DMODEL_DIR": str(share / "3dmodels"),
        "KICAD10_TEMPLATE_DIR": str(share / "template"),
    }


# --- checks ---------------------------------------------------------------------------

def find_projects(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*.kicad_pro")
                  if not any(part.endswith("-backups") or part.startswith(".") for part in p.relative_to(root).parts))


def count_violations(path: Path) -> dict | None:
    """Severity and type counts from a kicad-cli ERC or DRC JSON report."""
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    groups = {"violations": [v for s in data.get("sheets", []) for v in s.get("violations", [])]}
    if "violations" in data:
        groups = {k: data.get(k, []) for k in ("violations", "unconnected_items", "schematic_parity")}
    out: dict = {"ignored_checks": [c.get("key") for c in data.get("ignored_checks", []) if c.get("key")]}
    for group, items in groups.items():
        severities: dict = {}
        types: dict = {}
        for v in items:
            severities[v.get("severity", "?")] = severities.get(v.get("severity", "?"), 0) + 1
            types[v.get("type", "?")] = types.get(v.get("type", "?"), 0) + 1
        out[group] = {"total": len(items), "by_severity": severities,
                      "by_type": dict(sorted(types.items(), key=lambda kv: -kv[1]))}
    return out


def describe_counts(counts: dict | None) -> str:
    if counts is None:
        return "no report"
    v = counts["violations"]
    parts = [", ".join(f"{n} {sev}" for sev, n in sorted(v["by_severity"].items())) or "no violations"]
    for group in ("unconnected_items", "schematic_parity"):
        if group in counts:
            parts.append(f"{counts[group]['total']} {group.replace('_', ' ')}")
    top = ", ".join(f"{t} x{n}" for t, n in list(v["by_type"].items())[:5])
    text = "; ".join(parts) + (f" (top types: {top})" if top else "")
    if counts["ignored_checks"]:
        text += f"; checks ignored by the project: {', '.join(counts['ignored_checks'])}"
    return text


def run_checks(projects: list[Path], env: dict, checks_root: Path) -> list[dict]:
    tools = OPT / "tools"
    results = []
    for pro in projects:
        name = safe_name(pro.stem)
        d = checks_root / name
        d.mkdir(parents=True, exist_ok=True)
        sch, pcb = pro.with_suffix(".kicad_sch"), pro.with_suffix(".kicad_pcb")
        steps: list[tuple[str, list]] = []
        if sch.exists():
            steps += [
                ("erc", ["kicad-cli", "sch", "erc", "--format", "json", "--severity-all", "--units", "mm", "-o", d / "erc.json", sch]),
                ("erc-report", ["kicad-cli", "sch", "erc", "--format", "report", "--severity-all", "--units", "mm", "-o", d / "erc.rpt", sch]),
                ("netlist", ["kicad-cli", "sch", "export", "netlist", "--format", "kicadsexpr", "-o", d / "netlist.net", sch]),
                ("netlist-tables", ["python3", tools / "parse_netlist.py", d / "netlist.net", d]),
                ("bom", ["kicad-cli", "sch", "export", "bom", "-o", d / "bom.csv", sch]),
                ("schematic-pdf", ["kicad-cli", "sch", "export", "pdf", "-o", d / "schematic.pdf", sch]),
            ]
        if pcb.exists():
            parity = ["--schematic-parity"] if sch.exists() else []
            steps += [
                ("drc", ["kicad-cli", "pcb", "drc", "--format", "json", "--severity-all", "--units", "mm", *parity, "-o", d / "drc.json", pcb]),
                ("drc-refilled", ["kicad-cli", "pcb", "drc", "--format", "json", "--severity-all", "--units", "mm", "--refill-zones", "-o", d / "drc-refilled.json", pcb]),
                ("board-tables", ["python3", tools / "pcb_extract.py", pcb, d]),
            ]
        record = {"project": str(pro.relative_to(WORK / "repo")) if pro.is_relative_to(WORK / "repo") else str(pro),
                  "dir": str(d), "steps": []}
        for step, cmd in steps:
            progress("checks", f"{name}: {step}")
            rc, seconds = run(cmd, cwd=pro.parent, env=env, log_path=d / f"{step}.log")
            record["steps"].append({"step": step, "exit": rc, "seconds": round(seconds, 1),
                                    "command": " ".join(str(c) for c in cmd)})
            log(f"check {name} {step}: exit {rc} in {seconds:.0f} s")
        record["erc"] = count_violations(d / "erc.json") if sch.exists() else None
        record["drc"] = count_violations(d / "drc.json") if pcb.exists() else None
        record["drc_refilled"] = count_violations(d / "drc-refilled.json") if pcb.exists() else None
        results.append(record)
    write_checks_readme(results, checks_root)
    return results


def write_checks_readme(results: list[dict], checks_root: Path) -> None:
    lines = ["# Checks run by the job", "",
             "Each project's outputs are in its own folder. A step's console output is in `<step>.log`;",
             "rerun a step with the command shown (from the project's folder).", ""]
    if not results:
        lines.append("No KiCad project (`*.kicad_pro`) was found in the review scope.")
    for r in results:
        lines += [f"## {r['project']}", "",
                  f"- ERC: {describe_counts(r['erc'])}" if r["erc"] is not None or any(s["step"] == "erc" for s in r["steps"]) else "- ERC: no schematic",
                  f"- DRC, saved zone fill: {describe_counts(r['drc'])}" if r["drc"] is not None or any(s["step"] == "drc" for s in r["steps"]) else "- DRC: no board",
                  ]
        if r["drc_refilled"] is not None:
            lines.append(f"- DRC, zones refilled: {describe_counts(r['drc_refilled'])}")
        lines += ["", "| Step | Exit | Seconds | Command |", "|---|---|---|---|"]
        lines += [f"| {s['step']} | {s['exit']} | {s['seconds']} | `{s['command']}` |" for s in r["steps"]]
        lines.append("")
    (checks_root / "README.md").write_text("\n".join(lines) + "\n")


# --- shared drive ---------------------------------------------------------------------

def drive_markdown_copies(drive: Path) -> int:
    """Put a Markdown copy beside each Word file so it can be read and grepped."""
    made = 0
    for doc in drive.rglob("*.docx"):
        md = doc.with_suffix(".docx.md")
        if not md.exists() and run(["pandoc", "--from", "docx", "--to", "gfm", "-o", md, doc], timeout=120)[0] == 0:
            made += 1
    return made


def drive_files(drive: Path) -> list[Path]:
    if not drive.is_dir():
        return []
    return sorted(p for p in drive.rglob("*") if p.is_file() and not p.name.startswith("_drive-manifest"))


def prompt_override(drive: Path) -> Path | None:
    for p in drive_files(drive):
        if re.fullmatch(r"review[-_ ]prompt", p.stem, re.I) and p.suffix.lower() in (".md", ".txt"):
            return p
    return None


# --- task and Claude Code -------------------------------------------------------------

def build_task(job: dict, instructions: str, ctx: dict) -> str:
    o = job["options"]
    branch = ref_name(job["ref"])
    lines = [instructions.strip(), "", "## This review", "",
             f"- Repository: https://github.com/{job['repo']}, `{branch}` at commit `{job['sha']}`, checked out in "
             f"`/work/repo`. Write the revision line as **Revision reviewed** - `{branch}` @ `{job['sha']}`.",
             f"- Review scope: `{o['path']}` in the repository."]
    if ctx["projects"]:
        lines.append("- KiCad projects in scope: " + ", ".join(f"`{p}`" for p in ctx["projects"]) + ".")
    else:
        lines.append("- No KiCad project was found in scope; review what the scope holds and say so.")
    lines.append(f"- Checks already run: `/work/checks/README.md`. {ctx['checks_line']}")
    if ctx["commit_message"]:
        lines.append(f"- Commit message: {ctx['commit_message']!r}")
    if ctx["drive_count"]:
        n = ctx["drive_count"]
        lines.append(f"- Shared drive: {n} file{'s' if n != 1 else ''} in `/work/drive` (index `/work/drive/_drive-manifest.json`).")
        if ctx["earlier_reviews"]:
            lines.append("- Earlier reviews in the shared drive: " + ", ".join(f"`{p}`" for p in ctx["earlier_reviews"][:10])
                         + ". Check which of their findings this commit resolves.")
    else:
        lines.append("- Shared drive: no files for this repository.")
    for name, lib in ctx["libraries"].items():
        lines.append(f"- Library variable `{name}`: " + (f"`{lib['path']}` ({lib['url']} at {lib['commit']})"
                                                        if "path" in lib else f"not resolved: {lib['error']}"))
    if ctx["team_context"]:
        lines.append(f"- Team context: `{ctx['team_context']}`; read it before tracing interfaces to other boards.")
    lines += [f"- Limits: about ${o['budget_usd']:.0f} of API spend and {ctx['minutes']} minutes; the session stops when either runs out.",
              f"- Requested by a {job['event']} from {job['actor']}: https://github.com/{job['repo']}/actions/runs/{job['run_id']}.",
              "- Write the report to `/out/report.md` in GitHub-flavored Markdown; headings and tables carry over to the .docx copy."]
    return "\n".join(lines) + "\n"


# Tools with no use in an unattended run. Workflow would fan out paid agents.
DISALLOWED_TOOLS = ["Workflow", "CronCreate", "CronDelete", "CronList", "ScheduleWakeup", "EnterWorktree",
                    "ExitWorktree", "SendMessage", "ListAgents", "ReportFindings"]


def claude_command(job: dict) -> list[str]:
    # Not --bare: bare mode offers only Bash, Edit and Read (no Write, WebFetch,
    # WebSearch or subagents). User settings come from the job's empty HOME, and the
    # reviewed repository sits below the working directory, so its .claude/ settings
    # and .mcp.json are not loaded.
    o = job["options"]
    return ["claude", "-p", "--output-format", "stream-json", "--verbose",
            "--model", o["model"], "--effort", o["effort"], "--max-budget-usd", f"{float(o['budget_usd']):.2f}",
            "--permission-mode", "bypassPermissions", "--setting-sources", "user", "--strict-mcp-config",
            "--disallowedTools", ",".join(DISALLOWED_TOOLS),
            "--append-system-prompt-file", str(OPT / "prompts/ci-context.md"),
            "--add-dir", str(OUT), "--add-dir", str(OPT)]


def describe_tool(block: dict) -> str:
    inp = block.get("input") or {}
    detail = next((str(inp[k]) for k in ("command", "file_path", "path", "pattern", "url", "query", "description", "prompt")
                   if inp.get(k)), "")
    detail = " ".join(detail.split())
    return f"{block.get('name', 'tool')}: {detail[:140]}" if detail else str(block.get("name", "tool"))


def run_claude(cmd: list[str], task: str, deadline: float, env: dict) -> dict:
    """Run Claude Code, mirror its stream to transcript.jsonl, and stop it at the deadline."""
    state = {"turns": 0, "tools": 0, "result": None, "stopped": None, "exit": None}
    started = time.monotonic()
    with open(OUT / "transcript.jsonl", "w") as transcript, open(OUT / "claude-stderr.log", "w") as stderr:
        proc = subprocess.Popen(cmd, cwd=WORK, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=stderr, text=True)
        assert proc.stdin and proc.stdout
        proc.stdin.write(task)
        proc.stdin.close()

        def stop(reason: str) -> None:
            if proc.poll() is None and not state["stopped"]:
                state["stopped"] = reason
                log(f"stopping Claude Code: {reason}")
                proc.send_signal(signal.SIGINT)

        def on_alarm(_sig, _frame) -> None:
            # First alarm: ask Claude Code to stop. Second, 30 s later: kill it.
            if not state["stopped"]:
                stop("time limit reached")
                signal.alarm(30)
            elif proc.poll() is None:
                proc.kill()

        signal.signal(signal.SIGALRM, on_alarm)
        signal.alarm(max(1, int(deadline - time.time())))
        signal.signal(signal.SIGTERM, lambda *_: stop("container stopped"))
        for line in proc.stdout:
            transcript.write(line)
            transcript.flush()
            try:
                event = json.loads(line)
            except ValueError:
                continue
            kind = event.get("type")
            if kind == "assistant":
                state["turns"] += 1
                for block in event.get("message", {}).get("content", []):
                    if block.get("type") == "tool_use":
                        state["tools"] += 1
                        progress("review", describe_tool(block), turns=state["turns"], tool_calls=state["tools"],
                                 minutes=round((time.monotonic() - started) / 60, 1))
            elif kind == "result":
                state["result"] = event
        state["exit"] = proc.wait()
        signal.alarm(0)
    return state


# --- report ---------------------------------------------------------------------------

def finalize_report(report: Path, state: dict, record: list[str]) -> tuple[bool, bool, str | None]:
    """Make sure a report exists, flag an incomplete one, append the run record.

    Returns (has_report, complete, error)."""
    result = state.get("result") or {}
    text = report.read_text() if report.exists() else ""
    error = None
    if not text.strip() and (result.get("result") or "").strip():
        text = ("> Claude Code ended without writing `/out/report.md`; this is its final message.\n\n"
                + result["result"].strip() + "\n")
    if not text.strip():
        error = (f"no report: Claude Code {result.get('subtype') or 'exited'}"
                 f" (exit {state.get('exit')}){'; ' + state['stopped'] if state.get('stopped') else ''}")
        return False, False, error
    complete = result.get("subtype") == "success" and not state.get("stopped") and not result.get("is_error")
    if not complete:
        why = state.get("stopped") or result.get("subtype") or f"exit {state.get('exit')}"
        text = (f"> **Incomplete review**: the session stopped before it finished ({why}). "
                "The findings below are the ones it had settled.\n\n" + text)
        error = f"incomplete: {why}"
    report.write_text(text.rstrip() + "\n\n---\n\n" + "\n".join(record) + "\n")
    return True, complete, error


def brief_check(report: Path) -> str | None:
    """One line for the run record: what tools/brief_lint.py finds in the finding briefs."""
    if not report.exists():
        return None
    try:
        spec = importlib.util.spec_from_file_location("brief_lint", OPT / "tools/brief_lint.py")
        if spec is None or spec.loader is None:
            return "not run: tools/brief_lint.py is missing"
        lint = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(lint)
        problems = lint.check(report.read_text(errors="replace"))
    except Exception as exc:  # a broken check must not cost the team the report
        return f"not run: {exc}"
    if not problems:
        return "every finding brief is a plain-language heading"
    where = list(dict.fromkeys(w for _, w, _ in problems))
    shown = ", ".join(where[:10]) + (", ..." if len(where) > 10 else "")
    return f"{len(problems)} problem{'s' if len(problems) > 1 else ''} ({shown}); run `brief_lint.py` on the report"


def run_record(job: dict, state: dict, ctx: dict, versions: dict, briefs: str | None = None) -> list[str]:
    o = job["options"]
    result = state.get("result") or {}
    cost = result.get("total_cost_usd")
    minutes = (result.get("duration_ms") or 0) / 60000
    rows = [
        ("Revision", f"`{ref_name(job['ref'])}` @ `{job['sha']}`"),
        ("Scope", f"`{o['path']}`"),
        ("Model", f"{o['model']}, effort {o['effort']}"),
        ("Spend", (f"${cost:.2f}" if isinstance(cost, (int, float)) else "unknown")
                  + f" of ${float(o['budget_usd']):.2f}, {result.get('num_turns', state.get('turns'))} turns, {minutes:.0f} min"),
        ("Checks", ctx["checks_line"] or "none"),
        ("Shared drive", f"{ctx['drive_count']} files" + (f", prompt from `{ctx['prompt_source']}`" if ctx["prompt_source"] != "built-in" else "")),
        ("Tools", f"Claude Code {versions['claude']}, KiCad {versions['kicad']}"),
        ("Job", f"{job['id']}, https://github.com/{job['repo']}/actions/runs/{job['run_id']}"),
    ]
    if briefs:
        rows.insert(-1, ("Brief check", briefs))
    return ["## Review run", "", "| | |", "|---|---|", *[f"| {k} | {v} |" for k, v in rows]]


def to_docx(md: Path, docx: Path) -> str | None:
    rc, _ = run(["pandoc", "--from", "gfm", "--to", "docx", "-o", docx, md], timeout=300, log_path=OUT / "pandoc.log")
    return None if rc == 0 and docx.exists() else f"pandoc failed (exit {rc}); see pandoc.log"


def report_name(job: dict, projects: list[str]) -> str:
    board = Path(projects[0]).stem if len(projects) == 1 else job["repo"].split("/")[-1]
    return f"{safe_name(board)}-review-{datetime.now(timezone.utc):%Y-%m-%d}-{job['sha'][:7]}"


def read_version(name: str) -> str:
    try:
        return (OPT / f"{name}-version").read_text().split()[0]
    except (OSError, IndexError):
        return "unknown"


# --- main -----------------------------------------------------------------------------

def main() -> int:
    job = json.loads((WORK / "job.json").read_text())
    OUT.mkdir(parents=True, exist_ok=True)
    deadline = float(job.get("deadline_epoch") or time.time() + 3600 * 3)
    summary: dict = {"ok": False, "complete": False, "error": None, "started": now_iso()}
    versions = {"claude": read_version("claude"), "kicad": read_version("kicad")}
    summary["versions"] = versions
    home = WORK / "home"
    home.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, HOME=str(home))
    env.update(setup_kicad_home(home))
    (WORK / "scratch" / "ds").mkdir(parents=True, exist_ok=True)
    try:
        progress("clone", f"cloning {job['repo']} at {job['sha'][:12]}")
        repo = WORK / "repo"
        clone_repo(job, repo)
        scope = (repo / job["options"]["path"]).resolve()
        if not scope.is_relative_to(repo.resolve()) or not scope.is_dir():
            raise RuntimeError(f"review path {job['options']['path']!r} is not a directory in the repository")

        progress("libraries", "cloning KiCad libraries")
        known = json.loads((OPT / "libraries.json").read_text())
        libraries = clone_libraries(library_vars(scope), known, WORK / "libs")
        env.update({name: lib["path"] for name, lib in libraries.items() if "path" in lib})

        projects = find_projects(scope)
        checks = run_checks(projects, env, WORK / "checks")
        checks_line = "; ".join(
            f"{Path(c['project']).stem}: ERC {describe_counts(c['erc']) if c['erc'] else 'n/a'}; "
            f"DRC {describe_counts(c['drc']) if c['drc'] else 'n/a'}" for c in checks)

        drive = WORK / "drive"
        if drive.is_dir():
            drive_markdown_copies(drive)
        files = drive_files(drive)
        override = prompt_override(drive)
        instructions = (override or OPT / "prompts/review-task.md").read_text()
        team = OPT / "prompts" / f"context-{job['repo'].split('/')[0].lower()}.md"
        project_names = [c["project"] for c in checks]
        ctx = {
            "projects": project_names,
            "checks_line": checks_line,
            "commit_message": git_out(["log", "-1", "--format=%B", job["sha"]], repo)[:2000],
            "drive_count": len(files),
            "earlier_reviews": [str(p.relative_to(drive)) for p in files
                                if "review" in p.name.lower() and p != override and p.suffix in (".md", ".docx")],
            "libraries": libraries,
            "team_context": str(team) if team.exists() else None,
            "minutes": max(1, int((deadline - time.time()) / 60)),
            "prompt_source": f"drive/{override.relative_to(drive)}" if override else "built-in",
        }
        summary.update(checks=checks, libraries=libraries, drive_files=len(files), prompt_source=ctx["prompt_source"])
        task = build_task(job, instructions, ctx)
        (WORK / "task.md").write_text(task)

        progress("review", "Claude Code is starting")
        state = run_claude(claude_command(job), task, deadline - FINISH_RESERVE_S, env)
        result = state.get("result") or {}
        summary["claude"] = {k: result.get(k) for k in ("subtype", "is_error", "total_cost_usd", "num_turns", "duration_ms", "session_id")}
        summary["claude"].update(exit=state["exit"], stopped=state["stopped"], tool_calls=state["tools"])

        progress("report", "writing the report and its .docx copy")
        report = OUT / "report.md"
        record = run_record(job, state, ctx, versions, brief_check(report))
        has_report, complete, error = finalize_report(report, state, record)
        summary.update(ok=has_report, complete=complete, error=error)
        if has_report:
            summary["report_name"] = report_name(job, project_names)
            summary["docx_error"] = to_docx(report, OUT / "report.docx")
    except Exception as exc:  # the summary must say what broke, whatever it was
        log(f"error: {exc}")
        summary["error"] = str(exc)
    summary["finished"] = now_iso()
    write_json(OUT / "summary.json", summary)
    progress("done", "report ready" if summary["ok"] else f"failed: {summary['error']}")
    return 0 if summary["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())

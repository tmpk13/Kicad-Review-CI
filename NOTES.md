# Notes

Decisions and pitfalls, with the reasons. Open decisions are in [QUESTIONS.md](QUESTIONS.md).

## Credentials and trust

- The Anthropic key lives only in the relay. The container's Claude Code uses
  `ANTHROPIC_BASE_URL=<relay>/anthropic` with a per-job token as its API key, and the
  relay swaps in the real key. The agent has a shell and reads untrusted text
  (datasheets, web pages, drive documents, design files); if an injected instruction
  leaks the token, it is worth only what the job may spend before it ends. The relay
  stores the token's SHA-256, accepts it only while its job runs, and only for
  `/v1/messages`, `/v1/messages/count_tokens` and `/v1/models`.
- The runner, not the container, talks to Drive: it downloads the job's files before
  the container starts and uploads the .docx after it exits. A Drive-capable token in
  the container would let the agent list, read and add files beyond its job.
- GitHub Actions authenticate with OIDC, not a shared secret. The relay takes the
  repository, commit, ref and run from the verified token; the request body only picks
  the path, model, effort and budget, each checked against relay settings. Board
  repositories hold no secret, fork pull requests get no token, and `ALLOWED_EVENTS`
  keeps `pull_request_target` out.
- The Apps Script checks every read and upload against its own `ALLOWED_ROOTS`, so a
  leaked script key or a wrong `DRIVE_CONFIG` cannot reach the rest of the drive.
  Shortcuts are skipped: their targets can lie outside the roots.
- The Apps Script key travels in the query string. Apps Script web apps do not see
  request headers, and the Worker cannot put it into an upload body that it streams
  without parsing.

## Relay on the Cloudflare free plan

- 10 ms of CPU per request: the Worker never parses a large body. Apps Script answers
  (a file is base64 inside JSON, up to tens of MB) and Anthropic streams pass through
  as streams. The runner's finish call is parsed; a report is typically under 100 KB.
- One SQLite Durable Object holds every job. It is strongly consistent, and its SQL
  calls are synchronous, so each state change runs with no `await` between its read
  and its write: two runners cannot claim one job. The free plan offers only
  SQLite-backed Durable Objects. KV would be eventually consistent and allows 1000
  list operations a day.
- Workers Builds deploys from git (root directory `relay`). Every tunable is a
  dashboard variable read with a default in `src/config.ts`; none is in
  `wrangler.jsonc`, so the public repository holds no settings, and `keep_vars` keeps
  a deploy from deleting them.
- The relay's main module exports only the handler and the Durable Object class:
  workerd refuses to start on any other named export, which tests that import the
  module and `wrangler deploy --dry-run` do not notice. The workerd test catches it.
- Free plan quotas (100,000 requests a day for the Worker and the Durable Object)
  leave room: a review makes a few hundred API calls through the proxy.

## GitHub side

- The job waits for the review and polls every minute, for up to five hours. One run
  then carries the request, the progress, the artifact and the summary. The earlier
  batch workflow collected results on a `schedule`, which GitHub runs only from the
  default branch, so it never fired for the review branch. Waiting costs nothing on
  public repositories.
- A newer push supersedes a queued job for the same ref. A running job finishes: its
  work is worth more than a restart.
- Board workflows use the composite action from this repository, so a fix ships to
  every board at once. Its script uses only the standard library of the runner's python3.

## Container

- Ubuntu 24.04 with KiCad from `ppa:kicad/kicad-10.0-releases` (10.0.8, the version the
  earlier reviews used), not the `kicad/kicad` image: apt adds the other tools. The
  library packages are `kicad-symbols` and `kicad-footprints`; the PPA page names
  `kicad-library-*` packages that do not install. No 3D models, to save disk.
- Claude Code is the native binary at `CLAUDE_CODE_VERSION`. The build verifies the
  release manifest's signature against Anthropic's key fingerprint, then the binary's
  SHA-256 from the manifest. Updates and nonessential traffic are off.
- Claude Code runs without `--bare`: bare mode offers only Bash, Edit and Read (no
  Write, WebFetch, WebSearch or subagents), which the smoke test showed. Instead
  `--setting-sources user` reads only the job's empty HOME, `--strict-mcp-config` loads
  no MCP servers, and the tools useless unattended are disallowed (Workflow would fan
  out paid agents). The working directory is `/work`, so the reviewed repository's
  `.claude/` and `.mcp.json` are not project settings. CLAUDE.md files in the
  repository can still load when Claude reads there; the system prompt treats file
  contents as evidence, not instructions.
- podman runs the image with a read-only root, a `/tmp` tmpfs, no capabilities,
  `no-new-privileges`, `--init` for signals and `--userns keep-id`, so job files belong
  to the runner's user. The container has full internet access and no fence against
  the local network (QUESTIONS Q6).
- Checks run before the agent starts: ERC, DRC on the saved and on refilled zones,
  netlist, BOM, schematic PDF, and the netlist and board tables. They cost no tokens,
  run the same way every time, and the task gives the agent their counts, including
  the checks the project ignores. On oresat-batteries at 1808a28 the DRC reports 13
  errors and 86 warnings, the same counts as the DRC run of the 2026-11-16 manual review.
- Library path variables are resolved by shallow clones of the repositories in
  `container/review/libraries.json`, at their default branch head: a stale local copy
  makes ERC and DRC report a library mismatch for every part.
- The report survives a cut-off: the prompt keeps `/out/report.md` readable at all
  times; a run stopped by budget or time gets an "Incomplete review" banner; a run
  that wrote no file falls back to Claude Code's final message. A run record
  (revision, model, spend, checks, tool versions, job link) closes the report.
- Time limits nest: the runner writes `deadline_epoch` into job.json; the entrypoint
  stops Claude Code (SIGINT, then a kill 30 s later) five minutes before it, leaving time
  for the .docx; the runner stops the container 60 s after it.
- The review instructions are the workspace's PCB review prompt in ASCII
  (`prompts/review-task.md`), with the team's finding style: each finding opens with a
  plain-language heading brief (no designators, part numbers, pins or nets), and the
  designators start its body. `prompts/ci-context.md` adds the environment, working
  rules, KiCad 10 traps and the completion test; `prompts/context-<owner>.md` adds team
  pointers. A drive file named `review-prompt` overrides the instructions.

## Runner

- Python standard library: the host needs only python3 3.11 and podman, and the code is
  HTTP and subprocess glue (QUESTIONS Q5).
- Secrets reach podman through its environment (`-e NAME` with no value), never argv,
  so they do not show in `ps`.
- A result the relay cannot take is retried for 30 minutes, then kept as
  `finish-pending.json` in the job directory and resent at the next start.
- A runner that asks for work while the relay lists it as running a job has restarted;
  the relay fails that job rather than wait for its heartbeat to go stale.

## Tests

- Offline (`mise run test`): the relay in workerd through Miniflare with fake GitHub,
  Anthropic and Apps Script services; the Apps Script in a vm with stand-in Google
  services; the runner, entrypoint and action against stand-ins for podman, claude and
  the relay; and one end-to-end run through all of them.
- `mise run image-smoke` adds the real image, podman, GitHub clone, kicad-cli and
  Claude Code, against a stand-in Anthropic API that answers with one Write call.
- Untested so far: the deploy through Workers Builds, the Apps Script against real
  Google (shared-drive behavior, exports, the "Anyone" setting under a Workspace
  policy), the real Anthropic API through the relay, a real GitHub Actions run (the
  OIDC request, the composite action), and review quality and cost.

# Setup

Five parts, set up in this order: an Anthropic API key, the relay on Cloudflare, the
Apps Script Drive bridge, the runner host, and the workflow in each board repository.
Generate each shared secret with `openssl rand -base64 32`.

## 1. Anthropic API key

In the Anthropic Console, create a workspace for reviews with a monthly spend limit,
and an API key in it. The key goes only into the relay (step 2): the review container
never sees it. The spend limit is the backstop if anything else fails.

## 2. Relay (Cloudflare Worker, deployed from git)

Cloudflare builds and deploys the Worker from this repository on every push to the
production branch; nothing runs `wrangler` on a workstation.

1. Push this repository to GitHub. It must be public for board repositories in other
   accounts to use its action.
2. Cloudflare dashboard: Workers & Pages > Create > Import a repository. Choose this
   repository, then set
   - project name `hw-review-relay` (it must match `name` in `relay/wrangler.jsonc`),
   - root directory `relay`,
   - build command empty, deploy command `npx wrangler deploy` (the default),
   - build watch path `relay/*`, so changes elsewhere do not redeploy.
3. Settings > Variables and Secrets (runtime, not build), as secrets:

   | Secret | Value |
   |---|---|
   | `ANTHROPIC_API_KEY` | the key from step 1 |
   | `RUNNER_TOKEN` | a new random secret; the runner host gets the same value |
   | `APPS_SCRIPT_URL` | the web app URL from step 3 (ends in `/exec`) |
   | `APPS_SCRIPT_KEY` | the `RELAY_KEY` from step 3 |

   and as text variables (defaults live in `relay/src/config.ts`):

   | Variable | Meaning | Default |
   |---|---|---|
   | `ALLOWED_REPOS` | repositories that may request reviews: `owner/name` or `owner/*`, comma-separated | none: every request is refused |
   | `DRIVE_CONFIG` | shared-drive folders per repository, JSON (below) | no Drive |
   | `DEFAULT_MODEL` | Claude model | `claude-opus-5-5` |
   | `ALLOWED_MODELS` | other models a workflow may ask for, comma-separated | none |
   | `DEFAULT_EFFORT` | `low`, `medium`, `high`, `xhigh` or `max` | `high` |
   | `DEFAULT_BUDGET_USD` / `MAX_BUDGET_USD` | API spend per review, default and cap | 20 / 40 |
   | `MAX_JOBS_PER_DAY` | reviews accepted per 24 hours, all repositories | 12 |
   | `MAX_RUNTIME_MINUTES` | time limit per review | 180 |
   | `ALLOWED_EVENTS` | GitHub events that may start a review | `push,workflow_dispatch` |
   | `OIDC_AUDIENCE` | audience the workflow's token must carry | `hw-review-relay` |
   | `STALE_MINUTES` / `QUEUE_TTL_MINUTES` | fail a job whose runner went silent / expire an unclaimed one | 10 / 360 |

   `DRIVE_CONFIG` maps a repository (exact name, then `owner/*`, then `*`) to the folders
   the review reads and the folder its .docx goes to:

   ```json
   {"oresat/oresat-batteries": {"read": ["<battery card folder ID>"], "upload": "<Reviews folder ID>"}}
   ```

   A folder ID is the last part of its Drive URL (`drive.google.com/drive/folders/<ID>`).
   Put the upload folder inside a read folder: the next review then sees the earlier
   reports and checks which findings were fixed.
4. Note the Worker's URL from its overview page. `curl <URL>/healthz` answers
   `hw-review relay`.

`keep_vars` in `relay/wrangler.jsonc` keeps a deploy from deleting the dashboard
variables. Secrets always survive deploys.

## 3. Apps Script Drive bridge

The script reads and writes the shared drive as the Google account that deploys it, so
use an account that can open the review folders.

1. script.google.com > New project. Replace `Code.gs` with `apps-script/Code.gs`. Under
   Project Settings, tick "Show appsscript.json manifest file", then replace that file
   with `apps-script/appsscript.json`.
2. Project Settings > Script properties: `RELAY_KEY` (a new random secret) and
   `ALLOWED_ROOTS` (comma-separated IDs of the folders reviews may read or write;
   anything outside them is refused even if the relay asks). Optional: `MAX_FILE_BYTES`
   (default 25000000).
3. In the editor, run `selfTest` once. Google asks for the Drive permission; the log then
   lists the files under each allowed folder.
4. Deploy > New deployment > Web app: Execute as "Me", Who has access "Anyone". Copy the
   URL into the relay's `APPS_SCRIPT_URL`, and `RELAY_KEY` into `APPS_SCRIPT_KEY`.
5. After editing the script: Deploy > Manage deployments > edit > Version: New version.
   The URL stays the same.

A Google Doc named `review-prompt` in a read folder replaces the built-in review
instructions (`container/review/prompts/review-task.md`) for that repository.

If a job's Drive step fails with "Apps Script answered HTTP 200 with text/html", Google
served a sign-in page instead of running the script: the deployment is not open to
"Anyone", or the Workspace administrator does not allow that setting. Deploy from an
account that may publish such web apps.

## 4. Runner host

A Linux machine with rootless podman, python3 3.11 or newer, git and
[mise](https://mise.jdx.dev). The review container has internet access, so prefer a
machine or network segment without access to anything sensitive on the local network.

1. Clone this repository, then `mise install` and `mise run image` (about 2.2 GB).
2. Copy `runner/runner.example.toml` to `$HOME/.config/hw-review/runner.toml` and set
   `relay_url`. Write the relay's `RUNNER_TOKEN` into the token file it names, mode 600.
3. `mise run image-smoke` checks the image without spending anything.
4. `mise run runner` runs the runner in the foreground. For a service:
   `mise run runner-install` (a systemd user unit), then `loginctl enable-linger` so it
   keeps running after you log out. Logs: `journalctl --user -u hw-review-runner -f`.

Each job leaves a directory under `work_root`: the clone, the shared-drive files, the
check outputs, Claude Code's transcript and the report. The newest `keep_jobs` stay.

To update: `git pull`, `mise run image`, then `systemctl --user restart hw-review-runner`.
To move to a newer Claude Code, change `CLAUDE_CODE_VERSION` in `container/Containerfile`,
rebuild, and run the smoke test.

## 5. Board repository

1. Copy `action/example-workflow.yml` to `.github/workflows/` and set `path` to the
   directory holding the KiCad project.
2. Settings > Secrets and variables > Actions > Variables: `HW_REVIEW_RELAY_URL` = the
   Worker URL. No secrets are needed: the relay checks the job's GitHub OIDC token,
   which names the repository, commit and run.
3. Add the repository to the relay's `ALLOWED_REPOS` (and `DRIVE_CONFIG`).
4. Push to the review branch. The job shows the queue position and the review's
   progress, then saves the report as an artifact and in the job summary, with a link
   to the .docx in the shared drive. Run the workflow by hand to choose another effort
   or budget. A newer push replaces a review that is still queued; a running review
   finishes.

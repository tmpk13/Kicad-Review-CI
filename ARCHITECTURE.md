# Architecture

## Components

```mermaid
flowchart LR
    subgraph GH[GitHub]
        push[push to the review branch] --> job[Actions job<br/>hw-review action]
        repos[(public repositories)]
    end
    subgraph CF[Cloudflare]
        worker[relay Worker<br/>relay/src/index.ts]
        hub[(Hub Durable Object<br/>SQLite job queue)]
        worker <--> hub
    end
    subgraph G[Google]
        gas[Apps Script web app<br/>apps-script/Code.gs]
        drive[(shared drive)]
        gas <--> drive
    end
    subgraph H[runner host]
        runner[review_runner.py]
        subgraph C[podman container]
            ep[entrypoint.py]
            cc[Claude Code]
            tools[kicad-cli, git, pandoc]
        end
        runner -- starts, stops --> C
    end
    api[Anthropic API]
    web[(datasheet sites)]

    job -- "OIDC token: create, poll, report, cancel" --> worker
    runner -- "runner token: claim, heartbeat, finish, Drive" --> worker
    worker -- "script key: list, get, upload" --> gas
    cc -- "per-job API token" --> worker
    worker -- "real API key" --> api
    ep -- "anonymous HTTPS clone" --> repos
    cc -- "WebFetch, wget" --> web
```

Who holds which credential:

| Credential | Held by | Grants |
|---|---|---|
| GitHub OIDC token | the Actions job (minted per request, minutes long) | create and read jobs of that one repository |
| `RUNNER_TOKEN` | relay, runner host | claim jobs, report results, Drive for the claimed job |
| per-job API token | relay (hash only), runner, container | the Anthropic API through the relay, while its job runs |
| `ANTHROPIC_API_KEY` | relay | the Anthropic API |
| `APPS_SCRIPT_KEY` | relay, Apps Script | the Drive bridge, inside its `ALLOWED_ROOTS` |

## Job states

```mermaid
stateDiagram-v2
    [*] --> queued: POST /v1/jobs (OIDC)
    queued --> running: a runner claims it
    queued --> superseded: newer push to the same ref
    queued --> canceled: canceled from GitHub
    queued --> expired: unclaimed for QUEUE_TTL_MINUTES
    running --> done: the runner reports with a report
    running --> failed: no report, lost heartbeat, time limit or runner restart
    running --> canceled: cancel seen at the next heartbeat
    failed --> done: a late result from the runner
    done --> [*]
```

## One review over time

A typical push; times are illustrative.

```mermaid
gantt
    dateFormat HH:mm
    axisFormat %H:%M
    section GitHub job
    submit, poll every minute      :g1, 00:00, 62m
    artifact and summary           :g2, after g1, 1m
    section Relay job
    queued                         :q, 00:00, 1m
    running                        :r, after q, 61m
    section Runner
    claim, Drive download          :d, 00:01, 1m
    container                      :c, after d, 59m
    docx upload, finish            :f, after c, 1m
    section Container
    clone repository and libraries :e1, 00:02, 2m
    ERC, DRC, exports              :e2, after e1, 3m
    Claude Code review             :e3, after e2, 53m
    run record, docx               :e4, after e3, 1m
```

## Files

- `relay/`: the Worker (`src/index.ts` routes, `src/hub.ts` queue, `src/oidc.ts` token
  check, `src/config.ts` settings) and its tests, including the relay in workerd.
- `runner/`: the host runner, its example config and the systemd installer.
- `container/`: the image (`Containerfile`), the entrypoint, the review prompts, the
  helper tools and the KiCad library map.
- `action/`: the composite GitHub action and an example board workflow.
- `apps-script/`: the Drive bridge and its manifest.
- `tests/`: Python tests, the stand-ins for podman and claude, the end-to-end test and
  the image smoke test.

// The job queue: one SQLite-backed Durable Object instance holds every job.
//
// Concurrency: SQL calls on a Durable Object are synchronous, and a method only yields
// at an await. Each state change below runs its reads and writes with no await in
// between, so two requests can never claim or finish the same job twice.

import { DurableObject } from "cloudflare:workers";
import { loadConfig, type Env, type JobOptions } from "./config";
import type { GithubClaims } from "./oidc";

export type JobStatus = "queued" | "running" | "done" | "failed" | "canceled" | "superseded" | "expired";

const DAY_MS = 24 * 3600 * 1000;
const KEEP_MS = 30 * DAY_MS;
const MAX_UPLOADS = 3;

const SCHEMA = [
    `CREATE TABLE IF NOT EXISTS jobs (
        id TEXT PRIMARY KEY,
        repo TEXT NOT NULL,
        repo_id TEXT NOT NULL,
        ref TEXT NOT NULL,
        sha TEXT NOT NULL,
        event TEXT NOT NULL,
        actor TEXT NOT NULL,
        run_id TEXT NOT NULL,
        run_attempt TEXT NOT NULL,
        workflow_ref TEXT NOT NULL,
        options TEXT NOT NULL,
        status TEXT NOT NULL,
        cancel_requested INTEGER NOT NULL DEFAULT 0,
        runner TEXT,
        token_hash TEXT,
        uploads INTEGER NOT NULL DEFAULT 0,
        progress TEXT,
        error TEXT,
        summary TEXT,
        report TEXT,
        log_tail TEXT,
        created_at INTEGER NOT NULL,
        started_at INTEGER,
        heartbeat_at INTEGER,
        finished_at INTEGER
    )`,
    "CREATE UNIQUE INDEX IF NOT EXISTS jobs_run ON jobs(repo, run_id, run_attempt)",
    "CREATE INDEX IF NOT EXISTS jobs_status ON jobs(status, created_at)",
    "CREATE INDEX IF NOT EXISTS jobs_token ON jobs(token_hash)",
];

const COLUMNS = `id, repo, repo_id, ref, sha, event, actor, run_id, run_attempt, workflow_ref, options, status,
    cancel_requested, runner, uploads, progress, error, summary, created_at, started_at, heartbeat_at, finished_at,
    (report IS NOT NULL) AS has_report`;

interface Row {
    id: string;
    repo: string;
    repo_id: string;
    ref: string;
    sha: string;
    event: string;
    actor: string;
    run_id: string;
    run_attempt: string;
    workflow_ref: string;
    options: string;
    status: JobStatus;
    cancel_requested: number;
    runner: string | null;
    uploads: number;
    progress: string | null;
    error: string | null;
    summary: string | null;
    created_at: number;
    started_at: number | null;
    heartbeat_at: number | null;
    finished_at: number | null;
    has_report: number;
}

// Options as shown outside the relay: Drive folder IDs stay inside.
export interface PublicOptions {
    path: string;
    model: string;
    effort: string;
    budget_usd: number;
    max_runtime_minutes: number;
    drive_folders: number;
    drive_upload: boolean;
}

export interface JobView {
    id: string;
    repo: string;
    ref: string;
    sha: string;
    event: string;
    actor: string;
    run_id: string;
    run_attempt: string;
    workflow_ref: string;
    status: JobStatus;
    cancel_requested: boolean;
    runner: string | null;
    progress: string | null;
    error: string | null;
    summary: unknown;
    options: PublicOptions;
    queue_position: number | null;
    created_at: string;
    started_at: string | null;
    finished_at: string | null;
    has_report: boolean;
}

export type CreateResult =
    | { ok: true; created: boolean; job: JobView; superseded: string[] }
    | { ok: false; status: number; error: string };

export interface RunnerResult {
    status: "done" | "failed" | "canceled";
    report?: string | null;
    summary?: unknown;
    error?: string | null;
    log_tail?: string | null;
}

export interface DriveJob {
    id: string;
    read: string[];
    upload: string | null;
    uploads: number;
}

function iso(ms: number | null): string | null {
    return ms === null ? null : new Date(ms).toISOString();
}

function clip(value: string | null | undefined, max: number): string | null {
    if (value === null || value === undefined) return null;
    return value.length > max ? value.slice(0, max) + "\n[truncated by relay]" : value;
}

export class Hub extends DurableObject<Env> {
    private sql: SqlStorage;

    constructor(ctx: DurableObjectState, env: Env) {
        super(ctx, env);
        this.sql = ctx.storage.sql;
        for (const statement of SCHEMA) this.sql.exec(statement);
    }

    private rows(query: string, ...args: SqlStorageValue[]): Row[] {
        return this.sql.exec(query, ...args).toArray() as unknown as Row[];
    }

    private row(query: string, ...args: SqlStorageValue[]): Row | null {
        return this.rows(query, ...args)[0] ?? null;
    }

    private view(r: Row): JobView {
        const o = JSON.parse(r.options) as JobOptions;
        let summary: unknown = null;
        if (r.summary) {
            try {
                summary = JSON.parse(r.summary);
            } catch {
                summary = null;
            }
        }
        let position: number | null = null;
        if (r.status === "queued") {
            const ahead = this.sql
                .exec("SELECT COUNT(*) AS n FROM jobs WHERE status = 'queued' AND created_at < ?", r.created_at)
                .one().n as number;
            position = ahead + 1;
        }
        return {
            id: r.id,
            repo: r.repo,
            ref: r.ref,
            sha: r.sha,
            event: r.event,
            actor: r.actor,
            run_id: r.run_id,
            run_attempt: r.run_attempt,
            workflow_ref: r.workflow_ref,
            status: r.status,
            cancel_requested: r.cancel_requested === 1,
            runner: r.runner,
            progress: r.progress,
            error: r.error,
            summary,
            options: {
                path: o.path,
                model: o.model,
                effort: o.effort,
                budget_usd: o.budget_usd,
                max_runtime_minutes: o.max_runtime_minutes,
                drive_folders: o.drive_read.length,
                drive_upload: o.drive_upload !== null,
            },
            queue_position: position,
            created_at: iso(r.created_at)!,
            started_at: iso(r.started_at),
            finished_at: iso(r.finished_at),
            has_report: r.has_report === 1,
        };
    }

    // Close jobs nobody will finish. Runs at the start of the busier calls.
    private sweep(now: number): void {
        const config = loadConfig(this.env);
        const stale = now - config.staleMinutes * 60_000;
        this.sql.exec(
            `UPDATE jobs SET status = 'failed', token_hash = NULL, finished_at = ?,
                error = 'the runner stopped sending heartbeats'
             WHERE status = 'running' AND heartbeat_at < ?`,
            now,
            stale,
        );
        this.sql.exec(
            `UPDATE jobs SET status = 'failed', token_hash = NULL, finished_at = ?,
                error = 'the job ran past its time limit'
             WHERE status = 'running' AND started_at < ? - (json_extract(options, '$.max_runtime_minutes') + 15) * 60000`,
            now,
            now,
        );
        this.sql.exec(
            `UPDATE jobs SET status = 'expired', finished_at = ?,
                error = 'no runner took the job; is the runner host up?'
             WHERE status = 'queued' AND created_at < ?`,
            now,
            now - config.queueTtlMinutes * 60_000,
        );
        this.sql.exec("DELETE FROM jobs WHERE created_at < ?", now - KEEP_MS);
    }

    createJob(input: {
        claims: Pick<
            GithubClaims,
            "repository" | "repository_id" | "ref" | "sha" | "event_name" | "actor" | "run_id" | "run_attempt" | "workflow_ref"
        >;
        options: JobOptions;
    }): CreateResult {
        const now = Date.now();
        this.sweep(now);
        const c = input.claims;
        const repo = c.repository.toLowerCase();
        const existing = this.row(
            `SELECT ${COLUMNS} FROM jobs WHERE repo = ? AND run_id = ? AND run_attempt = ?`,
            repo,
            c.run_id,
            c.run_attempt,
        );
        if (existing) return { ok: true, created: false, job: this.view(existing), superseded: [] };
        const limit = loadConfig(this.env).maxJobsPerDay;
        const recent = this.sql.exec("SELECT COUNT(*) AS n FROM jobs WHERE created_at > ?", now - DAY_MS).one()
            .n as number;
        if (recent >= limit) {
            return { ok: false, status: 429, error: `the relay's limit of ${limit} jobs per 24 hours is reached` };
        }
        const id = crypto.randomUUID();
        const superseded = this.rows("SELECT id FROM jobs WHERE repo = ? AND ref = ? AND status = 'queued'", repo, c.ref).map(
            (r) => r.id,
        );
        this.sql.exec(
            `UPDATE jobs SET status = 'superseded', finished_at = ?, error = ?
             WHERE repo = ? AND ref = ? AND status = 'queued'`,
            now,
            `superseded by job ${id} for ${c.sha.slice(0, 12)}`,
            repo,
            c.ref,
        );
        this.sql.exec(
            `INSERT INTO jobs (id, repo, repo_id, ref, sha, event, actor, run_id, run_attempt, workflow_ref,
                options, status, created_at)
             VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?)`,
            id,
            repo,
            c.repository_id,
            c.ref,
            c.sha,
            c.event_name,
            c.actor,
            c.run_id,
            c.run_attempt,
            c.workflow_ref,
            JSON.stringify(input.options),
            now,
        );
        const job = this.view(this.row(`SELECT ${COLUMNS} FROM jobs WHERE id = ?`, id)!);
        return { ok: true, created: true, job, superseded };
    }

    getJob(id: string, repo: string): JobView | null {
        this.sweep(Date.now());
        const r = this.row(`SELECT ${COLUMNS} FROM jobs WHERE id = ? AND repo = ?`, id, repo.toLowerCase());
        return r ? this.view(r) : null;
    }

    getReport(id: string, repo: string): string | null {
        const r = this.sql
            .exec("SELECT report FROM jobs WHERE id = ? AND repo = ?", id, repo.toLowerCase())
            .toArray()[0] as { report: string | null } | undefined;
        return r?.report ?? null;
    }

    cancelJob(id: string, repo: string): { ok: boolean; status: JobStatus | null } {
        const now = Date.now();
        const r = this.row(`SELECT ${COLUMNS} FROM jobs WHERE id = ? AND repo = ?`, id, repo.toLowerCase());
        if (!r) return { ok: false, status: null };
        if (r.status === "queued") {
            this.sql.exec(
                "UPDATE jobs SET status = 'canceled', finished_at = ?, error = 'canceled from GitHub' WHERE id = ?",
                now,
                id,
            );
            return { ok: true, status: "canceled" };
        }
        if (r.status === "running") {
            this.sql.exec("UPDATE jobs SET cancel_requested = 1 WHERE id = ?", id);
            return { ok: true, status: "running" };
        }
        return { ok: true, status: r.status };
    }

    // Hand the oldest queued job to a runner. The caller made the API token and passes
    // only its hash, so the relay never stores a usable token.
    claim(runner: string, tokenHash: string): { job: JobView; options: JobOptions } | null {
        const now = Date.now();
        this.sweep(now);
        // A runner asks for work only when idle, so a job it still holds was lost in a restart.
        this.sql.exec(
            `UPDATE jobs SET status = 'failed', token_hash = NULL, finished_at = ?,
                error = 'the runner restarted while the job was running'
             WHERE status = 'running' AND runner = ?`,
            now,
            runner,
        );
        const r = this.row(`SELECT ${COLUMNS} FROM jobs WHERE status = 'queued' ORDER BY created_at LIMIT 1`);
        if (!r) return null;
        this.sql.exec(
            `UPDATE jobs SET status = 'running', runner = ?, token_hash = ?, started_at = ?, heartbeat_at = ?,
                progress = 'claimed by the runner'
             WHERE id = ?`,
            runner,
            tokenHash,
            now,
            now,
            r.id,
        );
        const job = this.view(this.row(`SELECT ${COLUMNS} FROM jobs WHERE id = ?`, r.id)!);
        return { job, options: JSON.parse(r.options) as JobOptions };
    }

    heartbeat(id: string, runner: string, progress: string | null): { ok: boolean; cancel: boolean } {
        const now = Date.now();
        const r = this.row(`SELECT ${COLUMNS} FROM jobs WHERE id = ?`, id);
        if (!r || r.runner !== runner || r.status !== "running") return { ok: false, cancel: true };
        this.sql.exec(
            "UPDATE jobs SET heartbeat_at = ?, progress = COALESCE(?, progress) WHERE id = ?",
            now,
            clip(progress, 500),
            id,
        );
        return { ok: true, cancel: r.cancel_requested === 1 };
    }

    // Accept a result from the runner that holds the job. A job the sweep failed for
    // missed heartbeats still takes a late result: the report is worth keeping.
    finish(id: string, runner: string, result: RunnerResult): { ok: boolean; error?: string } {
        const now = Date.now();
        const r = this.row(`SELECT ${COLUMNS} FROM jobs WHERE id = ?`, id);
        if (!r || r.runner !== runner) return { ok: false, error: "no such job for this runner" };
        if (r.status !== "running" && !(r.status === "failed" && r.finished_at !== null)) {
            return { ok: false, error: `job is ${r.status}` };
        }
        const status = ["done", "failed", "canceled"].includes(result.status) ? result.status : "failed";
        let summary: string | null = null;
        if (result.summary !== undefined && result.summary !== null) summary = clip(JSON.stringify(result.summary), 64_000);
        this.sql.exec(
            `UPDATE jobs SET status = ?, token_hash = NULL, finished_at = ?, report = ?, summary = ?, error = ?,
                log_tail = ?, progress = ?
             WHERE id = ?`,
            status,
            now,
            clip(result.report, 4_000_000),
            summary,
            clip(result.error, 2_000),
            clip(result.log_tail, 64_000),
            `finished: ${status}`,
            id,
        );
        return { ok: true };
    }

    // Anthropic proxy check: the token is live only while its job runs, within its time limit.
    authApiToken(tokenHash: string): string | null {
        const now = Date.now();
        const r = this.row(`SELECT ${COLUMNS} FROM jobs WHERE token_hash = ? AND status = 'running'`, tokenHash);
        if (!r || r.started_at === null) return null;
        const o = JSON.parse(r.options) as JobOptions;
        if (now > r.started_at + (o.max_runtime_minutes + 15) * 60_000) return null;
        return r.id;
    }

    driveJob(id: string, runner: string): DriveJob | null {
        const r = this.row(`SELECT ${COLUMNS} FROM jobs WHERE id = ? AND runner = ? AND status = 'running'`, id, runner);
        if (!r) return null;
        const o = JSON.parse(r.options) as JobOptions;
        return { id: r.id, read: o.drive_read, upload: o.drive_upload, uploads: r.uploads };
    }

    noteUpload(id: string, runner: string): { ok: boolean; error?: string } {
        const r = this.row(`SELECT ${COLUMNS} FROM jobs WHERE id = ? AND runner = ? AND status = 'running'`, id, runner);
        if (!r) return { ok: false, error: "no running job for this runner" };
        if (r.uploads >= MAX_UPLOADS) return { ok: false, error: `a job may upload at most ${MAX_UPLOADS} files` };
        this.sql.exec("UPDATE jobs SET uploads = uploads + 1 WHERE id = ?", id);
        return { ok: true };
    }

    listJobs(limit: number): JobView[] {
        this.sweep(Date.now());
        return this.rows(`SELECT ${COLUMNS} FROM jobs ORDER BY created_at DESC LIMIT ?`, Math.min(limit, 200)).map((r) =>
            this.view(r),
        );
    }
}

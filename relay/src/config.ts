// Relay configuration, read from Worker variables and secrets set in the Cloudflare
// dashboard. Nothing here is a secret by itself; the secrets stay in `env`.

import type { Hub } from "./hub";

export interface Env {
    HUB: DurableObjectNamespace<Hub>;
    // Secrets
    ANTHROPIC_API_KEY?: string;
    RUNNER_TOKEN?: string;
    APPS_SCRIPT_URL?: string;
    APPS_SCRIPT_KEY?: string;
    // Plain variables
    ALLOWED_REPOS?: string;
    ALLOWED_EVENTS?: string;
    OIDC_AUDIENCE?: string;
    DRIVE_CONFIG?: string;
    DEFAULT_MODEL?: string;
    ALLOWED_MODELS?: string;
    DEFAULT_EFFORT?: string;
    DEFAULT_BUDGET_USD?: string;
    MAX_BUDGET_USD?: string;
    MAX_JOBS_PER_DAY?: string;
    MAX_RUNTIME_MINUTES?: string;
    STALE_MINUTES?: string;
    QUEUE_TTL_MINUTES?: string;
    // Overrides for local tests only
    GITHUB_OIDC_JWKS_URL?: string;
    ANTHROPIC_UPSTREAM?: string;
}

export interface DriveTarget {
    read: string[];
    upload: string | null;
}

export interface Config {
    allowedRepos: string[];
    allowedEvents: string[];
    audience: string;
    jwksUrl: string;
    drive: Record<string, DriveTarget>;
    defaultModel: string;
    allowedModels: string[];
    defaultEffort: string;
    defaultBudgetUsd: number;
    maxBudgetUsd: number;
    maxJobsPerDay: number;
    maxRuntimeMinutes: number;
    staleMinutes: number;
    queueTtlMinutes: number;
    anthropicUpstream: string;
}

export const EFFORTS = ["low", "medium", "high", "xhigh", "max"];
export const GITHUB_ISSUER = "https://token.actions.githubusercontent.com";

export class HttpError extends Error {
    constructor(public status: number, message: string) {
        super(message);
    }
}

function list(value: string | undefined, fallback: string): string[] {
    return (value ?? fallback)
        .split(",")
        .map((s) => s.trim())
        .filter(Boolean);
}

function number(value: string | undefined, fallback: number, name: string): number {
    if (value === undefined || value.trim() === "") return fallback;
    const n = Number(value);
    if (!Number.isFinite(n) || n <= 0) throw new HttpError(500, `relay variable ${name} must be a positive number`);
    return n;
}

function parseDrive(value: string | undefined): Record<string, DriveTarget> {
    if (!value || !value.trim()) return {};
    let raw: unknown;
    try {
        raw = JSON.parse(value);
    } catch {
        throw new HttpError(500, "relay variable DRIVE_CONFIG is not valid JSON");
    }
    if (typeof raw !== "object" || raw === null || Array.isArray(raw)) {
        throw new HttpError(500, "relay variable DRIVE_CONFIG must be a JSON object");
    }
    const out: Record<string, DriveTarget> = {};
    for (const [pattern, entry] of Object.entries(raw as Record<string, unknown>)) {
        const e = (entry ?? {}) as { read?: unknown; upload?: unknown };
        const read = Array.isArray(e.read) ? e.read.filter((x): x is string => typeof x === "string" && isDriveId(x)) : [];
        const upload = typeof e.upload === "string" && isDriveId(e.upload) ? e.upload : null;
        out[pattern.toLowerCase()] = { read, upload };
    }
    return out;
}

export function isDriveId(value: string): boolean {
    return /^[A-Za-z0-9_-]{10,100}$/.test(value);
}

export function loadConfig(env: Env): Config {
    const defaultModel = (env.DEFAULT_MODEL ?? "claude-opus-5-5").trim();
    const defaultEffort = (env.DEFAULT_EFFORT ?? "high").trim();
    if (!EFFORTS.includes(defaultEffort)) throw new HttpError(500, "relay variable DEFAULT_EFFORT is not a known effort level");
    const maxBudgetUsd = number(env.MAX_BUDGET_USD, 40, "MAX_BUDGET_USD");
    return {
        allowedRepos: list(env.ALLOWED_REPOS, "").map((s) => s.toLowerCase()),
        allowedEvents: list(env.ALLOWED_EVENTS, "push,workflow_dispatch"),
        audience: (env.OIDC_AUDIENCE ?? "hw-review-relay").trim(),
        jwksUrl: env.GITHUB_OIDC_JWKS_URL || `${GITHUB_ISSUER}/.well-known/jwks`,
        drive: parseDrive(env.DRIVE_CONFIG),
        defaultModel,
        allowedModels: [...new Set([defaultModel, ...list(env.ALLOWED_MODELS, "")])],
        defaultEffort,
        defaultBudgetUsd: Math.min(number(env.DEFAULT_BUDGET_USD, 20, "DEFAULT_BUDGET_USD"), maxBudgetUsd),
        maxBudgetUsd,
        maxJobsPerDay: number(env.MAX_JOBS_PER_DAY, 12, "MAX_JOBS_PER_DAY"),
        maxRuntimeMinutes: number(env.MAX_RUNTIME_MINUTES, 180, "MAX_RUNTIME_MINUTES"),
        // A running job with no runner heartbeat for this long has lost its runner.
        staleMinutes: number(env.STALE_MINUTES, 10, "STALE_MINUTES"),
        // A queued job nobody claims for this long expires (runner host down).
        queueTtlMinutes: number(env.QUEUE_TTL_MINUTES, 360, "QUEUE_TTL_MINUTES"),
        anthropicUpstream: env.ANTHROPIC_UPSTREAM || "https://api.anthropic.com",
    };
}

// A pattern is "owner/name" or "owner/*". Anything else (a bare "*" included) matches
// nothing, so an empty or mistyped ALLOWED_REPOS refuses every repository.
export function repoAllowed(repo: string, patterns: string[]): boolean {
    const [owner, name] = repo.toLowerCase().split("/");
    if (!owner || !name) return false;
    return patterns.some((p) => {
        const [po, pn, ...rest] = p.split("/");
        if (!po || !pn || rest.length) return false;
        return po === owner && (pn === "*" || pn === name);
    });
}

// Drive folders for a repository: an exact entry wins over "owner/*", which wins over "*".
export function driveFor(repo: string, drive: Record<string, DriveTarget>): DriveTarget {
    const r = repo.toLowerCase();
    const owner = r.split("/")[0];
    return drive[r] ?? drive[`${owner}/*`] ?? drive["*"] ?? { read: [], upload: null };
}

export interface JobRequest {
    path?: unknown;
    model?: unknown;
    effort?: unknown;
    budget_usd?: unknown;
}

export interface JobOptions {
    path: string;
    model: string;
    effort: string;
    budget_usd: number;
    max_runtime_minutes: number;
    drive_read: string[];
    drive_upload: string | null;
}

// Turn the optional fields of a job request into checked options. The repository and
// commit never come from the request body: they come from the verified OIDC token.
export function jobOptions(body: JobRequest, repo: string, config: Config): JobOptions {
    let path = typeof body.path === "string" ? body.path.trim() : ".";
    path = path.replace(/^\.\//, "").replace(/\/+$/, "") || ".";
    if (
        path.length > 200 ||
        path.startsWith("/") ||
        path.split("/").some((part) => part === ".." || part === "") ||
        !/^[A-Za-z0-9._ /-]+$/.test(path)
    ) {
        throw new HttpError(400, "path must be a repository-relative directory");
    }
    const model = body.model === undefined || body.model === "" ? config.defaultModel : body.model;
    if (typeof model !== "string" || !config.allowedModels.includes(model)) {
        throw new HttpError(400, `model must be one of: ${config.allowedModels.join(", ")}`);
    }
    const effort = body.effort === undefined || body.effort === "" ? config.defaultEffort : body.effort;
    if (typeof effort !== "string" || !EFFORTS.includes(effort)) {
        throw new HttpError(400, `effort must be one of: ${EFFORTS.join(", ")}`);
    }
    let budget = config.defaultBudgetUsd;
    if (body.budget_usd !== undefined && body.budget_usd !== "") {
        const n = Number(body.budget_usd);
        if (!Number.isFinite(n) || n <= 0) throw new HttpError(400, "budget_usd must be a positive number");
        if (n > config.maxBudgetUsd) throw new HttpError(400, `budget_usd may not exceed ${config.maxBudgetUsd}`);
        budget = n;
    }
    const drive = driveFor(repo, config.drive);
    return {
        path,
        model,
        effort,
        budget_usd: budget,
        max_runtime_minutes: config.maxRuntimeMinutes,
        drive_read: drive.read,
        drive_upload: drive.upload,
    };
}

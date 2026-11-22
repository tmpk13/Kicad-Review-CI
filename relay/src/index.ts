// hw-review relay: a Cloudflare Worker between GitHub Actions, the review runner and
// the outside services.
//
//   GitHub Actions (OIDC token)  POST /v1/jobs, GET /v1/jobs/:id[/report], POST .../cancel
//   Runner (RUNNER_TOKEN)        POST /v1/runner/claim, .../heartbeat, .../finish,
//                                GET .../drive/list, .../drive/file/:id, POST .../drive/upload
//   Container (job API token)    /anthropic/v1/messages and friends, proxied with the real key
//
// The main module may export only handlers and Durable Object classes: workerd treats
// every named export as an entrypoint and refuses to start on anything else.

import { Hub, type CreateResult, type JobView, type RunnerResult } from "./hub";
import { HttpError, jobOptions, loadConfig, repoAllowed, type Env, type JobRequest } from "./config";
import { verifyGithubToken, type GithubClaims } from "./oidc";
import { bearer, json, randomToken, readJson, secretEquals, sha256hex } from "./http";

export { Hub };

type HubStub = DurableObjectStub<Hub>;

const JOB = "([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})";

// Endpoints Claude Code needs. Batches, Files and admin routes stay closed.
const ANTHROPIC_ROUTES: Array<[string, RegExp]> = [
    ["POST", /^\/v1\/messages$/],
    ["POST", /^\/v1\/messages\/count_tokens$/],
    ["GET", /^\/v1\/models$/],
    ["GET", /^\/v1\/models\/[A-Za-z0-9._-]+$/],
];

// Request headers not passed upstream: credentials, cookies and Cloudflare's own.
const DROP_HEADERS = new Set(["host", "x-api-key", "authorization", "cookie", "x-forwarded-for", "x-forwarded-proto", "x-real-ip"]);

function anthropicError(status: number, type: string, message: string): Response {
    return json({ type: "error", error: { type, message } }, status);
}

async function githubClaims(request: Request, env: Env): Promise<GithubClaims> {
    const config = loadConfig(env);
    const token = bearer(request);
    if (!token) throw new HttpError(401, "missing GitHub OIDC token (the job needs permissions: id-token: write)");
    const claims = await verifyGithubToken(token, { audience: config.audience, jwksUrl: config.jwksUrl });
    if (!repoAllowed(claims.repository, config.allowedRepos)) {
        throw new HttpError(403, `repository ${claims.repository} is not on this relay's ALLOWED_REPOS list`);
    }
    return claims;
}

async function runnerName(request: Request, env: Env): Promise<string> {
    if (!env.RUNNER_TOKEN) throw new HttpError(503, "RUNNER_TOKEN is not set on the relay");
    const token = bearer(request);
    if (!token || !(await secretEquals(token, env.RUNNER_TOKEN))) throw new HttpError(401, "bad runner token");
    const name = request.headers.get("x-runner-name") ?? "runner";
    if (!/^[A-Za-z0-9._-]{1,64}$/.test(name)) throw new HttpError(400, "x-runner-name must be 1 to 64 of A-Z a-z 0-9 . _ -");
    return name;
}

async function proxyAnthropic(request: Request, env: Env, hub: HubStub, url: URL): Promise<Response> {
    const token = request.headers.get("x-api-key") ?? bearer(request);
    if (!token) return anthropicError(401, "authentication_error", "missing job API token");
    const jobId = await hub.authApiToken(await sha256hex(token));
    if (!jobId) return anthropicError(401, "authentication_error", "job API token is not valid: unknown, or its job has ended");
    if (!env.ANTHROPIC_API_KEY) return anthropicError(503, "api_error", "ANTHROPIC_API_KEY is not set on the relay");
    const sub = url.pathname.slice("/anthropic".length);
    if (!ANTHROPIC_ROUTES.some(([method, re]) => method === request.method && re.test(sub))) {
        return anthropicError(403, "permission_error", `${request.method} ${sub} is not available through the relay`);
    }
    const target = new URL(sub + url.search, loadConfig(env).anthropicUpstream);
    const headers = new Headers();
    for (const [name, value] of request.headers) {
        if (!DROP_HEADERS.has(name) && !name.startsWith("cf-")) headers.set(name, value);
    }
    headers.set("x-api-key", env.ANTHROPIC_API_KEY);
    const upstream = await fetch(target.toString(), {
        method: request.method,
        headers,
        body: request.method === "GET" ? null : request.body,
        redirect: "manual",
    });
    console.log(
        JSON.stringify({ event: "anthropic", job: jobId, path: sub, status: upstream.status, request_id: upstream.headers.get("request-id") }),
    );
    return new Response(upstream.body, { status: upstream.status, statusText: upstream.statusText, headers: upstream.headers });
}

// Forward to the Apps Script web app. Its answer streams through unparsed: a file comes
// back as base64 inside JSON and can be tens of megabytes, more than the Workers free
// plan's CPU budget could parse.
async function appsScript(env: Env, params: Record<string, string>, body?: ArrayBuffer): Promise<Response> {
    if (!env.APPS_SCRIPT_URL || !env.APPS_SCRIPT_KEY) {
        return json({ ok: false, error: "Drive access is not configured on the relay (APPS_SCRIPT_URL, APPS_SCRIPT_KEY)" }, 503);
    }
    const target = new URL(env.APPS_SCRIPT_URL);
    target.searchParams.set("key", env.APPS_SCRIPT_KEY);
    for (const [k, v] of Object.entries(params)) target.searchParams.set(k, v);
    const resp = await fetch(
        target.toString(),
        body ? { method: "POST", body, headers: { "content-type": "text/plain" }, redirect: "follow" } : { redirect: "follow" },
    );
    const type = resp.headers.get("content-type") ?? "";
    if (!type.includes("json")) {
        await resp.body?.cancel();
        return json(
            {
                ok: false,
                error: `Apps Script answered HTTP ${resp.status} with ${type || "no content type"}; check the web app's access setting and its execution log`,
            },
            502,
        );
    }
    return new Response(resp.body, { status: resp.ok ? 200 : 502, headers: { "content-type": "application/json; charset=utf-8" } });
}

async function handle(request: Request, env: Env): Promise<Response> {
    const url = new URL(request.url);
    const path = url.pathname;
    const method = request.method;
    const hub: HubStub = env.HUB.get(env.HUB.idFromName("hub"));
    let m: RegExpExecArray | null;

    if (path === "/" || path === "/healthz") {
        return new Response("hw-review relay\n", { headers: { "content-type": "text/plain" } });
    }
    if (path.startsWith("/anthropic/")) return proxyAnthropic(request, env, hub, url);

    // GitHub Actions
    if (path === "/v1/jobs" && method === "POST") {
        const claims = await githubClaims(request, env);
        const config = loadConfig(env);
        if (!config.allowedEvents.includes(claims.event_name)) {
            throw new HttpError(403, `${claims.event_name} events may not start reviews (ALLOWED_EVENTS)`);
        }
        const body = await readJson<JobRequest>(request, 16_000);
        const options = jobOptions(body, claims.repository, config);
        // RPC stubs wrap return types, which defeats narrowing on `ok`; restore the declared type.
        const result = (await hub.createJob({
            claims: {
                repository: claims.repository,
                repository_id: claims.repository_id,
                ref: claims.ref,
                sha: claims.sha,
                event_name: claims.event_name,
                actor: claims.actor,
                run_id: claims.run_id,
                run_attempt: claims.run_attempt,
                workflow_ref: claims.workflow_ref,
            },
            options,
        })) as CreateResult;
        if (!result.ok) return json({ error: result.error }, result.status);
        return json({ job: result.job, created: result.created, superseded: result.superseded }, result.created ? 201 : 200);
    }
    if ((m = new RegExp(`^/v1/jobs/${JOB}$`).exec(path)) && method === "GET") {
        const claims = await githubClaims(request, env);
        const job = await hub.getJob(m[1], claims.repository);
        return job ? json({ job }) : json({ error: "no such job for this repository" }, 404);
    }
    if ((m = new RegExp(`^/v1/jobs/${JOB}/report$`).exec(path)) && method === "GET") {
        const claims = await githubClaims(request, env);
        const report = await hub.getReport(m[1], claims.repository);
        if (report === null) return json({ error: "no report for this job" }, 404);
        return new Response(report, { headers: { "content-type": "text/markdown; charset=utf-8", "cache-control": "no-store" } });
    }
    if ((m = new RegExp(`^/v1/jobs/${JOB}/cancel$`).exec(path)) && method === "POST") {
        const claims = await githubClaims(request, env);
        const result = await hub.cancelJob(m[1], claims.repository);
        return result.ok ? json(result) : json({ error: "no such job for this repository" }, 404);
    }

    // Runner
    if (path === "/v1/runner/claim" && method === "POST") {
        const runner = await runnerName(request, env);
        const token = randomToken("hwr_");
        const claimed = (await hub.claim(runner, await sha256hex(token))) as { job: JobView } | null;
        if (!claimed) return new Response(null, { status: 204 });
        return json({ job: claimed.job, api_token: token, anthropic_base_url: `${url.origin}/anthropic` });
    }
    if (path === "/v1/runner/jobs" && method === "GET") {
        await runnerName(request, env);
        return json({ jobs: await hub.listJobs(Number(url.searchParams.get("limit") ?? "20")) });
    }
    if ((m = new RegExp(`^/v1/runner/jobs/${JOB}/heartbeat$`).exec(path)) && method === "POST") {
        const runner = await runnerName(request, env);
        const body = await readJson<{ progress?: unknown }>(request, 8_000);
        const progress = typeof body.progress === "string" ? body.progress : null;
        return json(await hub.heartbeat(m[1], runner, progress));
    }
    if ((m = new RegExp(`^/v1/runner/jobs/${JOB}/finish$`).exec(path)) && method === "POST") {
        const runner = await runnerName(request, env);
        const body = await readJson<RunnerResult>(request, 5_000_000);
        const result = await hub.finish(m[1], runner, body);
        return result.ok ? json(result) : json({ error: result.error }, 409);
    }
    if ((m = new RegExp(`^/v1/runner/jobs/${JOB}/drive/(list|file/([A-Za-z0-9_-]{10,100})|upload)$`).exec(path))) {
        const runner = await runnerName(request, env);
        const job = await hub.driveJob(m[1], runner);
        if (!job) return json({ ok: false, error: "no running job for this runner" }, 404);
        if (m[2] === "list" && method === "GET") {
            if (!job.read.length) return json({ ok: true, files: [], note: "no Drive folders are configured for this repository" });
            return appsScript(env, { action: "list", roots: job.read.join(",") });
        }
        if (m[3] && method === "GET") {
            if (!job.read.length) return json({ ok: false, error: "no Drive folders are configured for this repository" }, 404);
            return appsScript(env, { action: "get", roots: job.read.join(","), id: m[3] });
        }
        if (m[2] === "upload" && method === "POST") {
            if (!job.upload) return json({ ok: false, error: "no Drive upload folder is configured for this repository" }, 409);
            const name = url.searchParams.get("name") ?? "";
            if (!/^[A-Za-z0-9._ ()-]{1,150}\.(docx|md|pdf)$/.test(name)) {
                return json({ ok: false, error: "name must be a plain .docx, .md or .pdf file name" }, 400);
            }
            const noted = await hub.noteUpload(job.id, runner);
            if (!noted.ok) return json({ ok: false, error: noted.error }, 429);
            const data = await request.arrayBuffer();
            if (data.byteLength > 40_000_000) return json({ ok: false, error: "upload over 40 MB" }, 413);
            return appsScript(env, { action: "upload", folder: job.upload, name }, data);
        }
    }
    return json({ error: "not found" }, 404);
}

export default {
    async fetch(request: Request, env: Env): Promise<Response> {
        try {
            return await handle(request, env);
        } catch (e) {
            if (e instanceof HttpError) return json({ error: e.message }, e.status);
            console.error(e);
            return json({ error: "internal error" }, 500);
        }
    },
} satisfies ExportedHandler<Env>;

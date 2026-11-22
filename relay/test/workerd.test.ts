// The relay running in workerd (through Miniflare, which wrangler installs), with a
// SQLite Durable Object and fake GitHub, Anthropic and Apps Script services.
// Under Bun, call workerd with plain fetch on the URL from mf.ready: mf.dispatchFetch
// relies on an undici dispatcher that Bun's fetch ignores.

import { afterAll, beforeAll, describe, expect, test } from "bun:test";
import { existsSync } from "node:fs";
import { join } from "node:path";
import { claims, makeSigner, startUpstreams, type FakeUpstreams, type Signer } from "./helpers";

const ROOT = join(import.meta.dir, "..");
const HAVE_MINIFLARE = existsSync(join(ROOT, "node_modules/miniflare/package.json"));
const RUNNER_TOKEN = "runner-secret-token";
const ANTHROPIC_KEY = "sk-ant-real-key";

interface Relay {
    url: string;
    dispose(): Promise<void>;
}

let script: string;

export async function startRelay(up: FakeUpstreams, extra: Record<string, string> = {}): Promise<Relay> {
    const { Miniflare, convertV4MiniflareOptions } = await import("miniflare");
    if (!script) {
        const built = await Bun.build({
            entrypoints: [join(ROOT, "src/index.ts")],
            format: "esm",
            target: "browser",
            external: ["cloudflare:workers"],
        });
        if (!built.success) throw new Error(built.logs.map(String).join("\n"));
        script = await built.outputs[0].text();
    }
    const mf = new Miniflare(
        convertV4MiniflareOptions({
            modules: true,
            script,
            compatibilityDate: "2026-10-01",
            durableObjects: { HUB: { className: "Hub", useSQLite: true } },
            bindings: {
                ANTHROPIC_API_KEY: ANTHROPIC_KEY,
                RUNNER_TOKEN,
                APPS_SCRIPT_URL: `${up.url}/exec`,
                APPS_SCRIPT_KEY: "apps-script-key",
                ALLOWED_REPOS: "oresat/*",
                DRIVE_CONFIG: JSON.stringify({ "oresat/*": { read: ["readFolder001"], upload: "uploadFolder1" } }),
                GITHUB_OIDC_JWKS_URL: `${up.url}/jwks`,
                ANTHROPIC_UPSTREAM: up.url,
                ...extra,
            },
        }),
    );
    const base = await mf.ready;
    return { url: base.toString().replace(/\/$/, ""), dispose: () => mf.dispose() };
}

describe.skipIf(!HAVE_MINIFLARE)("relay in workerd", () => {
    let signer: Signer;
    let up: FakeUpstreams;
    let relay: Relay;

    beforeAll(async () => {
        signer = await makeSigner();
        up = startUpstreams(signer);
        relay = await startRelay(up, { MAX_JOBS_PER_DAY: "4" });
    });
    afterAll(async () => {
        await relay?.dispose();
        up?.stop();
    });

    const gh = async (method: string, path: string, c: Record<string, unknown>, body?: unknown) =>
        fetch(relay.url + path, {
            method,
            headers: { authorization: `Bearer ${await signer.sign(c)}`, "content-type": "application/json" },
            body: body === undefined ? undefined : JSON.stringify(body),
        });
    const runner = (method: string, path: string, body?: unknown, token = RUNNER_TOKEN, name = "bench") =>
        fetch(relay.url + path, {
            method,
            headers: { authorization: `Bearer ${token}`, "x-runner-name": name, "content-type": "application/json" },
            body: body === undefined ? undefined : typeof body === "string" ? body : JSON.stringify(body),
        });

    test("full job lifecycle", async () => {
        // GitHub side: authentication and authorization
        expect((await fetch(`${relay.url}/v1/jobs`, { method: "POST" })).status).toBe(401);
        expect((await gh("POST", "/v1/jobs", claims({ repository: "someone/fork" }), {})).status).toBe(403);
        expect((await gh("POST", "/v1/jobs", claims({ event_name: "pull_request_target" }), {})).status).toBe(403);
        expect((await gh("POST", "/v1/jobs", claims(), { path: "../x" })).status).toBe(400);

        // Two pushes to the same branch: the second supersedes the first while it waits.
        const c1 = claims({ sha: "1111111111111111111111111111111111111111" });
        const r1 = await gh("POST", "/v1/jobs", c1, { path: "oresat-battery-card" });
        expect(r1.status).toBe(201);
        const j1 = ((await r1.json()) as any).job;
        expect(j1).toMatchObject({ status: "queued", queue_position: 1, repo: "oresat/oresat-batteries" });
        expect(j1.options).toMatchObject({ path: "oresat-battery-card", model: "claude-opus-5-5", drive_folders: 1, drive_upload: true });
        const again = await gh("POST", "/v1/jobs", c1, {});
        expect(again.status).toBe(200);
        expect(((await again.json()) as any).job.id).toBe(j1.id);

        const c2 = claims({ sha: "2222222222222222222222222222222222222222" });
        const r2 = await gh("POST", "/v1/jobs", c2, {});
        expect(r2.status).toBe(201);
        const created2 = (await r2.json()) as any;
        expect(created2.superseded).toEqual([j1.id]);
        const j2 = created2.job;
        expect((((await (await gh("GET", `/v1/jobs/${j1.id}`, c1)).json()) as any).job).status).toBe("superseded");
        // Another repository cannot see the job.
        expect((await gh("GET", `/v1/jobs/${j2.id}`, claims({ repository: "oresat/other" }))).status).toBe(404);

        // Runner side
        expect((await runner("POST", "/v1/runner/claim", {}, "wrong")).status).toBe(401);
        const claim = await runner("POST", "/v1/runner/claim", {});
        expect(claim.status).toBe(200);
        const claimed = (await claim.json()) as any;
        expect(claimed.job).toMatchObject({ id: j2.id, sha: c2.sha, status: "running", runner: "bench" });
        expect(claimed.api_token).toMatch(/^hwr_/);
        expect(claimed.anthropic_base_url).toBe(`${relay.url}/anthropic`);
        expect(JSON.stringify(claimed)).not.toContain("readFolder001");
        // A second runner finds nothing. (The same runner asking again would mean it
        // restarted, and the relay would fail the job it held.)
        expect((await runner("POST", "/v1/runner/claim", {}, RUNNER_TOKEN, "spare")).status).toBe(204);

        // Anthropic proxy: the job token goes in, the real key goes upstream, the stream comes back.
        const token = claimed.api_token as string;
        expect((await fetch(`${relay.url}/anthropic/v1/messages`, { method: "POST", body: "{}" })).status).toBe(401);
        const msg = await fetch(`${relay.url}/anthropic/v1/messages?beta=true`, {
            method: "POST",
            headers: { "x-api-key": token, "anthropic-version": "2023-06-01", "content-type": "application/json" },
            body: JSON.stringify({ model: "claude-opus-5-5", max_tokens: 10, stream: true, messages: [] }),
        });
        expect(msg.status).toBe(200);
        expect(msg.headers.get("content-type")).toContain("text/event-stream");
        const text = await msg.text();
        expect(text).toContain("message_start");
        expect(text).toContain("message_stop");
        const seen = up.requests.filter((r) => r.path === "/v1/messages").at(-1)!;
        expect(seen.headers["x-api-key"]).toBe(ANTHROPIC_KEY);
        expect(seen.query.beta).toBe("true");
        expect(seen.headers["anthropic-version"]).toBe("2023-06-01");
        expect(JSON.stringify(seen.headers)).not.toContain(token);
        const bearerModels = await fetch(`${relay.url}/anthropic/v1/models`, { headers: { authorization: `Bearer ${token}` } });
        expect(bearerModels.status).toBe(200);
        const batch = await fetch(`${relay.url}/anthropic/v1/messages/batches`, { method: "POST", headers: { "x-api-key": token } });
        expect(batch.status).toBe(403);

        // Drive, through the runner
        const list = (await (await runner("GET", `/v1/runner/jobs/${j2.id}/drive/list`)).json()) as any;
        expect(list.files[0].path).toBe("docs/requirements.md");
        const listCall = up.requests.filter((r) => r.query.action === "list").at(-1)!;
        expect(listCall.query).toMatchObject({ key: "apps-script-key", roots: "readFolder001" });
        const file = (await (await runner("GET", `/v1/runner/jobs/${j2.id}/drive/file/file0000001`)).json()) as any;
        expect(atob(file.data)).toBe("hello");
        expect((await runner("POST", `/v1/runner/jobs/${j2.id}/drive/upload?name=../x.docx`, "QUJD")).status).toBe(400);
        const upload = await runner("POST", `/v1/runner/jobs/${j2.id}/drive/upload?name=review-board-1808a28.docx`, "QUJD");
        expect(upload.status).toBe(200);
        expect(((await upload.json()) as any).url).toBe("https://drive.example/uploaded001");
        const upCall = up.requests.filter((r) => r.query.action === "upload").at(-1)!;
        expect(upCall).toMatchObject({ method: "POST", body: "QUJD" });
        expect(upCall.query).toMatchObject({ folder: "uploadFolder1", name: "review-board-1808a28.docx" });
        expect((await runner("POST", `/v1/runner/jobs/${j2.id}/drive/upload?name=b.docx`, "QQ")).status).toBe(200);
        expect((await runner("POST", `/v1/runner/jobs/${j2.id}/drive/upload?name=c.docx`, "QQ")).status).toBe(200);
        expect((await runner("POST", `/v1/runner/jobs/${j2.id}/drive/upload?name=d.docx`, "QQ")).status).toBe(429);

        // Progress, then a cancel from GitHub reaches the runner on its next heartbeat.
        const hb = (await (await runner("POST", `/v1/runner/jobs/${j2.id}/heartbeat`, { progress: "turn 12, $1.40" })).json()) as any;
        expect(hb).toEqual({ ok: true, cancel: false });
        const polled = ((await (await gh("GET", `/v1/jobs/${j2.id}`, c2)).json()) as any).job;
        expect(polled).toMatchObject({ status: "running", progress: "turn 12, $1.40" });
        expect(((await (await gh("POST", `/v1/jobs/${j2.id}/cancel`, c2)).json()) as any).status).toBe("running");
        expect(((await (await runner("POST", `/v1/runner/jobs/${j2.id}/heartbeat`, {})).json()) as any).cancel).toBe(true);

        // Finish: the report is stored, the API token dies.
        const fin = await runner("POST", `/v1/runner/jobs/${j2.id}/finish`, {
            status: "done",
            report: "# Review\n\n**Revision reviewed** - `review` @ `2222`",
            summary: { cost_usd: 3.2, num_turns: 40, drive_url: "https://drive.example/uploaded001" },
        });
        expect(fin.status).toBe(200);
        const done = ((await (await gh("GET", `/v1/jobs/${j2.id}`, c2)).json()) as any).job;
        expect(done).toMatchObject({ status: "done", has_report: true, summary: { cost_usd: 3.2 } });
        const report = await gh("GET", `/v1/jobs/${j2.id}/report`, c2);
        expect(report.headers.get("content-type")).toContain("text/markdown");
        expect(await report.text()).toContain("Revision reviewed");
        const after = await fetch(`${relay.url}/anthropic/v1/messages`, { method: "POST", headers: { "x-api-key": token }, body: "{}" });
        expect(after.status).toBe(401);
        expect((await runner("POST", `/v1/runner/jobs/${j2.id}/finish`, { status: "done" })).status).toBe(409);

        // Daily cap: 2 jobs so far, the limit is 4.
        expect((await gh("POST", "/v1/jobs", claims({ ref: "refs/heads/a" }), {})).status).toBe(201);
        expect((await gh("POST", "/v1/jobs", claims({ ref: "refs/heads/b" }), {})).status).toBe(201);
        expect((await gh("POST", "/v1/jobs", claims({ ref: "refs/heads/c" }), {})).status).toBe(429);
    });

    test("an HTML answer from Apps Script becomes a JSON error", async () => {
        const r2 = await startRelay(up, { APPS_SCRIPT_URL: `${up.url}/exec-html` });
        try {
            const c = claims();
            await fetch(`${r2.url}/v1/jobs`, { method: "POST", headers: { authorization: `Bearer ${await signer.sign(c)}` }, body: "{}" });
            const claim = (await (
                await fetch(`${r2.url}/v1/runner/claim`, { method: "POST", headers: { authorization: `Bearer ${RUNNER_TOKEN}` } })
            ).json()) as any;
            const list = await fetch(`${r2.url}/v1/runner/jobs/${claim.job.id}/drive/list`, {
                headers: { authorization: `Bearer ${RUNNER_TOKEN}` },
            });
            expect(list.status).toBe(502);
            const body = (await list.json()) as any;
            expect(body.ok).toBe(false);
            expect(body.error).toContain("text/html");
        } finally {
            await r2.dispose();
        }
    });

    test("stale and unclaimed jobs are closed by the sweep", async () => {
        const r3 = await startRelay(up, { STALE_MINUTES: "0.002", QUEUE_TTL_MINUTES: "0.002" });
        try {
            const post = async (c: Record<string, unknown>) =>
                (await (
                    await fetch(`${r3.url}/v1/jobs`, {
                        method: "POST",
                        headers: { authorization: `Bearer ${await signer.sign(c)}` },
                        body: "{}",
                    })
                ).json()) as any;
            const get = async (id: string, c: Record<string, unknown>) =>
                ((await (await fetch(`${r3.url}/v1/jobs/${id}`, { headers: { authorization: `Bearer ${await signer.sign(c)}` } })).json()) as any)
                    .job;
            const cq = claims({ ref: "refs/heads/queued" });
            const queued = await post(cq);
            await Bun.sleep(300);
            expect(await get(queued.job.id, cq)).toMatchObject({ status: "expired" });

            const cr = claims({ ref: "refs/heads/running" });
            const running = await post(cr);
            const claim = await fetch(`${r3.url}/v1/runner/claim`, { method: "POST", headers: { authorization: `Bearer ${RUNNER_TOKEN}` } });
            expect(((await claim.json()) as any).job.id).toBe(running.job.id);
            await Bun.sleep(300);
            const failed = await get(running.job.id, cr);
            expect(failed.status).toBe("failed");
            expect(failed.error).toContain("heartbeats");
            // A late result from the runner is still accepted.
            const late = await fetch(`${r3.url}/v1/runner/jobs/${running.job.id}/finish`, {
                method: "POST",
                headers: { authorization: `Bearer ${RUNNER_TOKEN}` },
                body: JSON.stringify({ status: "done", report: "late but useful" }),
            });
            expect(late.status).toBe(200);
            expect((await get(running.job.id, cr)).status).toBe("done");
        } finally {
            await r3.dispose();
        }
    });
});

// Test doubles: a GitHub-like OIDC signer and one local server that plays GitHub's
// JWKS endpoint, the Anthropic API and the Apps Script web app.

export const AUDIENCE = "hw-review-relay";
export const KID = "test-key-1";

export interface Signer {
    jwks: { keys: JsonWebKey[] };
    sign(claims: Record<string, unknown>): Promise<string>;
}

function b64url(bytes: Uint8Array | string): string {
    const raw = typeof bytes === "string" ? new TextEncoder().encode(bytes) : bytes;
    let bin = "";
    for (const b of raw) bin += String.fromCharCode(b);
    return btoa(bin).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

export async function makeSigner(kid = KID): Promise<Signer> {
    const pair = (await crypto.subtle.generateKey(
        { name: "RSASSA-PKCS1-v1_5", modulusLength: 2048, publicExponent: new Uint8Array([1, 0, 1]), hash: "SHA-256" },
        true,
        ["sign", "verify"],
    )) as CryptoKeyPair;
    const pub = (await crypto.subtle.exportKey("jwk", pair.publicKey)) as JsonWebKey;
    return {
        jwks: { keys: [{ ...pub, kid, alg: "RS256", use: "sig" } as JsonWebKey] },
        async sign(claims) {
            const head = b64url(JSON.stringify({ alg: "RS256", typ: "JWT", kid }));
            const body = b64url(JSON.stringify(claims));
            const sig = await crypto.subtle.sign("RSASSA-PKCS1-v1_5", pair.privateKey, new TextEncoder().encode(`${head}.${body}`));
            return `${head}.${body}.${b64url(new Uint8Array(sig))}`;
        },
    };
}

let runCounter = 1000;

export function claims(overrides: Record<string, unknown> = {}): Record<string, unknown> {
    const now = Math.floor(Date.now() / 1000);
    const repository = (overrides.repository as string) ?? "oresat/oresat-batteries";
    const ref = (overrides.ref as string) ?? "refs/heads/review";
    return {
        iss: "https://token.actions.githubusercontent.com",
        aud: AUDIENCE,
        sub: `repo:${repository}:ref:${ref}`,
        iat: now,
        nbf: now - 5,
        exp: now + 300,
        repository,
        repository_id: "123456",
        repository_owner: repository.split("/")[0],
        repository_owner_id: "654321",
        ref,
        ref_type: "branch",
        sha: "1808a28aa1c2b3c4d5e6f708192a3b4c5d6e7f80",
        run_id: String(runCounter++),
        run_attempt: "1",
        event_name: "push",
        actor: "octocat",
        workflow_ref: `${repository}/.github/workflows/main.yml@${ref}`,
        ...overrides,
    };
}

export interface Recorded {
    method: string;
    path: string;
    query: Record<string, string>;
    headers: Record<string, string>;
    body: string;
}

export interface FakeUpstreams {
    url: string;
    requests: Recorded[];
    stop(): void;
}

export interface UpstreamOptions {
    // When set, a /v1/messages request that offers the Write tool (and holds no tool
    // result yet) is answered with a Write call creating this file: real Claude Code
    // then writes a report without a real model.
    writeReportTo?: string;
    reportText?: string;
}

function sse(events: unknown[]): Response {
    const stream = new ReadableStream({
        async start(controller) {
            for (const e of events) {
                const type = (e as { type: string }).type;
                controller.enqueue(new TextEncoder().encode(`event: ${type}\ndata: ${JSON.stringify(e)}\n\n`));
                await Bun.sleep(5);
            }
            controller.close();
        },
    });
    return new Response(stream, { headers: { "content-type": "text/event-stream", "request-id": "req_test" } });
}

let messageCounter = 0;

// A Messages API stand-in: plain text, or one Write tool call (see UpstreamOptions).
function fakeMessage(raw: string, opts: UpstreamOptions): Response {
    let body: any = {};
    try {
        body = JSON.parse(raw || "{}");
    } catch {
        body = {};
    }
    const messages: any[] = body.messages ?? [];
    const answered = messages.some((m) => Array.isArray(m.content) && m.content.some((b: any) => b.type === "tool_result"));
    const offersWrite = (body.tools ?? []).some((t: any) => t.name === "Write");
    const id = `msg_fake_${++messageCounter}`;
    const usage = { input_tokens: 25, output_tokens: 12, cache_creation_input_tokens: 0, cache_read_input_tokens: 0 };
    const write = opts.writeReportTo && offersWrite && !answered;
    const block = write
        ? { type: "tool_use", id: `toolu_fake_${messageCounter}`, name: "Write", input: { file_path: opts.writeReportTo, content: opts.reportText ?? "# Review\n" } }
        : { type: "text", text: "Review finished." };
    const stop = write ? "tool_use" : "end_turn";
    const message = { id, type: "message", role: "assistant", model: body.model ?? "claude-opus-5-5", content: [block], stop_reason: stop, stop_sequence: null, usage };
    if (!body.stream) return Response.json(message, { headers: { "request-id": "req_test" } });
    const startBlock = write ? { ...block, input: {} } : { type: "text", text: "" };
    const delta = write
        ? { type: "input_json_delta", partial_json: JSON.stringify((block as any).input) }
        : { type: "text_delta", text: (block as any).text };
    return sse([
        { type: "message_start", message: { ...message, content: [], stop_reason: null, usage: { ...usage, output_tokens: 1 } } },
        { type: "content_block_start", index: 0, content_block: startBlock },
        { type: "content_block_delta", index: 0, delta },
        { type: "content_block_stop", index: 0 },
        { type: "message_delta", delta: { stop_reason: stop, stop_sequence: null }, usage: { output_tokens: 12 } },
        { type: "message_stop" },
    ]);
}

// One server for every outside party the relay talks to, told apart by path prefix:
// GitHub's JWKS and OIDC token endpoints, the Anthropic API and the Apps Script web app.
// GET /_requests returns what it has seen, for tests running in another process.
export function startUpstreams(signer: Signer, opts: UpstreamOptions = {}): FakeUpstreams {
    const requests: Recorded[] = [];
    const server = Bun.serve({
        port: 0,
        hostname: "127.0.0.1",
        async fetch(req) {
            const u = new URL(req.url);
            if (u.pathname === "/_requests") return Response.json(requests);
            const rec: Recorded = {
                method: req.method,
                path: u.pathname,
                query: Object.fromEntries(u.searchParams),
                headers: Object.fromEntries(req.headers),
                body: await req.text(),
            };
            requests.push(rec);
            if (u.pathname === "/jwks") return Response.json(signer.jwks);
            if (u.pathname === "/oidc") {
                // Like ACTIONS_ID_TOKEN_REQUEST_URL: the other query parameters override claims.
                const { audience, ...over } = rec.query;
                return Response.json({ value: await signer.sign(claims({ ...over, aud: audience })) });
            }
            if (u.pathname === "/v1/messages") return fakeMessage(rec.body, opts);
            if (u.pathname === "/v1/messages/count_tokens") return Response.json({ input_tokens: 42 });
            if (u.pathname === "/v1/models") return Response.json({ data: [{ id: "claude-opus-5-5" }] });
            if (u.pathname === "/exec") {
                const action = u.searchParams.get("action");
                if (u.searchParams.get("key") !== "apps-script-key") return Response.json({ ok: false, error: "unauthorized" });
                if (action === "list") {
                    return Response.json({
                        ok: true,
                        files: [{ id: "file0000001", path: "docs/requirements.md", mimeType: "text/markdown", size: 5 }],
                    });
                }
                if (action === "get") {
                    return Response.json({ ok: true, id: u.searchParams.get("id"), name: "requirements.md", data: btoa("hello") });
                }
                if (action === "upload") {
                    return Response.json({ ok: true, id: "uploaded001", url: "https://drive.example/uploaded001" });
                }
                return Response.json({ ok: false, error: "unknown action" });
            }
            // What Apps Script serves when the deployment is not open to anonymous callers.
            if (u.pathname === "/exec-html") return new Response("<html>sign in</html>", { headers: { "content-type": "text/html" } });
            return new Response("not found", { status: 404 });
        },
    });
    return { url: `http://127.0.0.1:${server.port}`, requests, stop: () => server.stop(true) };
}

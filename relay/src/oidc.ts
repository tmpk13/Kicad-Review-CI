// Verify GitHub Actions OIDC tokens (RS256 JWTs) with WebCrypto, no dependencies.
// A workflow job with `permissions: id-token: write` can request one; its claims name
// the repository, commit, ref and run that the job belongs to.

import { GITHUB_ISSUER, HttpError } from "./config";

export interface GithubClaims {
    iss: string;
    aud: string | string[];
    sub: string;
    exp: number;
    iat?: number;
    nbf?: number;
    repository: string;
    repository_id: string;
    repository_owner: string;
    ref: string;
    sha: string;
    run_id: string;
    run_attempt: string;
    event_name: string;
    actor: string;
    workflow_ref: string;
}

const REQUIRED = [
    "sub",
    "repository",
    "repository_id",
    "repository_owner",
    "ref",
    "sha",
    "run_id",
    "run_attempt",
    "event_name",
    "actor",
    "workflow_ref",
] as const;

const LEEWAY_S = 60;
const REFETCH_S = 60;

interface KeyCache {
    url: string;
    keys: Map<string, CryptoKey>;
    fetchedAt: number;
}

// Per-isolate cache. GitHub rotates keys rarely; an unknown kid triggers one refetch.
let cache: KeyCache | null = null;

export function b64urlDecode(s: string): Uint8Array {
    if (!/^[A-Za-z0-9_-]*$/.test(s)) throw new Error("bad base64url");
    const b64 = s.replace(/-/g, "+").replace(/_/g, "/") + "===".slice((s.length + 3) % 4);
    const bin = atob(b64);
    const out = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
    return out;
}

function decodeJson(part: string): Record<string, unknown> {
    const value = JSON.parse(new TextDecoder().decode(b64urlDecode(part)));
    if (typeof value !== "object" || value === null) throw new Error("not an object");
    return value as Record<string, unknown>;
}

async function loadKeys(url: string, now: number): Promise<Map<string, CryptoKey>> {
    const resp = await fetch(url, { headers: { accept: "application/json" } });
    if (!resp.ok) throw new HttpError(503, `could not fetch GitHub signing keys (HTTP ${resp.status})`);
    const body = (await resp.json()) as { keys?: Array<JsonWebKey & { kid?: string }> };
    const keys = new Map<string, CryptoKey>();
    for (const jwk of body.keys ?? []) {
        if (jwk.kty !== "RSA" || !jwk.kid || !jwk.n || !jwk.e) continue;
        const key = await crypto.subtle.importKey(
            "jwk",
            { kty: "RSA", n: jwk.n, e: jwk.e, alg: "RS256", ext: true },
            { name: "RSASSA-PKCS1-v1_5", hash: "SHA-256" },
            false,
            ["verify"],
        );
        keys.set(jwk.kid, key);
    }
    cache = { url, keys, fetchedAt: now };
    return keys;
}

async function keyFor(kid: string, url: string, now: number): Promise<CryptoKey | undefined> {
    if (cache && cache.url === url) {
        const key = cache.keys.get(kid);
        if (key || now - cache.fetchedAt < REFETCH_S) return key;
    }
    return (await loadKeys(url, now)).get(kid);
}

export async function verifyGithubToken(
    token: string,
    opts: { audience: string; jwksUrl: string; now?: number },
): Promise<GithubClaims> {
    const now = opts.now ?? Math.floor(Date.now() / 1000);
    const parts = token.split(".");
    if (parts.length !== 3) throw new HttpError(401, "malformed token");
    const [h, p, s] = parts;
    let header: Record<string, unknown>;
    let claims: Record<string, unknown>;
    let signature: Uint8Array;
    try {
        header = decodeJson(h);
        claims = decodeJson(p);
        signature = b64urlDecode(s);
    } catch {
        throw new HttpError(401, "malformed token");
    }
    if (header.alg !== "RS256" || typeof header.kid !== "string") throw new HttpError(401, "unsupported token algorithm");
    const key = await keyFor(header.kid, opts.jwksUrl, now);
    if (!key) throw new HttpError(401, "token signed with an unknown key");
    const ok = await crypto.subtle.verify(
        "RSASSA-PKCS1-v1_5",
        key,
        signature,
        new TextEncoder().encode(`${h}.${p}`),
    );
    if (!ok) throw new HttpError(401, "bad token signature");
    if (claims.iss !== GITHUB_ISSUER) throw new HttpError(401, "token issuer is not GitHub Actions");
    const aud = Array.isArray(claims.aud) ? claims.aud : [claims.aud];
    if (!aud.includes(opts.audience)) throw new HttpError(401, `token audience must be ${opts.audience}`);
    if (typeof claims.exp !== "number" || claims.exp + LEEWAY_S < now) throw new HttpError(401, "token expired");
    if (typeof claims.nbf === "number" && claims.nbf - LEEWAY_S > now) throw new HttpError(401, "token not yet valid");
    for (const name of REQUIRED) {
        if (typeof claims[name] !== "string" || !claims[name]) throw new HttpError(401, `token lacks the ${name} claim`);
    }
    return claims as unknown as GithubClaims;
}

export function resetKeyCacheForTests(): void {
    cache = null;
}

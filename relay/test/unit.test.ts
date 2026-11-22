import { afterAll, beforeAll, beforeEach, describe, expect, test } from "bun:test";
import { verifyGithubToken, resetKeyCacheForTests, b64urlDecode } from "../src/oidc";
import { driveFor, HttpError, jobOptions, loadConfig, repoAllowed, type Env } from "../src/config";
import { AUDIENCE, claims, makeSigner, startUpstreams, type FakeUpstreams, type Signer } from "./helpers";

let signer: Signer;
let other: Signer;
let up: FakeUpstreams;

beforeAll(async () => {
    signer = await makeSigner();
    other = await makeSigner();
    up = startUpstreams(signer);
});
afterAll(() => up.stop());
beforeEach(() => resetKeyCacheForTests());

async function rejects(p: Promise<unknown>, message: RegExp) {
    let err: unknown;
    try {
        await p;
    } catch (e) {
        err = e;
    }
    expect(err).toBeInstanceOf(HttpError);
    expect((err as HttpError).message).toMatch(message);
    expect((err as HttpError).status).toBe(401);
}

describe("verifyGithubToken", () => {
    const opts = () => ({ audience: AUDIENCE, jwksUrl: `${up.url}/jwks` });

    test("accepts a well-formed token and returns its claims", async () => {
        const token = await signer.sign(claims({ sha: "abc123" }));
        const c = await verifyGithubToken(token, opts());
        expect(c.repository).toBe("oresat/oresat-batteries");
        expect(c.sha).toBe("abc123");
    });

    test("caches keys: a second token does not refetch the JWKS", async () => {
        const before = up.requests.filter((r) => r.path === "/jwks").length;
        await verifyGithubToken(await signer.sign(claims()), opts());
        await verifyGithubToken(await signer.sign(claims()), opts());
        expect(up.requests.filter((r) => r.path === "/jwks").length).toBe(before + 1);
    });

    test("rejects a token signed by another key with the same kid", async () => {
        await rejects(verifyGithubToken(await other.sign(claims()), opts()), /signature/);
    });

    test("rejects the wrong audience, issuer, and an expired token", async () => {
        await rejects(verifyGithubToken(await signer.sign(claims({ aud: "something-else" })), opts()), /audience/);
        await rejects(verifyGithubToken(await signer.sign(claims({ iss: "https://evil.example" })), opts()), /issuer/);
        const old = Math.floor(Date.now() / 1000) - 3600;
        await rejects(verifyGithubToken(await signer.sign(claims({ exp: old })), opts()), /expired/);
    });

    test("rejects a token without the sha claim", async () => {
        const c = claims();
        delete c.sha;
        await rejects(verifyGithubToken(await signer.sign(c), opts()), /sha/);
    });

    test("rejects garbage", async () => {
        await rejects(verifyGithubToken("not-a-jwt", opts()), /malformed/);
        await rejects(verifyGithubToken("a.b.c", opts()), /malformed/);
    });

    test("base64url decoding handles every padding length", () => {
        expect(new TextDecoder().decode(b64urlDecode("YQ"))).toBe("a");
        expect(new TextDecoder().decode(b64urlDecode("YWI"))).toBe("ab");
        expect(new TextDecoder().decode(b64urlDecode("YWJj"))).toBe("abc");
        expect(() => b64urlDecode("Y")).toThrow();
    });
});

describe("config", () => {
    const env = (over: Partial<Env> = {}) => ({ HUB: undefined as never, ALLOWED_REPOS: "oresat/*", ...over }) as Env;

    test("repository patterns match owner/* and exact names, case-insensitively", () => {
        const p = ["oresat/*", "tmpk13/review-test"];
        expect(repoAllowed("oresat/oresat-batteries", p)).toBe(true);
        expect(repoAllowed("OreSat/Oresat-Batteries", p)).toBe(true);
        expect(repoAllowed("tmpk13/review-test", p)).toBe(true);
        expect(repoAllowed("tmpk13/other", p)).toBe(false);
        expect(repoAllowed("someone/oresat-batteries", p)).toBe(false);
    });

    test("a bare * or an empty list allows nothing", () => {
        expect(repoAllowed("oresat/x", ["*"])).toBe(false);
        expect(repoAllowed("oresat/x", [])).toBe(false);
        expect(loadConfig(env({ ALLOWED_REPOS: "" })).allowedRepos).toEqual([]);
    });

    test("Drive lookup prefers the exact repository, then owner/*, then *", () => {
        const drive = loadConfig(
            env({
                DRIVE_CONFIG: JSON.stringify({
                    "oresat/oresat-batteries": { read: ["exactFolder01"], upload: "exactUpload01" },
                    "oresat/*": { read: ["ownerFolder01"] },
                    "*": { read: ["anyFolder0001"], upload: "anyUpload0001" },
                }),
            }),
        ).drive;
        expect(driveFor("oresat/oresat-batteries", drive)).toEqual({ read: ["exactFolder01"], upload: "exactUpload01" });
        expect(driveFor("OreSat/other", drive)).toEqual({ read: ["ownerFolder01"], upload: null });
        expect(driveFor("x/y", drive)).toEqual({ read: ["anyFolder0001"], upload: "anyUpload0001" });
    });

    test("job options apply defaults and refuse out-of-range requests", () => {
        const config = loadConfig(env({ MAX_BUDGET_USD: "30", ALLOWED_MODELS: "claude-sonnet-5-5" }));
        const o = jobOptions({}, "oresat/a", config);
        expect(o).toMatchObject({ path: ".", model: "claude-opus-5-5", effort: "high", budget_usd: 20 });
        expect(jobOptions({ path: "./oresat-battery-card/", effort: "max", budget_usd: "25" }, "oresat/a", config)).toMatchObject({
            path: "oresat-battery-card",
            effort: "max",
            budget_usd: 25,
        });
        expect(jobOptions({ model: "claude-sonnet-5-5" }, "oresat/a", config).model).toBe("claude-sonnet-5-5");
        expect(() => jobOptions({ path: "../etc" }, "oresat/a", config)).toThrow(/repository-relative/);
        expect(() => jobOptions({ path: "/abs" }, "oresat/a", config)).toThrow(/repository-relative/);
        expect(() => jobOptions({ path: "a//b" }, "oresat/a", config)).toThrow(/repository-relative/);
        expect(() => jobOptions({ model: "gpt-x" }, "oresat/a", config)).toThrow(/model/);
        expect(() => jobOptions({ effort: "turbo" }, "oresat/a", config)).toThrow(/effort/);
        expect(() => jobOptions({ budget_usd: 31 }, "oresat/a", config)).toThrow(/exceed/);
        expect(() => jobOptions({ budget_usd: -1 }, "oresat/a", config)).toThrow(/positive/);
    });

    test("bad DRIVE_CONFIG is reported, not ignored", () => {
        expect(() => loadConfig(env({ DRIVE_CONFIG: "{not json" }))).toThrow(/DRIVE_CONFIG/);
    });
});

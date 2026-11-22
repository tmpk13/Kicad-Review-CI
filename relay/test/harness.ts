// Start the relay in workerd through Miniflare (installed with wrangler), wired to the
// fake upstreams. Under Bun, talk to it with plain fetch on the URL from mf.ready:
// mf.dispatchFetch relies on an undici dispatcher that Bun's fetch ignores.

import { join } from "node:path";
import type { FakeUpstreams } from "./helpers";

export const RUNNER_TOKEN = "runner-secret-token";
export const ANTHROPIC_KEY = "sk-ant-real-key";
const ROOT = join(import.meta.dir, "..");

export interface Relay {
    url: string;
    dispose(): Promise<void>;
}

let script = "";

export async function startRelay(up: FakeUpstreams, extra: Record<string, string> = {}, port = 0): Promise<Relay> {
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
            host: "127.0.0.1",
            port,
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

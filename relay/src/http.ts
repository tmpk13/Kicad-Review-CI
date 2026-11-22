// Response, token and hashing helpers shared by the Worker routes.

import { HttpError } from "./config";

export function json(body: unknown, status = 200, headers: Record<string, string> = {}): Response {
    return new Response(JSON.stringify(body), {
        status,
        headers: { "content-type": "application/json; charset=utf-8", "cache-control": "no-store", ...headers },
    });
}

export function bearer(request: Request): string | null {
    const value = request.headers.get("authorization") ?? "";
    const m = /^Bearer\s+(\S+)$/i.exec(value);
    return m ? m[1] : null;
}

export async function sha256hex(value: string): Promise<string> {
    const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(value));
    return [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

// Compare two secrets without leaking where they differ: hash both, then compare the
// fixed-length digests in a loop that always runs to the end.
export async function secretEquals(a: string, b: string): Promise<boolean> {
    const [x, y] = await Promise.all([sha256hex(a), sha256hex(b)]);
    let diff = 0;
    for (let i = 0; i < x.length; i++) diff |= x.charCodeAt(i) ^ y.charCodeAt(i);
    return diff === 0;
}

export function randomToken(prefix: string): string {
    const bytes = crypto.getRandomValues(new Uint8Array(32));
    const b64 = btoa(String.fromCharCode(...bytes)).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
    return prefix + b64;
}

export async function readJson<T>(request: Request, maxBytes: number): Promise<T> {
    const length = Number(request.headers.get("content-length") ?? "0");
    if (length > maxBytes) throw new HttpError(413, `request body over ${maxBytes} bytes`);
    const text = await request.text();
    if (text.length > maxBytes) throw new HttpError(413, `request body over ${maxBytes} bytes`);
    if (!text.trim()) return {} as T;
    try {
        return JSON.parse(text) as T;
    } catch {
        throw new HttpError(400, "request body is not valid JSON");
    }
}

// Code.gs runs in a vm context with stand-ins for the Apps Script services and an
// in-memory Drive answering the REST calls it makes through UrlFetchApp.

import { beforeEach, describe, expect, test } from "bun:test";
import { createHash, randomUUID } from "node:crypto";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import vm from "node:vm";

const CODE = readFileSync(join(import.meta.dir, "../Code.gs"), "utf8");
const FOLDER = "application/vnd.google-apps.folder";

interface Item {
    id: string;
    name: string;
    mimeType: string;
    parents: string[];
    bytes?: Uint8Array;
}

// Root ROOTFOLDER01 ("Battery card") holds a PDF, a Google Doc, a shortcut, a subfolder
// with a nested file, and the reviews folder. OUTSIDE0001 is elsewhere in the drive.
function drive(): Map<string, Item> {
    const items: Item[] = [
        { id: "SHAREDDRIVE1", name: "OreSat", mimeType: FOLDER, parents: [] },
        { id: "ROOTFOLDER01", name: "Battery card", mimeType: FOLDER, parents: ["SHAREDDRIVE1"] },
        { id: "PDFFILE00001", name: "TPS25983.pdf", mimeType: "application/pdf", parents: ["ROOTFOLDER01"], bytes: new Uint8Array([37, 80, 68, 70]) },
        { id: "GDOCFILE0001", name: "Power budget", mimeType: "application/vnd.google-apps.document", parents: ["ROOTFOLDER01"] },
        { id: "SHORTCUT0001", name: "link", mimeType: "application/vnd.google-apps.shortcut", parents: ["ROOTFOLDER01"] },
        { id: "SUBFOLDER001", name: "Tests/2026", mimeType: FOLDER, parents: ["ROOTFOLDER01"] },
        { id: "NESTED000001", name: "thermal.csv", mimeType: "text/csv", parents: ["SUBFOLDER001"], bytes: new TextEncoder().encode("t,c\n") },
        { id: "REVIEWS00001", name: "Reviews", mimeType: FOLDER, parents: ["ROOTFOLDER01"] },
        { id: "OUTSIDE00001", name: "secret.pdf", mimeType: "application/pdf", parents: ["SHAREDDRIVE1"], bytes: new Uint8Array([1]) },
    ];
    return new Map(items.map((i) => [i.id, i]));
}

interface Fetched {
    url: URL;
    opts: any;
}

function context(store: Map<string, Item>, props: Record<string, string>) {
    const fetched: Fetched[] = [];
    const cache = new Map<string, string>();
    const reply = (code: number, body: unknown, bytes?: Uint8Array) => ({
        getResponseCode: () => code,
        getContentText: () => (typeof body === "string" ? body : JSON.stringify(body)),
        getBlob: () => ({ getBytes: () => Array.from(bytes ?? new Uint8Array()).map((b) => (b > 127 ? b - 256 : b)) }),
    });
    const UrlFetchApp = {
        fetch(raw: string, opts: any) {
            const url = new URL(raw);
            fetched.push({ url, opts });
            expect(opts.headers.Authorization).toBe("Bearer oauth-token");
            if (url.pathname === "/upload/drive/v3/files") {
                return reply(200, { id: "NEWFILE00001", name: "x", webViewLink: "https://drive.google.com/file/d/NEWFILE00001" });
            }
            const m = /^\/drive\/v3\/files(?:\/([^/]+))?(\/export)?$/.exec(url.pathname);
            if (!m) return reply(404, "no route");
            if (!m[1]) {
                const parent = /'([^']+)' in parents/.exec(url.searchParams.get("q") ?? "")![1];
                const files = [...store.values()].filter((i) => i.parents.includes(parent));
                return reply(200, { files: files.map((i) => ({ id: i.id, name: i.name, mimeType: i.mimeType, size: i.bytes ? String(i.bytes.length) : undefined })) });
            }
            const item = store.get(m[1]);
            if (!item) return reply(404, { error: "not found" });
            if (m[2]) return reply(200, "", new TextEncoder().encode(`# ${item.name}\n`));
            if (url.searchParams.get("alt") === "media") return reply(200, "", item.bytes);
            return reply(200, { id: item.id, name: item.name, mimeType: item.mimeType, size: item.bytes ? String(item.bytes.length) : undefined, parents: item.parents });
        },
    };
    const toSigned = (bytes: Uint8Array) => Array.from(bytes).map((b) => (b > 127 ? b - 256 : b));
    const ctx: Record<string, unknown> = {
        UrlFetchApp,
        ScriptApp: { getOAuthToken: () => "oauth-token" },
        PropertiesService: { getScriptProperties: () => ({ getProperty: (k: string) => props[k] ?? null }) },
        CacheService: { getScriptCache: () => ({ get: (k: string) => cache.get(k) ?? null, put: (k: string, v: string) => void cache.set(k, v) }) },
        ContentService: {
            MimeType: { JSON: "application/json" },
            createTextOutput: (text: string) => ({ text, setMimeType: (mime: string) => ({ text, mime }) }),
        },
        Utilities: {
            DigestAlgorithm: { SHA_256: "sha256" },
            computeDigest: (_alg: string, s: string) => toSigned(createHash("sha256").update(s).digest()),
            base64Encode: (bytes: number[]) => Buffer.from(bytes.map((b) => b & 255)).toString("base64"),
            base64Decode: (s: string) => toSigned(Buffer.from(s, "base64")),
            newBlob: (s: string) => ({ getBytes: () => toSigned(Buffer.from(s, "utf8")) }),
            getUuid: () => randomUUID(),
        },
        Logger: { log: () => {} },
    };
    vm.createContext(ctx);
    vm.runInContext(CODE, ctx);
    const call = (fn: "doGet" | "doPost", parameter: Record<string, string>, body?: string) => {
        const out = (ctx[fn] as Function)({ parameter, postData: body === undefined ? undefined : { contents: body } });
        expect(out.mime).toBe("application/json");
        return JSON.parse(out.text);
    };
    return { call, fetched };
}

describe("Code.gs", () => {
    let store: Map<string, Item>;
    let app: ReturnType<typeof context>;
    const props = { RELAY_KEY: "k3y-of-the-relay", ALLOWED_ROOTS: "ROOTFOLDER01", MAX_FILE_BYTES: "1000" };
    beforeEach(() => {
        store = drive();
        app = context(store, props);
    });
    const key = props.RELAY_KEY;

    test("refuses a missing or wrong key", () => {
        expect(app.call("doGet", { action: "ping" })).toEqual({ ok: false, error: "unauthorized" });
        expect(app.call("doGet", { action: "ping", key: "k3y-of-the-relaz" })).toEqual({ ok: false, error: "unauthorized" });
        expect(app.call("doGet", { action: "ping", key })).toEqual({ ok: true });
    });

    test("lists every file under the root, by path, without shortcuts", () => {
        const got = app.call("doGet", { action: "list", key, roots: "ROOTFOLDER01" });
        expect(got.ok).toBe(true);
        expect(got.files.map((f: any) => f.path).sort()).toEqual([
            "Battery card/Power budget",
            "Battery card/TPS25983.pdf",
            "Battery card/Tests_2026/thermal.csv",
        ]);
        expect(got.files.find((f: any) => f.id === "PDFFILE00001").size).toBe(4);
    });

    test("only folders in ALLOWED_ROOTS can be listed", () => {
        const got = app.call("doGet", { action: "list", key, roots: "SHAREDDRIVE1" });
        expect(got).toEqual({ ok: false, error: "none of the requested folders is in ALLOWED_ROOTS" });
    });

    test("downloads a file, exports a Google Doc as Markdown", () => {
        const pdf = app.call("doGet", { action: "get", key, roots: "ROOTFOLDER01", id: "PDFFILE00001" });
        expect(pdf).toMatchObject({ ok: true, name: "TPS25983.pdf", size: 4, exportedAs: null });
        expect(Buffer.from(pdf.data, "base64").toString()).toBe("%PDF");
        const nested = app.call("doGet", { action: "get", key, roots: "ROOTFOLDER01", id: "NESTED000001" });
        expect(Buffer.from(nested.data, "base64").toString()).toBe("t,c\n");
        const doc = app.call("doGet", { action: "get", key, roots: "ROOTFOLDER01", id: "GDOCFILE0001" });
        expect(doc).toMatchObject({ ok: true, name: "Power budget.md", exportedAs: "text/markdown" });
        const exportCall = app.fetched.find((f) => f.url.pathname.endsWith("/export"))!;
        expect(exportCall.url.searchParams.get("mimeType")).toBe("text/markdown");
    });

    test("refuses files outside the roots and files over the size cap", () => {
        const outside = app.call("doGet", { action: "get", key, roots: "ROOTFOLDER01", id: "OUTSIDE00001" });
        expect(outside).toEqual({ ok: false, error: "the file is outside the allowed folders" });
        store.get("PDFFILE00001")!.bytes = new Uint8Array(2000);
        const big = app.call("doGet", { action: "get", key, roots: "ROOTFOLDER01", id: "PDFFILE00001" });
        expect(big.ok).toBe(false);
        expect(big.error).toContain("over 1000 bytes");
    });

    test("uploads a new file into a folder inside the roots", () => {
        const docx = Buffer.from([0x50, 0x4b, 0x03, 0x04, 0xff]);
        const got = app.call("doPost", { action: "upload", key, folder: "REVIEWS00001", name: "board-review-2026-11-22-abc1234.docx" }, docx.toString("base64"));
        expect(got).toEqual({ ok: true, id: "NEWFILE00001", name: "x", url: "https://drive.google.com/file/d/NEWFILE00001" });
        const call = app.fetched.find((f) => f.url.pathname === "/upload/drive/v3/files")!;
        expect(call.url.searchParams.get("uploadType")).toBe("multipart");
        expect(call.url.searchParams.get("supportsAllDrives")).toBe("true");
        const payload = Buffer.from((call.opts.payload as number[]).map((b) => b & 255));
        const boundary = /boundary=(\S+)/.exec(call.opts.contentType)![1];
        const text = payload.toString("latin1");
        expect(text.startsWith(`--${boundary}\r\nContent-Type: application/json`)).toBe(true);
        expect(text).toContain('"parents":["REVIEWS00001"]');
        expect(text).toContain("wordprocessingml.document");
        expect(payload.includes(docx)).toBe(true);
        expect(text.endsWith(`\r\n--${boundary}--`)).toBe(true);
    });

    test("refuses uploads outside the roots, to files, and with odd names", () => {
        const b64 = Buffer.from("x").toString("base64");
        expect(app.call("doPost", { action: "upload", key, folder: "SHAREDDRIVE1", name: "a.docx" }, b64).error).toBe(
            "the upload folder is outside the allowed folders",
        );
        expect(app.call("doPost", { action: "upload", key, folder: "PDFFILE00001", name: "a.docx" }, b64).error).toBe(
            "the upload target is not a folder",
        );
        expect(app.call("doPost", { action: "upload", key, folder: "REVIEWS00001", name: "../a.docx" }, b64).error).toBe("bad file name");
        expect(app.call("doPost", { action: "upload", key, folder: "REVIEWS00001", name: "a.exe" }, b64).error).toBe("bad file name");
        expect(app.call("doPost", { action: "upload", key, folder: "REVIEWS00001", name: "a.docx" }, "").error).toBe("empty upload");
    });
});

// Run the relay in workerd with fake upstreams for tests in other processes (the Python
// end-to-end test and the image smoke test). Prints one JSON line with the URLs, then
// serves until stdin closes or a signal arrives.
//
//   bun relay/test/serve.ts [--write-report /out/report.md]
// --write-report makes the fake Anthropic API answer with a Write call creating that file.

import { startRelay } from "./harness";
import { makeSigner, startUpstreams } from "./helpers";

const args = process.argv.slice(2);
const at = args.indexOf("--write-report");
const writeReportTo = at >= 0 ? args[at + 1] : undefined;
const reportText = [
    "# Smoke-test review",
    "",
    "**Revision reviewed** - written by the fake model through the relay.",
    "",
    "| Check | Result |",
    "|---|---|",
    "| Relay proxy | the Write call came back through it |",
    "",
].join("\n");

const signer = await makeSigner();
const up = startUpstreams(signer, { writeReportTo, reportText });
const relay = await startRelay(up);
console.log(JSON.stringify({ relay: relay.url, upstream: up.url }));

async function shutdown(): Promise<never> {
    await relay.dispose();
    up.stop();
    process.exit(0);
}
process.on("SIGTERM", shutdown);
process.on("SIGINT", shutdown);
process.stdin.on("end", shutdown);
process.stdin.on("close", shutdown);
process.stdin.resume();

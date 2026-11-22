# hw-review-ci

Automated hardware design reviews. A push to a board repository's review branch asks a
Cloudflare relay for a review; a runner host picks up the job and runs Claude Code in a
podman container with git, KiCad 10 and the team's shared-drive files. The report comes
back to the GitHub run and lands in the shared drive as a .docx.

One-time setup: [SETUP.md](SETUP.md). How the parts connect: [ARCHITECTURE.md](ARCHITECTURE.md).
Why it is built this way: [NOTES.md](NOTES.md).

## Day to day

- `mise run test`: every offline test (relay in workerd, Apps Script, runner, action, end to end)
- `mise run image`: build the review container image
- `mise run image-smoke`: run one job through the built image against stand-in APIs
- `mise run runner`: run the job runner in the foreground
- `mise run relay-check`: type-check the relay and dry-run its Cloudflare bundle

# Questions

Each recommended option is already applied; an answer only redirects that work.
Answer on the `Answer:` line or in chat (`Q2 B`).

## Q2. Should the runner or the container upload the .docx to the shared drive?
Why it matters: whatever uploads needs a Drive credential, and the container runs an agent that reads untrusted datasheets and web pages.
- A. The runner (recommended): the container makes the .docx; the runner, outside the agent's reach, uploads it and also does the downloads.
- B. The container: what you described; it would need a Drive-capable token that an injected instruction could misuse.
Applied: A, in c11d91f.
Answer:

## Q3. Should the GitHub job wait for the review to finish?
Why it matters: a waiting job holds a hosted runner for the length of the review (up to five hours): free on public repositories, billed minutes on private ones.
- A. Wait and poll (recommended): one run shows the progress and carries the report.
- B. Return at once and deliver the report only to the shared drive.
Applied: A.
Answer:

## Q4. Are $20 per review (cap $40), Opus 5.5 at high effort and 12 reviews a day the right defaults?
Why it matters: they set the cost of every push; a full review with datasheet lookups runs long.
- A. As set (recommended): room for a full review; the Console spend limit is the backstop.
- B. Cheaper: for example $8 at medium effort, for shallower but more frequent reviews.
Applied: A, as the relay defaults (dashboard variables override them).
Answer:

## Q5. Is Python (standard library) acceptable for the runner and the action script, instead of Rust?
Why it matters: your language order puts Rust first.
- A. Python (recommended): the host needs only python3, the image already has it, and the code is HTTP and subprocess glue.
- B. Rust: a compiled runner, at the cost of a toolchain on the host or a glibc floor to manage.
Applied: A.
Answer:

## Q6. Which machine runs the runner, and must it be fenced from the local network?
Why it matters: the container has full internet access and runs an agent with a shell on untrusted text; on a lab or home network it could reach other devices.
- A. A dedicated machine or VM on a guest network or VLAN (recommended).
- B. This workstation, with an nftables rule that drops the container's traffic to private address ranges (to be written).
- C. This workstation as it is.
Applied: nothing installed; SETUP.md recommends A.
Answer:

# Private runtime and recovery CLI

## Scheduled publication integration

For the scheduled collect → grounded research → validated content PR → Cloudflare Pages → email workflow, use **[SCHEDULED_JOB.md](SCHEDULED_JOB.md)**. `runtime.weekly` freezes the scheduler's original Friday occurrence and full collection counts, checks research against that immutable selection, and binds explicit public provenance to the exact reviewed bytes. `runtime.publisher` is the content-only automatic publication adapter; `runtime.site_verify` supports the observed trusted Cloudflare Pages check-run plus exact production HTTPS readback without requiring `gh`.

The operator separately authorized automatic validated **weekly content only**. Source PRs still require human approval. The scheduled source launch gate refuses unmerged runtime/source changes; its pre-activation fallback preserves the existing email-only job and must not open another send path for any occurrence with private delivery state. The configuration examples below document the low-level adapter contract, including the deliberately disabled bootstrap setup and older GitHub Deployments evidence mode; they are not the production weekly settings.

Python 3.11+ on Linux, standard library only. Run commands from the repository root (`/home/hermes/projects/twihn`). No command installs a scheduler, changes DNS, creates a GitHub repository, or sends an operational alert. `resume` **can send the approved digest once** through the configured submission adapter; none of the tests use a real transport.

## Identity and immutable review

Every command requires the **intended Friday's date**, never a date derived from the retry clock:

```sh
python3 -m runtime bounds --issue 2026-11-13
# {"issueId":"2026-11-13","start":"2026-11-06T08:00:00Z","end":"2026-11-13T08:00:00Z",...}
```

The end is Friday 08:00 Europe/London, converted to UTC. Start is exactly 168 hours earlier in UTC, not seven local calendar days. The window is half-open `[start,end)`; DST transitions can change its UK-local starting hour. Non-Friday and malformed identifiers are rejected. Scheduler integration must pass the original scheduled date on retries and backfills.

Use those exact bounds with the repository's collector, not drifting `--days 7`:

```sh
python3 integrations/weekly_hn_digest.py \
  --start 2026-11-06T08:00:00Z --end 2026-11-13T08:00:00Z \
  --limit 10 --format selected-json > "$HOME/.hermes/data/twihn-selected.json"
```

Research **every unique selected item** and add `story` and `discussion` paragraphs. Repeat items must carry identical research/metrics in both independent ordinary rankings. Do not claim an inaccessible source was read; record its availability accurately. Collection and factual research are intentionally outside this deterministic runtime. A human/agent reviewer must approve the completed dataset; validation cannot establish factual truth.

`prep` freezes the **exact reviewed legacy JSON bytes**, their SHA-256, identity, seven-day bounds, revision, and independent pending channel states:

```sh
umask 077
python3 -m runtime prep --issue 2026-11-13 \
  --reviewed "$HOME/.hermes/data/twihn-reviewed.json" --approve-reviewed
```

Default renderer: `integrations/render_email.py`; `prep --renderer /absolute/path.py` can explicitly select a compatible renderer. Input remains the existing `{start,end,sections:[{heading,items:[...]}]}` format. The current compatibility gate deliberately requires ten entries in each first-three section, as the legacy renderer does. A genuinely smaller population currently stops email rather than inventing entries; support for fewer must be added consistently to collector, renderer and runtime. Empty Show HN is supported; there is no maximum Show count.

Re-preparing identical bytes is idempotent. Any byte change, changed summaries across rankings, duplicate IDs within a section, invalid order/metrics/window, missing summaries or unsafe legacy URLs is rejected. A whitespace-only change is still a different frozen digest. Revision 1 cannot be silently overwritten; factual corrections use the site's reviewed revision/retention workflow and must **not** reset private email state or cause a resend.

## Private durable state and lock

Default: `$HOME/.hermes/data/twihn/`; override with global `--state-dir /durable/private/path`. Never put it in Git, `data/`, `public/`, `dist/`, scratch, or a web-served directory. Directories are owned by the current UID and mode 0700; files are 0600. Symlink paths are rejected. Writes use same-directory temporary files, `fsync`, atomic replace, and directory `fsync`. Every public state-changing CLI operation takes one nonblocking `flock` across all issues/channels, serializing shared website/Git adapters. It fails clearly if another writer owns the lock.

Per issue: `reviewed.json`, `state.json`, `baseline.eml`, `send.eml`, `context.json`, `report.json`. The latter files can contain private sender/recipient details and adapter errors. No runtime file is public content. `state.json` carries separate website/email statuses and durable verification evidence.

Tests verify independent-process lock exclusion and real descendant process killing on the tested Linux filesystem. Before moving private state to NFS/FUSE or another persistent filesystem, rerun these tests with `TMPDIR` on that filesystem and verify its flock/rename/fsync guarantees. Do not infer distributed locking from local tests.

## Practical adapter configuration

Put configuration and credentials **outside the repository**, mode 0600. Sender and recipient have no committed production defaults; copy the already-approved values from the existing private job, never infer or substitute an address. Example `$HOME/.hermes/data/twihn-runtime.json` (example addresses must be replaced privately):

```json
{
  "sender": "approved-sender@example.org",
  "recipient": "approved-recipient@example.org",
  "cwd": "/home/hermes/projects/twihn",
  "command_timeout": 120,
  "website_timeout": 600,
  "email_preflight": ["python3", "-m", "runtime.adapters", "preflight", "--settings", "/home/hermes/.hermes/data/twihn-mail.json", "--context", "{context}"],
  "email_send": ["python3", "-m", "runtime.adapters", "send", "--settings", "/home/hermes/.hermes/data/twihn-mail.json", "--context", "{context}"],
  "email_verify": ["python3", "-m", "runtime.adapters", "verify", "--settings", "/home/hermes/.hermes/data/twihn-mail.json", "--context", "{context}"],
  "website_publish": ["python3", "-m", "runtime.adapters", "website-disabled", "--context", "{context}"]
}
```

This initial website adapter fails immediately and deliberately; the baseline email branch still runs. It is the honest configuration while repo/domain/deployment access is blocked, not a claim the website is deployed. A missing website adapter also fails closed without suppressing email.

Private `$HOME/.hermes/data/twihn-mail.json`:

```json
{
  "binary": "/home/hermes/.hermes/home/.local/bin/himalaya",
  "home": "/home/hermes/.hermes/home",
  "sent_mailbox": "Sent Items",
  "timeout": 30,
  "verify_page_size": 50,
  "verify_max_pages": 4
}
```

Optional `account` and `config` select an explicit Himalaya account/TOML. Retain the already-verified account; runtime does not configure credentials. These adapters use the installed v2 CLI contracts (`account check --json`, `envelope search --mailbox ... --json 'subject "START to END"'`, `message read --mailbox ... --raw ID`, `message send --save ... -- FILE`). They do not use obsolete `--folder`, `--output json`, or `message export` flags. Only the `send` action calls submission. Preflight checks account handshakes (including each reported backend) and searches Sent Items by the frozen weekly date window before checking the exact decoded subject. A legacy same-window subject blocks a new send even without Message-ID; labelled HTML tests do not. This server-side window filter avoids scanning unrelated mail in large Sent folders. Himalaya v2.2.1 returns `{queued,envelopes}`; the adapter accepts that wrapper and older bare arrays. Verification scans a bounded number of matching envelopes and reads raw copies, comparing Message-ID, exact From/To/Subject and decoded complete plain/HTML content. Extra transit headers and CRLF normalization are permitted. Evidence records both the submission-file digest and actual readback-byte digest. A matching subject/Message-ID alone is insufficient. Increase the bounded scan limits deliberately for older reconciliation; absence never authorizes resend.

### Read-only production website adapters

Once an approved content PR has been merged and deployed, replace website settings with these concrete commands:

```json
{
  "website_publish": ["python3", "-m", "runtime.site_verify", "resolve", "--settings", "/home/hermes/.hermes/data/twihn-site.json", "--context", "{context}"],
  "website_verify": ["python3", "-m", "runtime.site_verify", "verify", "--settings", "/home/hermes/.hermes/data/twihn-site.json", "--context", "{context}"]
}
```

Private `twihn-site.json`:

```json
{
  "repository": "OWNER/REPOSITORY",
  "production_branch": "main",
  "environment": "Production",
  "gh": "gh",
  "timeout": 30
}
```

`resolve` performs **read-only** GitHub API calls: resolve actual production-branch commit, read that commit's tracked public issue, compare all ordered IDs, metrics, URLs, timestamps and both summaries with the frozen legacy data. It does not push, create/merge PRs, or publish new content. Import/publication of new weekly content still needs the approved content PR workflow and explicit policy; there is no hidden auto-merge or unchecked main push.

`verify` requires GitHub Deployment API evidence for that exact commit and configured production environment, with its latest deployment status successful. It reads the same commit's tracked `dist/build-manifest.json`, issue HTML and public JSON; checks manifest hashes and frozen-content parity; then requests the three real HTTPS `twihn.com` routes and compares exact bytes and revalidation cache headers. The normal Python TLS verifier is retained; previews/other hosts and immutable mutable-route caching fail closed. It stores artifact URLs/cache policies/digests and deployment ID as evidence. This adapter is usable when Pages reports GitHub Deployments. If the integration exposes only check-runs or Cloudflare deployment APIs, provide a scoped read adapter meeting the following contract; **do not fake a GitHub deployment record**. Live verification has not been claimed for unconfigured infrastructure.

### Custom publication and readback contract

All commands are JSON **argv arrays**, never shell snippets. Scalar `{context}`, `{reviewed}`, `{issueId}`, `{start}`, `{end}`, `{email}`, `{messageId}`, `{messageSha256}`, `{sender}`, `{recipient}` placeholders are literal substitutions; there is no shell expansion. Prefer the single mode-0600 context JSON to long argv containing private values. Context also contains revision and reviewed SHA-256. Adapters are trusted executable code, not a sandbox: review them and keep their config restricted. They must not detach/create a new session or leave background children, or the supervised process-group boundary cannot contain them.

Except submission, stdout must contain **one JSON object**; use stderr for logs. Exit zero alone never confirms publication/delivery. Custom website publication must be idempotent by issue/revision/review digest, preserve historic content, use reviewed restricted-path PRs/required checks, and return `{"commit":"<actual production commit SHA>"}` only once the approved content is in production. A PR awaiting approval should return nonzero, not pretend its head SHA is the deployed SHA. The worker persists a returned commit before verification, allowing later read-only reconcile.

`website_verify` must actually read back the exact production commit/deployment, manifest, public JSON/ordered summaries, HTTPS page and cache policy. Its evidence object must have:

```json
{
  "verified": true, "production": true, "https": true,
  "manifestMatched": true, "htmlMatched": true, "jsonMatched": true, "cacheChecked": true,
  "issueId": "YYYY-MM-DD", "reviewedSha256": "<frozen legacy digest>",
  "commit": "<exact requested production SHA>", "deploymentId": "<actual identifier>",
  "url": "https://twihn.com/issues/YYYY-MM-DD/"
}
```

Attach detailed source/deployment/artifact observations as additional evidence. The runtime checks identifiers/required booleans; it cannot prevent a malicious adapter lying about readback. Use the supplied read-only adapter or reviewed code. Email preflight evidence requires `accountChecked`, `sentItemsChecked`, explicit matching sender/recipient. Email readback requires `verified`, exact `messageId`, `messageSha256` of the intent whose content was compared, matching recipient, and a nonempty `sentItemsId`; the supplied adapter also stores `readbackSha256` and `contentMatched`. Submission output is never trusted as evidence.

The **entire website stage** (custom checkout/install/build/commit/PR/deploy/readback included) runs in a supervised child process with one configurable deadline, initially 600s. Each adapter command also has a deadline. The parent kills the child's entire process group, including adapter grandchildren, on deadline and on normal stage exit to prevent late background publication. A failed or timed-out website never determines whether email runs.

## Resume, repair, reconcile

```sh
# Can submit the approved digest; use only for the existing authorized delivery.
python3 -m runtime --config "$HOME/.hermes/data/twihn-runtime.json" \
  resume --issue 2026-11-13

# Website only: NEVER renders, preflights, or sends email.
python3 -m runtime --config "$HOME/.hermes/data/twihn-runtime.json" \
  website-only --issue 2026-11-13

# Read back an ambiguous send; NEVER sends, even if Sent Items is empty.
python3 -m runtime --config "$HOME/.hermes/data/twihn-runtime.json" \
  reconcile --issue 2026-11-13 --channel email

# Verify previously recorded production commit without publishing again.
python3 -m runtime --config "$HOME/.hermes/data/twihn-runtime.json" \
  reconcile --issue 2026-11-13 --channel website
```

`resume` first builds/validates a complete baseline with the existing renderer. If that fails, neither channel proceeds. If website verification succeeds within the deadline, it renders/validates a linked variant using the canonical stable URL. Linked rendering failure falls back to the already validated baseline. Website failure/timeout also uses baseline.

Before submission it checks account/Sent Items, freezes sender/recipient, stores full `send.eml`, its SHA-256, a stable Message-ID and an **in_progress send intent durably**, then invokes send once. Successful readback transitions to **verified**. Any submission error, verification absence/mismatch or crash after intent is **ambiguous** and requires reconciliation. SMTP success followed by IMAP-save failure is not a retryable send failure. The stable Message-ID aids reconciliation; SMTP servers do not guarantee deduplication.

A verified or ambiguous normal rerun is a no-op; an interrupted in_progress intent becomes ambiguous. Website-only repair can still run independently. Preflight/baseline failures *before* send intent are retryable; send failures *after* intent are never automatically retryable. Changing frozen sender/recipient is rejected. CLI guards apply before invoking adapters, not merely as library conventions. There is deliberately **no force-resend/reset-ambiguous command**. If readback remains absent, investigate/restore Sent Items or obtain an explicit operator decision outside this automated delivery flow; do not edit state to pending.

Exit 0 indicates the requested operation completed without a blocking channel failure, not inbox delivery. `resume` can return 0 for verified email plus failed website (intentional email-primary behavior); inspect the independent states/report. Exit 2 indicates validation/lock/config failure or failed/ambiguous email; website-only or website reconciliation returns 2 unless website is verified. The private `report.json` records outcomes, reconciliation requirement and website-repair command. No extra alert email is sent.

## Backup, restore, corrections

Back up the entire private state directory **encrypted**, together with private adapter settings, before/after sends, preserving restricted permissions. Credentials require their existing separate secure recovery process. Restore while the scheduler is paused and no process owns the lock. Check frozen reviewed SHA-256 and original issue/window, restore `send.eml`/intent/evidence together, then reconcile uncertain outcomes before resuming. Re-run normal resume only if state explicitly says pending/pre-intent failed, or verified (no-op).

An orphaned review without state is refused: it might represent a crash or lost delivery evidence. If all private state is lost, Git/public JSON does **not** prove whether email was sent. Do not initialize old issues as fresh pending sends; restore backup or reconstruct/reconcile delivery evidence under operator control. Preserve an ambiguous blocked state if evidence remains uncertain. Exactly-once SMTP delivery is not promised.

Public correction publication is separate from sending: preserve old public revision, add reviewed correction metadata and regenerated committed artifacts via the site's PR workflow. This CLI currently freezes private revision 1 and intentionally refuses in-place corrections; it has no automatic correction resend path. Historic website-only repairs must preserve the site archive/latest ordering. Clean-checkout site rebuild instructions belong to the site runbook; never restore email permission from regenerated HTML alone.

## Verification without sending

```sh
python3 -m unittest discover -s tests/runtime -v
python3 -m compileall -q runtime tests/runtime
python3 -m runtime --help
python3 -m runtime bounds --issue 2026-04-03
```

Tests use temporary private state, local fake transport executables and mocked GitHub/HTTPS boundaries. They exercise the actual runtime CLI/subprocess worker, real filesystem lock/modes/atomic state, DST bounds, immutable review/tamper guards, independent channels, no-op reruns, website failure/whole-stage timeout and descendant killing, baseline/linked-render fallback, preflight failure, ambiguous send/reconciliation, exact Sent MIME parity and stale/wrong deployment/live-byte rejection. No test invokes the installed Himalaya transport, sends mail, pushes Git, calls production APIs or publishes fixtures.

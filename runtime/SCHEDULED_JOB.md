# Weekly research → website → email job

The existing Friday **08:00 Europe/London** agent job remains responsible for research. This is not a headline-only unattended summarizer: every unique selected story and HN discussion must actually be retrieved and read. The automation publishes only the resulting complete reviewed dataset. Synthetic fixtures are test-only, never production issues.

## Activation policy

Source/runtime/configuration changes require a human-reviewed source PR. The operator has separately authorized **automatic publication of validated weekly content only**. The publication adapter may create/merge a content PR only when its server-read-back changed paths are the one new `data/issues/YYYY-MM-DD.json` and generated `dist/` output, its exact head passes the configured trusted CI checks, and its base is still the validated production commit. It never auto-merges source PRs, force-pushes main, deletes archived issues or silently edits a published issue.

Before enabling automatic content merges, configure and verify a GitHub **branch ruleset** for `main`:

1. In the repository, open **Settings → Rules → Rulesets → New branch ruleset**.
2. Set enforcement to **Active** and target the default branch (`main`). Leave the bypass list empty.
3. Enable **Require status checks to pass**, select **`validate` from GitHub Actions**, and enable **Require branches to be up to date before merging**. Save the rule. No merge queue or automatic source merging is required.
4. Run the read-only publisher doctor with the private site settings. `automaticPublicationReady` must be true. The same write-capable App identity used for publishing must read an active, effective strict rule with the trusted required check and explicit `current_user_can_bypass: never`; missing/unknown policy fails closed.

The App cannot administer repository rules. Client-side head/base reads and the merge API's head-SHA guard do **not** atomically protect the base: main can move between the last read and merge. Therefore verified server-side strict/no-bypass enforcement is mandatory before any automatic publication mutation and is rechecked before merge. A failed policy preflight only disables website publication; the complete baseline email remains available. Never relax the guard or substitute direct updates to main.

Before enabling the new source path, run the read-only source gate:

```sh
cd /path/to/checkout
python3 -m runtime.weekly launch-check --settings /private/twihn-site.json
```

It fetches production main using a short-lived repository-scoped credential and compares all tracked source paths (excluding only weekly `data/` and `dist/`) with the tested local source. This handles a squash merge without assuming the PR-head SHA became main. Unmerged source, changed tracked source or untracked source blocks activation. This gate does not send email or modify remote content. Cron retains the old email-only instructions as a fallback until the source PR is merged; if private delivery state already exists for the occurrence, fallback must stop rather than open a second send path.

## Scheduled run

All commands below use explicit private paths. Private sender/recipient, MIME, state, collection/research work, provenance binding and credential configuration must be **outside Git and web-served directories**, not scratch. Restrict private directories to 0700 and JSON/MIME files to 0600. The private runtime JSON supplies approved addresses and actual adapter argv. No credential values belong in this document or generated issues.

### 1. Freeze the intended scheduler occurrence and collection

```sh
python3 -m runtime.weekly begin \
  --job-id '<existing-weekly-job-id>' --jobs /private/hermes/cron/jobs.json \
  --work-root /private/twihn/work
```

The job must already have the Friday cron expression and Europe/London timezone. Identity comes from its persisted **original `last_dispatch.scheduled_at`**, not the completion time or current retry clock. Manual recovery instead supplies `--issue YYYY-MM-DD` to the same command. A future/unfinished window is refused.

`begin` collects with exact UTC bounds through the existing two-hour-sharded Algolia collector and freezes `selected.json` plus `collection.json`. The latter records the collection time, complete population counts, and exact selected-file checksum. A retry reuses them without recollecting or changing observed metrics. Interrupted/orphaned collection files and changed selected bytes stop for recovery, not silent replacement.

### 2. Research every unique selected ID

Read source pages/original HN text and discussion threads/replies according to the digest skill. Preserve every selected field and order; add only single-paragraph `story` and `discussion` strings. Reuse identical summaries for independent-ranking overlaps. Never silently omit the unbounded 50+ point Show HN section.

Save the resulting legacy `reviewed.json` alongside the collection. Create a separate `sources.json` with **exactly** the selected unique IDs:

```json
{
  "itemSources": {
    "12345678": {
      "sources": [
        {"kind":"story", "url":"https://example.org/article"},
        {"kind":"discussion", "url":"https://news.ycombinator.com/item?id=12345678"}
      ],
      "availability": {
        "story":{"status":"reviewed", "note":"Original article retrieved and read."},
        "discussion":{"status":"reviewed", "note":"HN post, visible top-level comments and substantive replies read."}
      }
    }
  }
}
```

This is a **format example, not publishable content**. Use the statuses accepted by `schema/issue.schema.json`: never infer `reviewed` without retrieval. Record `unavailable` with a clear factual note when blocked, `text-post` only for an HN text post without an external source, and `no-comments` only when comment count is zero. Always include the canonical discussion reference; include each external source URL as a story reference even if inaccessible. Keep private notes, email addresses and credentials out of provenance. Validation establishes structure/completeness, not the factual truth of a summary; actual source review is mandatory.

### 3. Freeze approved review and publication metadata

```sh
python3 -m runtime.weekly approve --issue YYYY-MM-DD \
  --work-root /private/twihn/work --state-root /private/twihn/issues \
  --metadata-root /private/twihn/metadata \
  --reviewed /private/twihn/work/YYYY-MM-DD/reviewed.json \
  --sources /private/twihn/work/YYYY-MM-DD/sources.json --approve-reviewed
```

This rejects changed selection, ordering, metrics, URLs, tags or bounds, validates complete review and freezes it through the existing runtime. It creates publication metadata with authentic population counts, collection timestamp, explicit provenance and checksum bindings. Private checksum bindings do not become public issue fields. Existing frozen metadata/review is never overwritten on retry. If website-only provenance preparation fails, the approved review remains available for the baseline email; report the returned `websitePreparationError`, do not fabricate provenance or reset delivery state.

### 4. Publish and deliver with independent outcomes

```sh
python3 -m runtime --state-dir /private/twihn/issues \
  --config /private/twihn-runtime.json resume --issue YYYY-MM-DD
```

The runtime first validates the baseline email. In one bounded website stage it imports reviewed public content into a clean production checkout, validates/tests/builds committed artifacts, opens and validates a restricted content PR, automatically merges that content PR under the approved policy, and waits for the exact production SHA's Cloudflare Pages check plus exact live HTTPS manifest/issue HTML/JSON readback. A preview check alone never proves production. Only verified production gets the stable issue link in the email.

Website timeout/failure does **not** suppress a valid baseline email. Read `report.json` and the independent channel states. A nonzero SMTP/send or missing/mismatched Sent copy is **ambiguous**, never permission to resend. A stable Message-ID, frozen byte checksum and durable send intent precede submission. Existing legacy same-window Sent subjects also block fresh sends, including messages without Message-ID. Successful delivery requires exact MIME readback from Sent Items; it is not a guarantee of recipient inbox placement.

## Recovery

- Retry the same issue and frozen review; never recollect a drifting `--days 7` window.
- To repair website only (never send email):
  ```sh
  python3 -m runtime --state-dir /private/twihn/issues --config /private/twihn-runtime.json website-only --issue YYYY-MM-DD
  ```
- To reconcile uncertain email (read-only; never send):
  ```sh
  python3 -m runtime --state-dir /private/twihn/issues --config /private/twihn-runtime.json reconcile --issue YYYY-MM-DD --channel email
  ```
- Stop on missing/orphaned private state. Public Git content cannot reconstruct permission to resend email. Preserve ambiguous state and investigate instead of deleting it.
- Preserve all archived issues and retained corrections. Corrections require the separately reviewed revision workflow, not a reset of the original send state.
- No test email or live synthetic publication is required or authorized by installation. Run offline tests and read-only production preflights. A first real weekly publication is verified only when its own production artifacts and Sent copy have been read back.

# TWIHN — This Week in Hacker News

Static Astro/TypeScript website for **twihn.com**, with a permanently retained public reviewed-JSON archive and committed generated HTML. Email and website consume the same reviewed content. The checked-in bootstrap contains no fictional issue content.

## Verify locally

Use the exact Node version in `.node-version` and npm version in `package.json`.

```sh
# Public archive fixtures require 0644 files; protect private logs separately.
umask 022
npm ci
npm run check
npm test
python3 -m unittest discover -s tests/runtime -v
python3 -m unittest discover -s tests -p 'test_*.py' -v
npm run build
npm run verify:dist
```

When system Node is older, install the pinned Node executable in an isolated toolchain and put its `bin` directory first on `PATH`, without replacing system Node. Verify both versions before running the commands:

```sh
node --version
npm --version
```

`dist/` is deliberately committed. CI rebuilds it and rejects differences/untracked output. `TWIHN_SOURCE_REVISION` records a template/source revision when a release is built; the deterministic bootstrap defaults to `unversioned`. Do not use the artifact-containing commit's own SHA inside its bytes. Resolve the deployed commit through external GitHub/Pages evidence.

## Content preparation

1. Derive the exact scheduled window with `python3 -m runtime bounds --issue YYYY-MM-DD` (Friday 08:00 Europe/London).
2. Collect with `integrations/weekly_hn_digest.py --start <UTC> --end <UTC> --limit 10 --format selected-json`.
3. Retrieve/read every unique story and discussion and add reviewed paragraphs. Preserve the collector's exact section membership/order: current email selection gives votes precedence and chooses the discussed list from remaining ordinary stories. Public archives can retain older independent rankings; repeated IDs there must have identical metrics/summaries. Do not invent inaccessible-source content.
4. Supply the public metadata required by `scripts/content.mjs`: issue/revision, scheduled time, actual collection time, complete qualifying counts and per-item source references/availability. No delivery addresses or credentials.
5. Prepare and validate website-only topic assignments using the full production registry and existing story records, then freeze them with `runtime.weekly approve --tagging`. Follow [runtime/STORY_TAGS.md](runtime/STORY_TAGS.md) for the exact CLI and failure/retry rules. The model supplies semantic topic judgement; the validator only enforces mechanical/structural consistency. Do not add topic tags to reviewed issue or legacy email JSON.
6. For manual publication, `npm run import:legacy -- /private/reviewed.json /private/metadata.json data/issues/YYYY-MM-DD.json` validates and refuses overwrite; apply the matching frozen sidecar with the documented `tagging.mjs apply` command before checking/building. Run the full suite, regenerate and commit `dist/`, and propose a content PR. Scheduled publication uses the [weekly runbook](runtime/SCHEDULED_JOB.md); its adapter authorizes only the exact new issue, justified registry/story changes and generated output, with strict server-side merge protection. Source changes and standalone backfills remain human-merged; unchecked production pushes are never used.

Routes: `/`, `/issues/YYYY-MM-DD/`, `/tags/`, `/tags/<slug>/`, `/tags/<slug>/<page>/` (pages 2+), `/data/index.json`, `/data/latest.json`, `/data/issues/YYYY-MM-DD.json`, `/data/revisions/YYYY-MM-DD/vN.json`, `/data/tags.json`, `/data/stories/<HN-ID>.json`, `/schema/issue.schema.json`, `/schema/tags.schema.json`, `/schema/story.schema.json`, `/build-manifest.json`. Index/latest are derived from canonical issues. Tag archives contain 20 unique stories per page, newest submission first, with numeric HN-ID descending as the tie-breaker. Stable slugs and existing assignments are reused; a complete rebuild covers all current issues, not just the latest week. Retained revisions are not extra archive stories. All issues/revisions are retained; approved corrections must preserve earlier revisions and label changes. Presentation tags do not rewrite reviewed facts or historical issue/revision source bytes.

The one-time AI backfill is included with the feature data; do not rerun it or retag known stories as part of weekly publication. See [the tagging operator runbook](runtime/STORY_TAGS.md) for complete coverage, byte preservation, deterministic two-build verification, activation gates and rollback. A local build is not proof of production deployment or scheduler activation.

## Email/recovery integration

See [runtime/README.md](runtime/README.md) for the tested CLI, exact private-config adapter contracts, baseline fallback, locked durable state and ambiguous-send reconciliation. Private sender/recipient configuration is required; none is committed. Tests use mocked submission only.

`integrations/render_email.py` supports `--archive-url`, `--message-id`, and private `TWIHN_SENDER`/`TWIHN_RECIPIENT` settings. The existing Friday 08:00 Europe/London agent job can freeze collection and review through `runtime.weekly`, publish through `runtime.publisher`, verify exact Cloudflare production evidence through `runtime.site_verify`, and submit through the durable email runtime. Website failure preserves the validated baseline email; uncertain email submission never automatically resends.

Activation has two independent gates: locally tested source must match human-merged production main, and automatic content publication requires a verified active main-branch ruleset with strict trusted status checks and no publishing-App bypass. The staged scheduler retains the original email-only fallback until source activation, provided no current-issue private work/state exists. Missing server policy blocks website mutations but does not suppress a complete baseline email. The App cannot administer repository rules; the operator configures that prerequisite in GitHub. See [runtime/SCHEDULED_JOB.md](runtime/SCHEDULED_JOB.md) for exact setup, original-scheduler identity, source provenance, private configuration, and repair commands.

Known limitation: runtime/legacy email requires full ten-entry score and Ask HN sections, with up to ten remaining ordinary stories in the discussed section, even though the public site supports proved smaller populations. Tests never publish fixtures or submit real email. Read-only infrastructure preflights and passing tests are not evidence that the first real weekly issue/email has been delivered; that run needs its own production and Sent MIME readbacks.

## Cloudflare Pages operator setup

Infrastructure is managed by `stebennett/home-tf`, in its `pages/` Terraform root.
Follow that repository's `docs/twihn-pages.md`; do not create a second Pages project
or make dashboard configuration changes that conflict with Terraform.

- Project: `twihn`; Git source: this repository; production branch: `main`.
- Serve committed artifacts: build `exit 0`, output `dist`, and
  `SKIP_DEPENDENCY_INSTALL=1` for both production and preview.
- After project creation, a new Git commit/PR exercises the connection. Check
  Cloudflare deployment results separately from the GitHub `validate` check.
  Project creation and passing site tests do not establish a deployed website.
- First verify the production `pages.dev` landing page, `/data/index.json`,
  `/data/latest.json` and `/build-manifest.json`. Empty index and null latest
  are expected until a real reviewed issue is published.
- Keep `TWIHN_CUSTOM_DOMAIN_ENABLED=false` until the initial production site
  works. Then use home-tf's reviewed plan/apply flow for the domain/CNAME;
  preserve existing DNS and verify TLS before enabling archive links in email.
- Production account GitHub authorization is a separate prerequisite. If a new
  commit does not create a deployment, inspect the Git connection/settings;
  do not treat an unrelated CI success as evidence that Pages received it.
- No weekly content automation or new email runtime is activated by deploying
  this bootstrap. No additional test email without explicit permission.

## Restore

Clone the repository, install the pinned toolchain/dependencies, run the full checks/build and compare committed `dist/`; all public content is rebuildable from retained JSON. Preserve required assets and public revisions. Back up private runtime state/settings separately with encryption, never in this repository. Restore with the scheduler paused and reconcile uncertain email intent before resuming. Public Git history alone cannot prove an email was sent.

# TWIHN — This Week in Hacker News

Static Astro/TypeScript website for **twihn.com**, with a permanently retained public reviewed-JSON archive. Git retains source and reviewed data; Cloudflare Pages builds and verifies generated HTML/JSON at deployment. Email and website consume the same reviewed content. Synthetic fixtures are test-only.

## Verify locally

Use the exact Node version in `.node-version` and npm version in `package.json`.

```sh
# Public archive fixtures require 0644 files; protect private logs separately.
umask 022
# Initial installation/approved restore, not automatic read-only recovery.
npm ci --include=dev
npm run check
npm test
python3 -m unittest discover -s tests/runtime -v
python3 -m unittest discover -s tests -p 'test_*.py' -v
npm run build
npm run verify:dist
npm run verify:reproducible
git diff --check
# Generated output must never be tracked.
test -z "$(git ls-files -- dist)"
```

When system Node is older, install the pinned Node executable in an isolated toolchain and put its `bin` directory first on `PATH`, without replacing system Node. Verify both versions before running the commands:

```sh
node --version
npm --version
```

`dist/` is ignored, generated output, retained locally for inspection but never staged or committed. `verify:dist` independently verifies the complete local release. `verify:reproducible` runs two complete builds in isolated workspaces, with separate output/caches, and compares the complete file inventory and bytes, **including `build-manifest.json`**. CI retains the full check/test suites, rejects any tracked `dist/`, proves repeat-build equality and requires unchanged source worktree/index with no unexpected untracked source.

`TWIHN_SOURCE_REVISION` can record a build/template revision, but this migration keeps the deterministic, CI/publisher/readback-compatible default **`unversioned`**. With output no longer tracked, embedding a commit SHA is not inherently circular; changing provenance is nevertheless a separate compatibility change. Do not set an independent Cloudflare commit-SHA override without matching trusted reconstruction. Resolve the exact deployed commit through external trusted GitHub/Cloudflare production evidence, not the manifest's revision string.

## Content preparation

1. Derive the exact scheduled window with `python3 -m runtime bounds --issue YYYY-MM-DD` (Friday 08:00 Europe/London).
2. Collect with `integrations/weekly_hn_digest.py --start <UTC> --end <UTC> --limit 10 --format selected-json`.
3. Retrieve/read every unique story and discussion and add reviewed paragraphs. Preserve the collector's exact section membership/order: current email selection gives votes precedence and chooses the discussed list from remaining ordinary stories. Public archives can retain older independent rankings; repeated IDs there must have identical metrics/summaries. Do not invent inaccessible-source content.
4. Supply the public metadata required by `scripts/content.mjs`: issue/revision, scheduled time, actual collection time, complete qualifying counts and per-item source references/availability. No delivery addresses or credentials.
5. Prepare and validate website-only topic assignments using the full production registry and existing story records, then freeze them with `runtime.weekly approve --tagging`. Follow [runtime/STORY_TAGS.md](runtime/STORY_TAGS.md) for the exact CLI and failure/retry rules. The model supplies semantic topic judgement; the validator only enforces mechanical/structural consistency. Do not add topic tags to reviewed issue or legacy email JSON.
6. For manual publication, `npm run import:legacy -- /private/reviewed.json /private/metadata.json data/issues/YYYY-MM-DD.json` validates and refuses overwrite; apply the matching frozen sidecar with the documented `tagging.mjs apply` command before checking/building. Run the full suite, build/verify and prove repeat-build equality; commit **only** the canonical new issue and justified registry/story source JSON in the content PR. Keep generated output for local inspection. Scheduled publication uses the [weekly runbook](runtime/SCHEDULED_JOB.md); its adapter rejects every `dist/` path in staged and server-read-back content changes, with strict server-side merge protection. Source changes, corrections and standalone backfills remain human-merged; unchecked production pushes are never used.

Routes: `/`, `/issues/YYYY-MM-DD/`, `/tags/`, `/tags/<slug>/`, `/tags/<slug>/<page>/` (pages 2+), `/data/index.json`, `/data/latest.json`, `/data/issues/YYYY-MM-DD.json`, `/data/revisions/YYYY-MM-DD/vN.json`, `/data/tags.json`, `/data/stories/<HN-ID>.json`, `/schema/issue.schema.json`, `/schema/tags.schema.json`, `/schema/story.schema.json`, `/build-manifest.json`. Index/latest are derived from canonical issues. Tag archives contain 20 unique stories per page, newest submission first, with numeric HN-ID descending as the tie-breaker. Stable slugs and existing assignments are reused; a complete rebuild covers all current issues, not just the latest week. Retained revisions are not extra archive stories. All issues/revisions are retained; approved corrections must preserve earlier revisions and label changes. Presentation tags do not rewrite reviewed facts or historical issue/revision source bytes.

The one-time AI backfill is included with the feature data; do not rerun it or retag known stories as part of weekly publication. See [the tagging operator runbook](runtime/STORY_TAGS.md) for complete coverage, byte preservation, deterministic two-build verification, activation gates and rollback. A local build is not proof of production deployment or scheduler activation.

## Email/recovery integration

See [runtime/README.md](runtime/README.md) for the tested CLI, exact private-config adapter contracts, baseline fallback, locked durable state and ambiguous-send reconciliation. Private sender/recipient configuration is required; none is committed. Tests use mocked submission only.

`integrations/render_email.py` supports `--archive-url`, `--message-id`, and private `TWIHN_SENDER`/`TWIHN_RECIPIENT` settings. The existing Friday 08:00 Europe/London agent job can freeze collection and review through `runtime.weekly`, publish through `runtime.publisher`, verify exact Cloudflare production evidence through `runtime.site_verify`, and submit through the durable email runtime. Website failure preserves the validated baseline email; uncertain email submission never automatically resends.

Readback rebuilds expectations from exact deployed-commit **source data** with the installed human-reviewed renderer, schemas and already-installed dependencies. It first binds the complete build-input inventory/bytes to that commit, extracts data only, and uses a disposable workspace isolated from installed weekly data/output/caches and credentials. It never executes fetched code/schema/header policy, uses legacy Git `dist/` as authority or installs packages during recovery. Missing dependencies or incompatible installed source fail website cleanly: pause the scheduler and install the approved compatible release/dependencies under operator control before retrying. No external database is required.

Activation has two independent gates: locally tested source must match human-merged production main, and automatic content publication requires a verified active main-branch ruleset with strict trusted status checks and no publishing-App bypass. The staged scheduler retains the original email-only fallback until source activation, provided no current-issue private work/state exists. Missing server policy blocks website mutations but does not suppress a complete baseline email. The App cannot administer repository rules; the operator configures that prerequisite in GitHub. See [runtime/SCHEDULED_JOB.md](runtime/SCHEDULED_JOB.md) for exact setup, original-scheduler identity, source provenance, private configuration, and repair commands.

Known limitation: runtime/legacy email requires full ten-entry score and Ask HN sections, with up to ten remaining ordinary stories in the discussed section, even though the public site supports proved smaller populations. Tests never publish fixtures or submit real email. Read-only infrastructure preflights and passing tests are not evidence that a real weekly issue/email has been delivered; each run needs its own production and Sent MIME readbacks.

## Cloudflare Pages operator setup and migration order

Infrastructure is managed by `stebennett/home-tf`, in its `pages/` Terraform root.
Follow that repository's `docs/twihn-pages.md`; do not create a second Pages project,
add site deployment credentials or make dashboard overrides that conflict with Terraform.

- Retain the existing `twihn` project, Git integration, this source repository and production branch `main`.
- Terraform-managed build command for **production and preview**:
  `npm install --global npm@12.0.2 && npm ci --include=dev && npm run build && npm run verify:dist`.
  Output remains `dist`; both environments set `NODE_VERSION=22.23.5` and
  `SKIP_DEPENDENCY_INSTALL=1`. The latter skips Cloudflare's automatic installer
  only: the command explicitly performs the locked install, including development
  dependencies even under `NODE_ENV=production`.
- Production account GitHub authorization remains a separate prerequisite. If a
  commit does not create a deployment, inspect the Git connection/settings;
  unrelated Actions success is not evidence that Pages received it.
- This migration changes build settings only, not DNS, domain resources, project
  identity or credential scopes. Domain/TLS changes need their own reviewed plan;
  never duplicate Terraform-owned DNS records or infer activation from a refreshed
  domain-status output.

**Ordered human gates:**

1. Review the companion infrastructure PR (`stebennett/home-tf` #29) and its exact
   live plan before human merge/apply. The November 23, 2026 reviewed plan for
   head `356bf9c` changes the Pages project in place (0 add, 1 change, 0 destroy),
   with no DNS/Zero Trust resource changes; computed attributes/output refreshes
   are not extra resource actions. No apply is established by that plan; recheck
   the plan if the head or live state changes. Human merge can trigger automatic
   apply in **all three Terraform roots** through the main workflow; inspect
   their jobs separately.
   The new command is backwards-compatible with the old production source, so
   infrastructure goes first. No agent merge/apply is authorized here.
2. After apply, read back the exact production/preview build command, output and
   pinned environment settings. Retry/trigger the new source PR's **exact-head**
   Cloudflare preview using those settings; verify real generated artifacts.
   An old `exit 0` preview failure is a blocked gate, not a passing build.
3. Keep tagging PR #17 unchanged during this work. The new source-only deployment
   PR on `ci/build-on-deploy` initially stacks on `feat/story-tags`. After the human
   merges tagging, retarget/rebase onto actual main as needed; rerun exact-head CI
   and preview whenever the head changes. Only the human merges the source migration.
4. Resolve the actual resulting production main commit and verify trusted
   Cloudflare **production** evidence plus exact manifest/JSON/HTML readback.
   Use the already-reviewed, tested verification checkout and its preinstalled
   dependencies for this readback; its build-input bytes must match deployed main.
   Do not use an incompatible old runtime or install packages during readback.
   Only then provision that approved release in the active runtime checkout while
   paused, pass doctor and launch-check, and follow the activation checklist.
   No live job/skill/config change or test email is authorized by this migration.

## Restore

Clone the approved source release, install its pinned toolchain/dependencies while the scheduler is paused, and run the full checks/build/semantic verifier plus two-isolated-build equality above. All public content is rebuildable from retained JSON, required assets and revisions; back those up with their source history. There is no committed `dist/` to restore or external database to recover. Keep generated output locally for inspection and verify production independently. Back up private runtime state/settings separately with encryption, never in this repository. Reconcile uncertain email intent before resuming; public Git history or rebuilt HTML alone cannot prove an email was sent or authorize a resend. Operator installation during setup/restore is distinct from automatic read-only recovery, which never installs packages.

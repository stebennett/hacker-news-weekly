# TWIHN — This Week in Hacker News

Static Astro/TypeScript website for **twihn.com**, with a permanently retained public reviewed-JSON archive and committed generated HTML. Email and website consume the same reviewed content. The checked-in bootstrap contains no fictional issue content.

## Verify locally

Use the exact Node version in `.node-version` and npm version in `package.json`.

```sh
npm ci
npm run check
npm test
python3 -m unittest discover -s tests/runtime -v
python3 -m unittest discover -s tests -p 'test_*.py' -v
npm run build
npm run verify:dist
```

When system Node is older, without changing system installation:

```sh
npm exec --yes --package=node@22.23.4 -- sh -c 'npm run check && npm test && npm run build && npm run verify:dist'
```

`dist/` is deliberately committed. CI rebuilds it and rejects differences/untracked output. `TWIHN_SOURCE_REVISION` records a template/source revision when a release is built; the deterministic bootstrap defaults to `unversioned`. Do not use the artifact-containing commit's own SHA inside its bytes. Resolve the deployed commit through external GitHub/Pages evidence.

## Content preparation

1. Derive the exact scheduled window with `python3 -m runtime bounds --issue YYYY-MM-DD` (Friday 08:00 Europe/London).
2. Collect with `integrations/weekly_hn_digest.py --start <UTC> --end <UTC> --limit 10 --format selected-json`.
3. Retrieve/read every unique story and discussion and add reviewed paragraphs. Repeated IDs across the two ordinary rankings must have identical metrics/summaries. Do not invent inaccessible-source content.
4. Supply the public metadata required by `scripts/content.mjs`: issue/revision, scheduled time, actual collection time, complete qualifying counts and per-item source references/availability. No delivery addresses or credentials.
5. `npm run import:legacy -- /private/reviewed.json /private/metadata.json data/issues/YYYY-MM-DD.json` validates and refuses overwrite.
6. Run the full verification suite, regenerate and commit `dist/`, and propose a content PR. No automatic merging or unchecked production push is installed.

Routes: `/`, `/issues/YYYY-MM-DD/`, `/data/index.json`, `/data/latest.json`, `/data/issues/YYYY-MM-DD.json`, `/data/revisions/YYYY-MM-DD/vN.json`, `/schema/issue.schema.json`, `/build-manifest.json`. Index/latest are derived from canonical issues. All issues/revisions are retained; approved corrections must preserve earlier revisions and label changes. Templates may evolve without changing reviewed facts.

## Email/recovery integration

See [runtime/README.md](runtime/README.md) for the tested CLI, exact private-config adapter contracts, baseline fallback, locked durable state and ambiguous-send reconciliation. Private sender/recipient configuration is required; none is committed. Tests use mocked submission only.

`integrations/render_email.py` supports `--archive-url`, `--message-id`, and private `TWIHN_SENDER`/`TWIHN_RECIPIENT` settings. The new runtime is **not activated in the scheduler yet**. The existing email job remains in place, with its old top-five prompt corrected to top ten. Do not swap that working path until publication/configuration and end-to-end preview checks are ready.

Known current limitations: runtime/legacy email requires full ten-entry capped sections even though the public site supports proved smaller populations. Website adapters perform read-only verification of already-approved publication, not automatic content-PR creation/merge. No content auto-merge policy has been authorized. GitHub Deployment reporting must match the concrete verifier; check-only Pages reporting needs a compatible read adapter. These are activation gates, not claims of completed production automation.

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

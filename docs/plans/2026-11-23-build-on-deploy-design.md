# Build-on-deployment design

## Approved direction

Steve requested the move from committed generated output to build-and-deploy on 2026-11-23, explicitly on a different branch with a new PR. The original tagging PR remains untouched and human-merged. This change removes generated `dist/` from version control, not the deployment output directory.

## Deployment architecture

Retain the existing GitHub-connected Cloudflare Pages project, production branch, preview check-run provenance and Terraform ownership. Do not introduce a second project, a dashboard override or deployment credentials in the site repository. Pages explicitly installs the repository-pinned npm, uses `npm ci`, builds Astro and runs the artifact verifier before publishing its resulting `dist/`. Both production and preview use the pinned Node version. Skipping *automatic* dependency installation remains appropriate because the configured build command performs the lockfile-based install itself.

A companion `home-tf` PR changes only the Pages build configuration and its regression tests/documentation. Its build command must also work against the current site main, which already has the same npm/build/verifier commands. The infrastructure PR can therefore be merged/applied first without requiring the new source branch. No agent merge, apply, DNS/domain change or scheduler activation is authorized by this implementation.

## Source and CI

Keep reviewed issues, revisions, the tag registry and independent story records in Git. Ignore `dist/`; remove all tracked generated files. Preserve generated public URLs, schemas, exact JSON exports and 20-story tag archives. CI continues full checks/tests and semantic artifact verification, then proves two complete builds have exactly the same file inventory and bytes, including the manifest. CI rejects tracked build output and unexpected tracked source changes rather than comparing output with a committed release. Preserve deterministic `unversioned` default provenance during this migration; exact deployed commit provenance continues to come from trusted external integration evidence.

## Guarded publication

The automatic publisher builds and validates locally but stages/authorizes only the new canonical issue and selected tag registry/story data. It must reject any `dist/` path in a content PR, not merely stop requiring one. Preserve exact-base record authorization, immutable historical facts, source/index equality, strict trusted main checks with no App bypass, exact-head and server-file-list verification, SHA-guarded merge and before/after source blob readback. Local public copies must still match validated source bytes; they are not remote Git blobs anymore. Existing-issue idempotent recovery must not depend on a checked-in build directory.

## Production readback

Keep exact deployed-commit resolution, trusted production Cloudflare check evidence, strict JSON, no redirects, fixed deadlines and the global request budget. Acquire only the immutable commit's public source data from the bounded, strictly validated GitHub archive. Reconstruct expected complete output with the installed human-reviewed build/template/schema code and already-installed pinned dependencies in a disposable workspace. Never execute fetched scripts, use fetched schemas as validator authority, install dependencies during recovery, or fetch mutable branch content. Do not trust a deployment's manifest to define its own expected inventory. Generate and independently validate the full local release, then compare the manifest and the existing selected issue/registry/story/tag directory/pagination boundary paths against production, retaining the explicit bounded-not-full-crawl claim.

The new local build workspace must be independent of installed `dist/`, installed current weekly data and attacker-supplied archive code. Extract only regular public data files, set public fixture permissions explicitly under a private parent, and retain all archive inventory/size/timeout guards. Subprocess environments must exclude GitHub and email credentials.

## Branch and rollout boundaries

The tagging PR is still open at discovery, so `ci/build-on-deploy` is based on its exact verified head and the new site PR is stacked on `feat/story-tags`. This avoids duplicating or rewriting the original PR. The companion infrastructure branch starts at current `home-tf` main.

Operator order: review/apply the backwards-compatible infrastructure PR; verify its exact live plan/apply and build settings; trigger/retry the new site PR preview and verify its real artifacts; merge the tagging PR; retarget/rebase the deployment PR to actual main as needed and rerun exact-head checks; human-merge the deployment PR; verify exact production deployment/artifacts; only then update installed runtime/dependencies and pass doctor/launch-check before scheduler activation. Source CI success does not prove infrastructure has been applied. A Cloudflare preview blocked by old `exit 0` configuration must be reported as an operational gate, never as a passing preview.

## Acceptance criteria

- Separate source branch/new PR; original tagging branch/head unchanged.
- No tracked `dist/`; source/data remain preserved.
- Real clean source-only build, semantic verification, repeat-build equality and full pinned regression suites pass.
- New content commits contain source JSON only, and existing safety/fallback/no-resend tests still exercise real boundaries.
- Exact-commit production expectations work from a source-only archive with trusted installed code and no package install/network subprocesses.
- Terraform fmt/validate and mocked tests pass with both inherited domain activation flag values; live plan is inspected before merge guidance if available.
- Independent specification and quality/security/privacy reviews pass.
- New PR(s) are read back with exact heads; observed CI and blocked external gates are reported separately. No merges, production apply, real email or live scheduler changes.

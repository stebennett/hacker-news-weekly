# Build-on-deployment implementation plan

> **For implementer:** Use TDD throughout. Write failing behavior tests first, run them and verify intended failures, then implement. Preserve unrelated worktrees and the original tagging PR.

**Goal:** Stop committing `dist/`, generate and validate it during Cloudflare deployment, and preserve guarded publication/readback guarantees in separate new PRs.

**Architecture:** Follow `2026-11-23-build-on-deploy-design.md`. Keep Git-connected Pages and Terraform ownership. Content PRs carry source data only; exact-commit production expectations are rebuilt using installed trusted code/dependencies, not downloaded scripts. No agent merges/apply/sends/scheduler changes.

**Tech Stack:** Astro/TypeScript/Node 22.23.5/npm 12.0.2; Python 3.13.5 stdlib runtime tests; Terraform 1.15.1 and locked Cloudflare provider.

## Baselines and isolated branches

- Site: `/home/hermes/projects/twihn-build-on-deploy`, `ci/build-on-deploy`, forked from tagging head `59e1a4dea8ec9d459862fb2ddf9cb7e24db13210`. Site baseline is running before code changes; evidence is in the private development-evidence directory, not this repo.
- Infra: `/home/hermes/projects/home-tf-build-on-deploy`, `ci/twihn-build-on-deploy`, forked from main `7114ed606f3db3506216977af27e8946823785b5`. Backend-disabled init/validate and both mocked scenarios passed with both inherited activation values before changes.
- Use `/home/hermes/.hermes/toolchains/twihn-node22.23.5-npm12.0.2/bin` first on PATH. Verify actual `node --version`, `npm --version`, `python3 --version`.
- Source checks/tests/build: `npm ci && npm run check && npm test && python3 -m unittest discover -s tests/runtime -v && python3 -m unittest discover -s tests -p 'test_*.py' -v && npm run build && npm run verify:dist`.
- Public fixtures need 0644; use `umask 022`, keeping evidence in a private 0700 parent. Do not run live publication or use real delivery configuration.

## Task 1: Generated-only output and build reproducibility

**Files:** `.gitignore`, `package.json`, `.github/workflows/validate.yml`, new `scripts/verify-reproducible.mjs` (or equivalently named focused helper), new `tests/site-reproducible.test.mjs`, deletion of tracked `dist/**`.

1. Add unit tests for snapshot/compare behavior: identical trees pass; changed bytes, extra/missing files and symlinks/nonregular entries fail. A valid control must call the real exported API before negative assertions. Add a CLI/integration regression proving a genuine build can be verified without any tracked `dist/`.
2. Run `node --test tests/site-reproducible.test.mjs`; record initial availability RED separately from meaningful behavioral RED. Do not use an undefined export as evidence for rejection behavior.
3. Implement a minimal complete sorted file+digest snapshot and exact equality check. Reuse existing inventory/digest utilities where appropriate. CLI must compare two complete real builds, including `build-manifest.json`; propagate child failures and bounded process errors. Keep default output/provenance deterministic. Avoid rerunning unrelated entire suites in the reproducibility helper.
4. Replace the committed-dist comment with `dist/` in `.gitignore`. Remove output from the index with `git rm -r --cached -- dist`; generated local files can remain ignored. Add `verify:reproducible` npm command. CI must build/verify, verify repeat-build equality, reject any tracked `dist/`, and detect unexpected source changes. Keep existing check/test steps and pinned toolchains.
5. Run targeted tests, `npm test`, real build/verify/repeat-build, `git ls-files dist` (empty), `git diff --check`. Commit scoped changes with evidence.

## Task 2: Source-only automatic publication

**Files:** `runtime/publisher.py`, `scripts/verify-tag-changes.mjs`, `tests/runtime/test_publisher.py`, `tests/site-tag-changes.test.mjs`, relevant source-only fixture updates in `tests/runtime/test_tagged_publication.py`. Both the Python publisher and the independent Node semantic diff authorizer currently require/allow dist; change both to source-only authorization.

1. Add failing behavior tests for `allowed_paths`: canonical issue plus authorized registry/story paths succeeds without any `dist/`; any generated path is rejected. Keep wrong/missing canonical issue, unknown stories, arbitrary source/config paths and retained-history mutation rejection tests.
2. Run `python3 -m unittest discover -s tests/runtime -p test_publisher.py -v`; capture intended assertions before implementation.
3. Keep local check/tests/build/verify before publication. Restrict staged/server changed paths to the canonical new issue plus authorized tag source records. Stop requiring/allowing generated Git paths. Preserve final record authorization and index/worktree parity after lifecycle/build scripts.
4. Split local public/source byte equality from remote Git source blob readback. Snapshot all selected registry/story bytes, including reused unchanged records, but do not query `contents/dist/...` anymore. Verify canonical issue source and selected source snapshots before/after merge. Move existing-issue idempotent recovery ahead of `npm ci`: no npm invocation/install and no checked-out/downloaded script execution on that path. Use installed trusted importer/diff-validator modules with explicit downloaded repository/data arguments and installed schema authority; verify full source/archive parity before independent production readback. Missing installed prerequisites fail the website stage cleanly rather than installing packages. Do not weaken frozen retry, strict policy, exact-base/head/server path list or no-source-PR-auto-merge guards.
5. Update real CLI fixture repositories to have no tracked dist and to reject production reads of generated Git paths. Prove a real content commit has source JSON paths only, then preserve all fallback/recovery/no-resend cases. Run focused tests and commit once GREEN.

## Task 3: Trusted exact-commit source-only production readback

**Files:** `runtime/site_verify.py`, `runtime/trusted_build.py`, `tests/runtime/test_site_verify.py`, `tests/runtime/test_trusted_build.py`; narrowly scoped process-ownership changes in `runtime/orchestrator.py` and focused runtime tests if needed. E2E fixture integration remains coordinated with Task 2.

1. Add a source-only exact-commit archive fixture with no `dist/`, real public data and malicious fetched build/template/schema markers. The real expectation path must generate/verify output using installed trusted code/dependencies, ignore fetched code and never run npm install. Add a test for missing installed dependencies and preserve clear fail-closed diagnostics.
2. Run focused tests to intended RED. Preserve and rerun existing duplicate-key, inventory traversal/symlink/link, compressed/extracted byte limits, global request/deadline and selected-pagination readback regressions. Enforce expansion/member/deadline limits before hidden PAX/GNU extension metadata is consumed, rather than counting only yielded regular files. Include a legitimate codeload/PAX success case and a small-limit hidden-metadata regression; do not increase the existing budgets.
3. Extract only exact-commit public data into a private temporary workspace, explicitly set public-file permissions, and retain strict whole-archive validation/limits. Before reconstruction, bind the exact commit's complete build-input inventories and bytes to the installed trusted inputs: src/, scripts/, schema/, public/, astro.config.mjs, tsconfig.json, package.json, package-lock.json, .node-version, .npmrc and any other actual build-affecting configuration. Reject missing/extra/changed inputs and fetched header/schema/config changes; never execute downloaded scripts. Stage the minimal installed trusted build inputs and already-installed dependencies into that disposable workspace; isolate Astro/Vite caches as well as output. Ensure installed current data/dist cannot affect expected output.
4. Invoke the pinned Node build with a stripped environment and the existing remaining deadline, then run independent complete semantic verification and derive selected expected artifact bytes. Preserve exact full manifest comparison, all selected registry/story/directory/page boundary comparisons and bounded-not-full-crawl evidence. No extra recovery network/package installs or delivery-state writes. Preserve request accounting for the one archive acquisition.
5. Exercise the real build (not just mocked expected output) from a source-only archive; test malicious/missing data and trusted-input/dependency refusal, including noncanonical validated source bytes. Run focused Python tests, full runtime suite and commit once GREEN.

**Discovered supervision regression gate:** An inner Node build must not escape the enclosing website stage's process group. Test the actual `website_worker` path with an outer timeout shorter than the inner build deadline, and prove no child can write after the stage exits. Preserve standalone inner-deadline cleanup and exited-leader descendant cleanup too. Process ownership must be established by trusted runtime policy, without an operator configuration change, and must not alter delivery/no-resend semantics. A passing standalone helper timeout test alone does not establish this cross-layer invariant.

## Task 4: Terraform-managed build-and-deploy

**Files in companion infra branch:** `pages/main.tf`, `pages/tests/twihn.tftest.hcl`, `docs/twihn-pages.md`.

1. Change mocked assertions first to require a build/install/verify command (not `exit 0`), output `dist`, pinned Node for prod+preview, and explicit lockfile-based dependency installation. Existing Git integration, domain, DNS and prevention-of-destruction scenarios remain.
2. Run `TF_VAR_twihn_custom_domain_enabled=false terraform -chdir=pages test` and record the intended RED against unchanged main config.
3. Replace only build configuration/environment settings. Use a build command compatible with current site main (the npm/build/verifier scripts already exist), e.g. `npm install --global npm@12.0.2 && npm ci && npm run build && npm run verify:dist`. Retain `SKIP_DEPENDENCY_INSTALL=1` only to skip Cloudflare's automatic installer, since the command performs explicit locked installation. Set both environments to Node 22.23.5. Do not alter source project, DNS, domain activation, credential scopes, provider lock or apply gates.
4. Update operator documentation with infrastructure-first compatibility, observed-vs-reported gates, preview retry for exact site head, human merge order/stack retarget and rollback. No dashboard drift, new project or inferred production success.
5. Run `terraform fmt -check -recursive`, backend-disabled init with readonly lockfile, validate, and mock tests under both inherited flag values. Commit the scoped infra changes. No real apply and no local remote state reads are needed for these tests; live PR plan is a separate read-only CI gate.

## Task 5: Documentation and integrated evidence

**Files:** `README.md`, `runtime/README.md`, `runtime/STORY_TAGS.md`, `runtime/SCHEDULED_JOB.md`, applicable current operator docs. Historical approved design/plan files remain historical and need not be rewritten.

1. Find active instructions requiring committed dist and update them consistently. Explain ignored output, clean checkout build, reproducibility, source-only content commits, required installed trusted dependencies for offline expectation builds, restore/recovery and exact production readback.
2. Preserve narrow automation permissions, frozen issue/window data, no-resend, baseline email fallback and post-merge activation gates. No private paths/data/credentials in public examples; use placeholders for operator secrets and IDs.
3. From clean source-only worktree run all pinned suites/check/build/verifier and repeat build. Verify `git ls-files dist` is empty, output generation is ignored, no reviewed issue/revision/source data changes, and a second build leaves source/index unchanged. Keep evidence private.
4. Obtain independent specification PASS and independent quality/security/privacy PASS for source and infra changes; fix blockers with targeted RED/GREEN regressions and rereview the affected scope. Do not treat interrupted review as approval.
5. Inspect final diff, source/tag-data preservation, original tagging head unchanged, and exact branch base/head. Commit final reviewed fixes/docs.

## Task 6: New PR delivery and operator gates

1. Refresh auth with the verified GitHub App workflow; use an environment-backed askpass helper, never a token-bearing URL or command argument. Pass exact auth/toolchain/worktree instructions to workers. Parent performs authorized remote writes; workers do not merge/apply.
2. Push `ci/twihn-build-on-deploy` and create a new human-reviewed infrastructure PR against home-tf main. Read back exact body/base/head/changed paths and inspect actual CI plan; ensure no apply steps ran on PR. Explain any automatic apply on human merge.
3. Push `ci/build-on-deploy` and create a separate new site PR, initially based on `feat/story-tags` while tagging PR remains open. Read back exact body/base/head/files; do not change original tagging PR.
4. Verify latest trusted checks/statuses for exact PR heads, following pagination and distinguishing absent/skipped/pending/failed checks. Inspect failures and fix actual code/config errors. If infrastructure is not applied, report Cloudflare's old-config preview failure as blocked, not passing; do not bypass the human merge gate to make it green.
5. Final report includes both new PR links, exact tested head(s), real test/review results, original PR untouched and explicit remaining infrastructure/preview/stack/human merge/production/scheduler gates. No production deployment, real email send or live scheduler change is authorized here.

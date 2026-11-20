# AI Story Tags and Paginated Archives — Implementation Plan

> **For implementer:** Use TDD throughout. Write failing test first. Watch it fail. Then implement. Use `superpowers` subagent development: one fresh implementer per task, spec review, then quality review, resolving findings before proceeding.

**Goal:** AI-tag every published story using a reuse-first free-form registry; render tags on weekly issues and complete static tag archives with 20 stories per page, including a safe historical backfill and scheduled publication integration.

**Architecture:** Keep one independent JSON record per HN ID plus an append-only tag registry in Git. Resolve reviewed facts from current issue JSON, keep AI assignments in a separately frozen website-only sidecar, and rebuild all tag pages during every publication. Extend existing content authorization at both path and record level without changing email delivery guarantees.

**Tech Stack:** Existing pinned Astro/TypeScript/Node/npm, Node test runner, Python standard-library runtime and unittest, committed static Cloudflare Pages artifacts. The current scheduled AI research agent supplies tags; no additional model API, SDK, database or service credentials.

---

## Approved specification and working context

Read `docs/plans/2026-11-20-story-tags-design.md` in full. That approved design is authoritative. Do not re-open approved decisions or expand scope. All paths below are relative to `/home/hermes/projects/twihn-story-tags` unless absolute.

- Feature branch: `feat/story-tags`, isolated worktree `/home/hermes/projects/twihn-story-tags`.
- Baseline: `98b4213213e1fd031fb34bac5fa011876dcca696`, main verified on 2026-11-20.
- Production checkout `/home/hermes/projects/twihn` is on an older feature branch. DO NOT switch/update it, alter private runtime configuration, or activate the scheduler while implementing.
- Human merge gate applies to this source/backfill PR. Weekly content-only auto-merge authorization is not source merge authorization.
- Load `twihn`, `superpowers`, `github-auth`, `github-pr-workflow` and `requesting-code-review` before their relevant phases. Delegated workers inherit this exact worktree, the plan, verified authentication workflow and human merge gate. Use Hermes `delegate_task`, the available equivalent of the skill's OpenClaw `sessions_spawn`.
- Existing job `8b345921306f` was read back as Friday 08:00 Europe/London, enabled, following the main model, workdir `/home/hermes/projects/twihn`. Do not pin/change its model or schedule. Current job settings are an operational fact, not proof a new tagging flow is active.
- Do not send any test/historical email, run production delivery, modify DNS, bypass GitHub checks, retag frozen stories or publish fixtures.
- Test output described below is EXPECTED evidence to obtain, not output already observed during planning.

## Prerequisite: establish reproducible baseline

1. Read `.node-version` and `package.json`. Discover the existing isolated pinned Node/npm executables; do not replace system Node or assume system versions match. Check live executable versions. If missing, provision only an isolated toolchain under Hermes scratch/cache; follow `twihn` toolchain lessons.
2. Put the verified Node/npm bin directories first on PATH, then run:
   ```sh
   npm ci
   npm run check
   npm test
   python3 -m unittest discover -s tests/runtime -v
   python3 -m unittest discover -s tests -p 'test_*.py' -v
   npm run build
   npm run verify:dist
   git diff --exit-code -- dist
   git status --short
   ```
3. Capture exact versions and results for later PR evidence. If baseline fails, reproduce and distinguish it from new failures before changing code. Main's `verify-dist.mjs` has tight HTML/order/inventory assertions; inspect current ranking-precedence fixtures rather than reverting the merged ranking behavior.
4. Use `node:os.tmpdir()`/Python `tempfile` (honoring TMPDIR) for tests. Public fixtures live only under test temporary directories, never production `data/`.

## Contracts to implement

### Public registry and independent records

`data/tags.json`:
```json
{"schemaVersion":1,"tags":[{"slug":"ai","label":"AI"},{"slug":"security","label":"Security"}]}
```

`data/stories/12345678.json`:
```json
{"schemaVersion":1,"id":"12345678","tagSlugs":["ai","security"],"issueIds":["2026-11-20"]}
```

These are format examples, not production data. Serialize with existing `stableJSON`. Registry entries and story tags are ordered by slug; issue references ascending. Registry labels/slugs never change during ordinary weekly publication. IDs must be literal `/^[1-9][0-9]*$/`; validate before lookup. Slugs must be `/^[a-z0-9]+(?:-[a-z0-9]+)*$/`, bounded to 80 characters; label length 1–80 after trimming, Unicode NFC, no control characters. Reject leading/trailing whitespace and mechanical duplicate labels; normalize comparison using NFC, whitespace collapse and lowercase, retaining meaningful punctuation. `C`, `C++` and `C#` must not collapse into one label. Do not arbitrarily restrict the number of distinct topic tags.

`scripts/tags.mjs` exports:
- `normalizeLabel(label)` -> comparison key (validation does not silently alter stored labels).
- `validateRegistry(value)` -> validated registry.
- `validateStoryRecord(value, registry)` -> validated record.
- `projectStories(issues, records, registry)` -> unique resolved story objects; require exact current-issue coverage, references and tag membership.
- `loadStoryArchive(dataDir?)` -> `{issues, revisions, registry, records, stories}`; use existing `loadArchive` for issue validation.
- `tagArchivePages(stories, registry)` -> complete page descriptors; `pageSize = 20` fixed.
- `validateTagging(input, response)` -> canonical frozen sidecar with resolved slugs, existing assignments and proposed new registry entries.
- `applyTagging(archive, frozen, newIssue)` -> complete next registry/record set, with no mutation of source objects and no unauthorized prior assignment changes.

A resolved story contains `{item, tagSlugs, issueIds, sourceIssueId}`. Resolve `item` from the latest current issue referencing the story; never count retained superseded revisions as extra stories. Sort by submittedAt descending then numeric HN ID descending (`BigInt`, not floating-point conversion).

### Private tagging envelope

Input built from the exact proposed/frozen review and current production archive:
```json
{
  "schemaVersion":1,
  "issueId":"2026-11-20",
  "reviewedSha256":"<actual digest of exact reviewed bytes>",
  "registrySha256":"<actual digest of canonical registry bytes>",
  "registry":{"schemaVersion":1,"tags":[]},
  "stories":[{"id":"12345678","title":"...","story":"...","discussion":"...","existingTagSlugs":[]}]
}
```

AI response: `{ "assignments": { "12345678": ["AI", "Security"] } }`. Exact requested IDs only. Existing assignments must be reused; do not ask the AI to invent alternatives for already-tagged stories. The validator maps labels to registry slugs, validates/allocates new slugs deterministically, and refuses a new label whose candidate slug conflicts with another meaning. `C++`/`C#` can use deterministic readable symbol names (`c-plus-plus`, `c-sharp`) or fail explicitly for a supplied safe slug decision; never conflate them with `C` or silently append random suffixes. Shared test vectors must pin the chosen rule.

Frozen sidecar: `{schemaVersion, issueId, reviewedSha256, registrySha256, registry, assignments, additions}` where `assignments` maps HN IDs to nonempty sorted slug arrays. The registry snapshot is the exact one used by the model; `additions` contains the proposed `{slug,label}` objects. Keep the canonical bytes, actual checksum and input/response files private. No invented digest placeholders in real artifacts.

`runtime.weekly approve --tagging /private/candidate.json` freezes the exact validated review first, then inside its WEBSITE-only error boundary validates and freezes the sidecar with existing atomic/private-store conventions. Metadata gains private `taggingPath` and `taggingSha256` bindings; exclude them and the registry snapshot from the public issue projection. Missing/invalid sidecar prevents a new website publication, not baseline email preparation. After a recoverable tagging failure, approval may finish preparing previously absent metadata from the same frozen review without resetting email state. Already-frozen metadata is immutable.

Python should verify binding/shape/exact IDs and immutable writes; Node is authoritative for public semantic reconciliation. If validation is shared through a Node CLI, invoke the configured pinned executable with an argv list, bounded timeout and no credentials. If small structural checks are repeated in Python, run common fixtures through both layers; do not build separate conflicting taxonomies.

## Task 1: Registry rules and schemas

**Files:** Create `schema/tags.schema.json`, `schema/story.schema.json`, `scripts/tags.mjs`, `tests/site-tags.test.mjs`.

**Step 1 — failing tests:** Start with real registry validation and label reuse, not a permissive mock:
```js
import test from 'node:test';
import assert from 'node:assert/strict';
import * as tags from '../scripts/tags.mjs';
const registry = {schemaVersion:1,tags:[{slug:'ai',label:'AI'}]};
test('registry rejects mechanically duplicated labels', () => {
  assert.equal(typeof tags.validateRegistry, 'function');
  assert.throws(() => tags.validateRegistry({schemaVersion:1,tags:[
    ...registry.tags,{slug:'artificial-intelligence',label:'ai'}
  ]}), /duplicate/i);
});
```
Run `node --test tests/site-tags.test.mjs`; confirm the intended missing-validator/behavior failure. Add each further case only before implementing it: invalid slug/path traversal/control text/unknown fields/duplicate slugs/trimmed labels, empty registry, multiple tags and punctuation-distinct labels.

**Step 2 — implementation:** Use existing AJV/addFormats and schema-loading conventions. Both schemas specify `additionalProperties:false`, constant schema version, nonempty/unique arrays where appropriate and safe patterns. Comparison helper's core is:
```js
export function normalizeLabel(label) {
  return label.normalize('NFC').trim().replace(/\s+/gu, ' ').toLowerCase();
}
```
Validate type/control/length before calling it. Enforce duplicate label keys/slugs in `validateRegistry`; enforce nonempty references and valid IDs in `validateStoryRecord`. Preserve the original accepted label bytes.

**Step 3 — green/refactor:** `node --test tests/site-tags.test.mjs && npm test`. Commit `feat: validate free-form tag registry and story records`.

## Task 2: Exact story coverage and resolved story loader

**Files:** Modify `scripts/tags.mjs`, `tests/site-tags.test.mjs`; create reusable `tests/site-tag-fixtures.mjs`.

**Step 1 — failing behavior:** Build records from `fixture()` in `tests/site-fixtures.mjs`. Assert exact missing/extra ID failures, wrong filename/id, dangling tag, missing/extra issue references, duplicate ID across rankings counted once, source fields resolved from current issue and old revisions excluded. Use temporary directories for loader cases. Minimal key test:
```js
test('each published ID needs exactly one independent tagged record', () => {
  const issue = fixture();
  assert.throws(() => tags.projectStories([issue], [], registry), /missing|coverage/i);
});
```
Run `node --test tests/site-tags.test.mjs`; capture RED before adding implementation.

**Step 2 — implementation:** Build a map of current issue ID -> validated issue and HN ID -> actual issue references. Compare exact sets with the provided records, choose latest issue ID for `sourceIssueId`, resolve its `items[id]`, reject unused registry entries, and return one object per ID. `loadStoryArchive` enumerates only regular `<ID>.json` records and rejects symlink/unexpected entries. An empty site accepts an empty registry plus zero stories. No fallback silently ignores absent tags for a nonempty site.

**Step 3:** Run targeted test, `npm test`; commit `feat: load independently tagged stories from reviewed issues`.

## Task 3: Deterministic ordering and 20-story pagination

**Files:** Modify `scripts/tags.mjs`, `tests/site-tags.test.mjs`.

**Step 1 — RED:** For sizes 0/1/19/20/21/40/41 assert page counts 0/1/1/1/2/2/3, exact ID sequences, no duplicates, final remainder, previous/next targets and no `/1/` route. Exercise date ties with IDs above JavaScript's safe integer range. Test a story in two tags appears once in each tag, not twice in either. Do not claim cross-week duplicates from structurally invalid issue fixtures; test the pure projection/page functions with valid inputs at their own boundary.

**Step 2 — implementation core:**
```js
const newestFirst = (a,b) => {
  const time = Date.parse(b.item.submittedAt) - Date.parse(a.item.submittedAt);
  if (time) return time;
  return BigInt(a.item.id) === BigInt(b.item.id) ? 0 : BigInt(a.item.id) > BigInt(b.item.id) ? -1 : 1;
};
const pageUrl = (slug,n) => `/tags/${slug}/${n === 1 ? '' : `${n}/`}`;
```
For each sorted registry tag, filter unique resolved stories, sort, slice at `(page-1)*20`, and emit `{tag, page, pageCount, storyCount, stories, url, previousUrl, nextUrl}`. Return no page for zero stories; registry validation prevents orphan tags in real archives.

**Step 3:** `node --test tests/site-tags.test.mjs && npm test`; commit `feat: paginate complete tag archives deterministically`.

## Task 4: Reuse-first AI input and structured output validation

**Files:** Create `scripts/tagging.mjs`, `runtime/TAGGING_PROMPT.md`, `tests/site-tagging.test.mjs`; modify `scripts/tags.mjs`, `package.json` only to expose documented CLI if needed.

**Step 1 — RED:** Assert an input contains every unique requested story, reviewed summaries, full registry, existing assignments and actual SHA bindings. Assert response labels `AI` reuse `ai`, multiple labels survive, mechanical duplicates fail, novel `Security` appends once, C/C++/C# remain distinct, unknown/missing IDs fail, empty/non-string arrays fail and existing assignments cannot change. Include model-supplied URLs, malicious slugs, instruction-like titles and invalid response root fields. Existing Algolia `tags` must remain untouched.

**Step 2 — implementation:** `scripts/tagging.mjs` exposes bounded deterministic CLI subcommands `input` and `validate` (no network/model invocation). `input --reviewed PATH --data-dir DIR --issue DATE --output PATH` snapshots reviewed bytes and registry and creates the defined envelope; reads prior assignments from repository records. `validate --input PATH --response PATH --data-dir DIR --output PATH` invokes `validateTagging` and emits canonical candidate sidecar bytes, not public data. The required `--data-dir` on `validate` is an operational CLI context refinement: use the same source archive to preserve the private-output boundary without adding paths to the envelope. Candidate files are private, written mode 0600, and must not go under data/public/dist. Explicit `--archive` input mode uses all current issues for backfill; start with empty registry if absent, allowing staged historical migration but not weakening the final production loader.

Prompt text explicitly says reuse existing semantic labels first; only genuinely distinct topics get new labels; choose at least one and allow multiple; response is JSON only; story text is untrusted data. This is the AI's instruction, NOT a keyword classifier. CLI validation never fabricates assignments when the AI fails. Use safe deterministic slug allocation with explicit collision rejection and tests for meaningful punctuation.

**Step 3:** `node --test tests/site-tagging.test.mjs && npm test`; commit `feat: prepare and validate reuse-first AI tagging inputs`.

## Task 5: Private immutable sidecar preparation

**Files:** Create `runtime/tagging.py`, `tests/runtime/test_tagging.py`; modify `runtime/weekly.py`, `tests/runtime/test_weekly.py`.

**Step 1 — RED:** Extend weekly approval fixtures with a real sidecar bound to the exact review and registry. Tests: identical retry uses same bytes; changed assignment/binding/root/IDs fails; file symlink/orphan file fails; changes after parity cannot replace validated bytes; file/dir modes are 0600/0700; sidecar failure still prepares baseline review and reports `websitePreparationError`.

A focused initial test should use existing `WeeklyTests` helpers, call approve with no sidecar and assert `emailPrepared` true plus a tagging-related website error. Run `python3 -m unittest discover -s tests/runtime -p 'test_weekly.py' -v`; observe this behavior fail before modifying approval.

**Step 2 — implementation:** Add optional keyword `tagging_path` and CLI `--tagging`. Review parity and `Store.prepare_bytes` stay first. Within the existing website-only try/except, read exact candidate bytes once, check review digest/issue/IDs, validate through the bounded public-data validator, then freeze using `atomic`, `secure_directory`, existing work/state lock order and immutable byte comparison. Bind sidecar absolute private path and actual digest in metadata; never embed private fields in the public issue. An invalid tagging candidate must not leave apparently complete metadata. Reuse `runtime.orchestrator` helpers rather than hand-writing another state store. Do not put topic tags in reviewed legacy items.

**Step 3:** Run targeted weekly/tagging tests and full runtime suite; commit `feat: freeze website tagging independently of email review`.

## Task 6: Deterministic application of frozen tags

**Files:** Modify `scripts/tags.mjs`, `scripts/tagging.mjs`, `tests/site-tagging.test.mjs`.

**Step 1 — RED:** Given old registry/records and a new issue, assert append-only registry additions, new tagged records for exactly new issue IDs, stable old assignments, valid issue-reference appends and byte-idempotent retries. Reject removed/renamed tags, conflicting slug reuse, changed old assignments and conflicting registry movement. Compatible independently appended registry labels may be preserved, but they must not reinterpret a frozen slug.

**Step 2 — implementation:** `applyTagging` takes copied validated inputs and compares the complete frozen registry snapshot with current mappings. Existing mappings and assignments must match; additions may be appended only if label/slug identities remain compatible. Construct all affected records in memory; validate the complete next projection before writing anything. CLI `apply --sidecar PATH --issue PATH --data-dir DIR` writes only the exact calculated record/registry set using canonical serialization after successful validation. Normal weekly application never performs the initial all-history migration.

**Step 3:** Run targeted + site tests; commit `feat: reconcile frozen tag assignments with repository content`.

## Task 7: Publisher metadata and narrowly authorized record diffs

**Files:** Modify `runtime/publisher.py`, `tests/runtime/test_publisher.py`; create `scripts/verify-tag-changes.mjs`, `tests/site-tag-changes.test.mjs`.

**Step 1 — RED:** Extend `snapshot` tests with exact sidecar digest/read-once tamper detection and private projection exclusion. Path tests allow only current issue + generated dist + `data/tags.json` + numeric story filenames justified by current issue. Semantic tests reject old label edit/removal, unrelated story add, existing assignment edit, orphan reference, hidden unknown field, source path, historical issue change, symlink/mode or rename/delete, and stale base. A passing path allowlist alone must not authorize modified historic records.

**Step 2 — implementation:** Keep private tagging bindings separate from `PUBLIC_METADATA`. Snapshot review/metadata/sidecar exactly once before the clean checkout; verify frozen hashes. After the issue import, apply frozen tags and run record-diff validator against the exact fetched base. Require `data/issues/<current>.json` and generated output; permit registry append and only current issue's justified story additions/reference appends. The Node validator computes the expected next archive from base + frozen sidecar + imported issue and compares exact proposed registry/records, checking unchanged history. Python preserves server-read-back path/blob identities and invokes this validation before any branch push. No blanket `data/**` authorization.

Do not grant backfill auto-merge permission. Keep all strict server policy checks, trusted exact-head CI, main-base revalidation and source-PR refusal. Include tagging digest in deterministic branch identity to prevent same-review/different-tag collision; existing branch recovery still requires identical tree and parent.

**Step 3:** Run `node --test tests/site-tag-changes.test.mjs`, publisher tests, full site/runtime regressions. Commit `feat: guard weekly story and registry content changes`.

## Task 8: Backfill CLI and offline no-send proof

**Files:** Create `scripts/backfill-tags.mjs`, `tests/site-backfill.test.mjs`; extend `runtime/TAGGING_PROMPT.md`.

**Step 1 — RED:** Temporary archive tests prove one input per unique current story, retained revisions excluded, later batches receive accepted labels, unchanged input/result can resume, duplicate/missing/extra stories fail, a partial backfill cannot become a complete production archive, and byte-for-byte source issue/revision preservation. No import of runtime delivery or transport functions; use a test sentinel/fake executable to prove no mail command is invoked.

**Step 2 — implementation:** CLI has explicit `prepare`, `accept` and `apply` steps. `prepare --data-dir DIR --work-dir PRIVATE` saves durable source digests, registry, unique story inputs and progress. `accept --work-dir PRIVATE --response PATH` validates actual AI response and carries accepted registry labels forward without replacing prior results. `apply --data-dir DIR --work-dir PRIVATE` refuses changed source digests/incomplete coverage, validates complete records and writes only `data/tags.json` and `data/stories/*.json`. It never calls network collection, delivery prep/resume, email send, or remote publication.

**Step 3:** `node --test tests/site-backfill.test.mjs && npm test`; commit `feat: backfill historical tags without modifying issues or email`.

## Task 9: Perform the real AI backfill in the feature worktree

**Files:** Create actual `data/tags.json`, `data/stories/<actual-ID>.json`. Private input/response/progress under a restricted durable per-task directory outside repository, e.g. `/home/hermes/.hermes/data/twihn/tagging-backfill/2026-11-20/`.

**Step 1 — prerequisites:** All Task 8 tests pass. Fetch/read current main and compare issue/revision inputs with the baseline; incorporate legitimately published content before final source delivery. Never overwrite concurrent content. Prepare real input with the implemented CLI. Count exact unique requested IDs programmatically.

**Step 2 — AI classification:** A parent or dedicated AI subagent reads the actual prepared titles and reviewed paragraphs, full registry and reuse-first prompt, then writes structured responses. No handcrafted keyword map, synthetic summaries, guessed model output or fake service response. For multiple batches append accepted results to durable JSON, pass the growing registry to the next batch and verify deduped coverage in code. The model may reuse broad tags; inspect near-synonyms before acceptance rather than invent a taxonomy in advance. If blocked, report the blocker; do not populate production with fixture tags.

**Step 3 — apply/verify:** Accept all responses through validators, apply complete records, validate the resulting story archive and compare every existing issue/revision byte with pre-backfill source. Only actual new registry/story files may change. Run site/runtime regressions and inspect `git diff --stat` plus exact public record set. Commit `content: backfill AI topic tags for published stories`. Generated dist follows once routes exist. This is real content in a human-reviewed source branch, not a production publication or email action.

## Task 10: Shared story/tag components and historical weekly rendering

**Files:** Create `src/components/TagLinks.astro`, `src/components/Story.astro`; modify `src/components/Issue.astro`, `src/lib/types.ts`, `src/styles/global.css`, `src/pages/index.astro`, `src/pages/issues/[issue].astro`, `tests/site-build.test.mjs`, `tests/site-tag-fixtures.mjs`.

**Step 1 — RED:** Extend real fixture build inputs with complete registry/records. Assert latest homepage and historical issue story titles are followed by exact tag links; story appears once in current precedence ranking, facts/summary bytes unchanged, dangerous label text escaped, and source/correction metadata previously removed stays absent. Include at least one story with two tags. Run `node --test tests/site-build.test.mjs` and observe missing links before production changes.

**Step 2 — implementation:** Load the shared resolved archive in both page entry points and pass registry/record maps into Issue. Extract only the existing story `<li>` payload into `Story.astro`, preserving title/metrics/date/HN link/Story/Discussion output. `TagLinks.astro` receives validated label/slug data and renders ordinary escaped `<a href={...}>` elements inside an accessible tag list. Archive-only original issue links are explicit props, not accidentally added metadata on weekly cards. Add restrained responsive CSS with no inline styles or scripts. Do not reinstate the removed header tagline.

**Step 3:** Targeted build test, `npm run check`, `npm test`; commit `feat: display shared topic tags on every weekly issue`.

## Task 11: Static tag directory and paginated archive routes

**Files:** Create `src/pages/tags/index.astro`, `src/pages/tags/[slug]/index.astro`, `src/pages/tags/[slug]/[page].astro`, `src/components/TagArchive.astro`; modify `src/layouts/Layout.astro`, `tests/site-build.test.mjs` or create `tests/site-tag-build.test.mjs`.

**Step 1 — RED:** A real build with 41 unique synthetic stories under one tag must serve `/tags/`, `/tags/ai/`, `/tags/ai/2/`, `/tags/ai/3/` with 20/20/1 IDs in exact order. Assert `/tags/ai/1/`, `/tags/ai/4/` and an unknown slug have no generated artifacts. Check Previous/Next boundaries, canonical paths, alphabetical directory labels, unique counts, multi-tag cards, original issue links and shared Tags navigation.

**Step 2 — implementation:** Both dynamic entry points use `getStaticPaths` from existing repository patterns. First-page route selects page===1 descriptors; numbered route selects page>1 and string page params. Shared `TagArchive` renders page title/count, stories via shared Story, pagination and directory link. Use `Layout` canonical `path` per page. No client JS or data fetch at view time. Empty site directory states neutrally that no tags are available, without creating fake topic pages.

**Step 3:** Route build tests and `npm run check`; commit `feat: add twenty-story static tag archives and directory`.

## Task 12: Public JSON, strict artifact inventory and full rebuild verification

**Files:** Modify `scripts/artifacts.mjs`, `scripts/validate.mjs`, `scripts/build.mjs`, `scripts/verify-dist.mjs`, `tests/site-build.test.mjs`, `tests/site-tag-build.test.mjs`.

**Step 1 — RED:** Assert public `/data/tags.json`, `/data/stories/<id>.json`, `/schema/tags.schema.json` and `/schema/story.schema.json` match validated source bytes and are inventoried. Tamper with a tag page's membership/order/links/count, omit a required page, insert an extra page, then regenerate manifest; verifier must still reject it independently of hashes. Build twice to compare complete inventory and every byte. Reduce fixture membership from 41 to 20, rebuild to the same output directory and assert obsolete page 2/3 files disappear. Untagged nonempty production inputs must fail `npm run check` and build.

**Step 2 — implementation:** Integrate `loadStoryArchive` into validation/build. `expectedData` exports tags/stories/new schemas in addition to unchanged issue/revision JSON. Compute exact tag HTML route set from validated descriptors, require every expected artifact and reject extra routes. Verify exact membership/order per page, rendered labels/hrefs, page counts/boundary navigation, issue tags and public-data parity, not loose regex presence alone. Prefer small semantic HTML parsers/helpers consistent with existing dependencies; no broad rewrite. Clean generated output before rebuild using Astro's existing outdir semantics and a focused cleanup if required; never delete input/source/private paths. Run whole-site build every time; no incremental tag-page build.

**Step 3:** Full site/runtime/collector suites, build, verify. Commit `feat: verify complete tag archive artifacts and public data`.

## Task 13: Exact-production readback for tagging

**Files:** Modify `runtime/site_verify.py`, `tests/runtime/test_site_verify.py`, `runtime/publisher.py`, `tests/runtime/test_publisher.py`.

**Step 1 — RED:** Exact-SHA/Cloudflare test fixtures now include public tags, relevant story records, directory and affected tag pages. Wrong registry/story assignment/tag HTML/manifest, preview-only evidence, wrong commit, changed main or expired deadline must fail. A success without any tag readback evidence must not pass as a complete tagged deployment.

**Step 2 — implementation:** Retain existing frozen-review `parity` and exact production evidence. Verify expected registry/affected story assignment readbacks and derive affected tag routes from the validated artifact inventory. Read live directory plus first/last pages of tags touched by current issue (deduped), including pagination-boundary evidence when present; verify all expected routes locally. Keep HTTP URLs hardcoded to canonical twihn.com, strict TLS/cache/redirect handling, bounded total requests and existing remaining-deadline checks. Return explicit tagging evidence, actual checked paths and checksums; do not claim every historical page was fetched remotely. If affected tags produce more requests than the stage budget allows, fail website cleanly and preserve baseline rather than weaken checks. No unbounded scan of the entire lifetime archive.

After merge, publisher readback must verify the registry/story blobs it actually committed, not merely the current issue JSON, before claiming the content result. Preserve idempotent recovery of already-published compatible content; old frozen email records never need to be reset just to read tagged production pages.

**Step 3:** Full runtime suite and site integration tests. Commit `feat: verify tagged production artifacts against exact deployed source`.

## Task 14: End-to-end scheduled failure and retry regressions

**Files:** Modify `tests/runtime/test_weekly.py`, `tests/runtime/test_publisher.py`, `tests/runtime/test_runtime.py`; create `tests/runtime/test_tagged_publication.py` if a focused end-to-end fixture is clearer.

**Step 1 — RED:** Exercise actual CLI/subprocess boundaries with local fake transport/GitHub boundaries, not only mocked helper return values. Scenarios: complete tagged new issue; missing/invalid tags -> website failure + validated baseline email; frozen retry -> no AI regeneration; stale base/conflicting registry -> no mutation; byte-identical existing content -> read-only recovery; website-only repair never touches email; verified/ambiguous historical email never resends; metadata and sidecar races fail safely. Prove full archive rebuild receives all retained issues, not just the new issue.

**Step 2 — implementation:** Fix only root-cause integration defects exposed by tests. Keep existing process-group deadline, locks, restrictive file modes, reviewed byte freezing, complete independent channel states and uncertain-send protections. No source gate or CI bypass.

**Step 3:**
```sh
npm run check
npm test
python3 -m unittest discover -s tests/runtime -v
python3 -m unittest discover -s tests -p 'test_*.py' -v
```
Commit `test: cover tagged weekly publication and safe fallback end to end` (include minimal fixes only if needed).

## Task 15: Operator runbook, scheduler prompt handoff and generated release

**Files:** Modify `README.md`, `runtime/README.md`, `runtime/SCHEDULED_JOB.md`; create `runtime/STORY_TAGS.md`; regenerate committed `dist/`.

1. Document exact tested CLI argv for input preparation, AI response, validation, `weekly approve --tagging`, normal publish/resume and website-only recovery. Use private path placeholders and no credentials/actual recipient values. Describe registry reuse, stable slugs, complete backfill, immutable issue JSON, 20-story archive pagination and the distinction between AI semantic judgement and mechanical duplicate validation.
2. Provide complete replacement/addition instructions for existing cron research step: read current production registry and existing assignments; prepare input from exact reviewed bytes; have the current AI model tag all unique IDs; validate sidecar; approve even if website tagging fails so baseline email remains available; call only the existing runtime's authorized resume. Do not create a duplicate job, pin a model, change the recipient, add standalone email sends, or paste private production paths into public records.
3. Prepare a post-human-merge activation checklist. Re-read live cron first; update its prompt through supported cron tools only AFTER exact source merge/CI/deployment and launch-check verification. Read back the exact job after any later update. During feature implementation leave the live job unchanged. Update `twihn`/digest skill runbooks at activation so a fresh scheduled agent gets the tested tagging instructions; do not rely on this conversation.
4. Re-run the exact pinned full validation/build suite, rebuild twice, compare output byte-for-byte, and commit all generated tag pages/data/schemas/assets/manifest. Preserve historical issue/revision JSON byte identity.
5. `git diff --check` and inspect changed paths for private artifacts/synthetic content. Commit `docs: document tagged publication and rebuild generated archives`.

## Task 16: Independent reviews, source PR and final evidence

**Files:** No new feature scope. Fix only findings, with fresh failing tests where behavior changes.

1. Dispatch overall independent spec review and code-quality/security review with approved design + complete diff + actual test evidence. Review must explicitly check real AI backfill, archive membership across all history, issue tag rendering, 20-entry pagination, deterministic clean builds, baseline email safety and narrowed content authorization. Resolve/re-review findings.
2. Verify Node/npm versions and run the entire repository suite, build and artifact verifier again. `git diff --exit-code -- dist` must pass after committed regeneration and `git ls-files --others --exclude-standard dist` must be empty. Record actual counts from code, not mental tally.
3. Use GitHub App auth from `github-auth` (read verified-token-generation reference if needed). Never expose tokens/private keys in commands, logs, docs or a PR. Use existing verified credential script/askpass workflow rather than guessed auth. Push only `feat/story-tags`, create source PR against current main, `draft:false`, with complete prerequisites, commands, private-path placeholders, test evidence, unchanged-email assurance, backfill coverage and activation/rollback instructions. Do not merge it.
4. Read back PR number/URL/exact head/base/changed paths; monitor trusted `validate` and Cloudflare preview for that exact head. Inspect failures and fix root causes. Read effective required checks when authorized; distinguish observed checks from unverified branch-policy prerequisites. Re-read head before advising merge. Do not claim success while relevant CI is failing/absent.
5. Verify preview routes for tags and historical weekly issue, with real content and no synthetic stories. Production tags and scheduler changes are NOT claimed active until Steve merges and actual production deployment readback passes.
6. Deliver concise PR URL, exact tested head, checks and any remaining operator action. Preserve worktree while PR is open; no destructive cleanup or automatic source merge.

## Acceptance checklist for the overall reviewer

- [ ] Every unique current published story has a real AI-assigned, nonempty multi-label-capable record.
- [ ] Registry is persisted; model sees full existing labels and prefers semantic reuse; mechanical duplicates rejected.
- [ ] Latest homepage, historical issues and archive stories display working tag links.
- [ ] `/tags/` and every complete tag archive exist with 20 stories/page, deterministic newest-first order and correct navigation.
- [ ] Scheduled publication fully rebuilds archives; frozen retries do not retag or mutate old assignments.
- [ ] Reviewed issue and retained revision JSON remain byte-identical under the backfill.
- [ ] Sidecar is bound to exact review/registry input and kept private; no credentials leak into public files.
- [ ] Website tagging errors preserve complete baseline email and never resend historic/uncertain email.
- [ ] Publisher permits only semantically justified issue/story/tag content updates and retains all strict merge guards.
- [ ] Local validation, realistic subprocess tests, clean/repeatable build, full artifact verification, exact-head CI and preview all have actual evidence.
- [ ] Human source merge, production verification and live cron activation remain explicit separate gates.

## Execution handoff

Offer Steve two options after this plan is committed:
1. **Subagent-driven in this session:** fresh implementer for each task, followed by separate spec and quality reviews, then source PR delivery.
2. **Manual:** use this saved plan to run tasks independently.

Do not start application implementation before that choice. Do not call the final feature complete because the plan exists.

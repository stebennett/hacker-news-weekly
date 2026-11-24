import { readFile, readdir, lstat } from 'node:fs/promises';
import { loadArchive, validateIssue, stableJSON } from './content.mjs';
import { createHash } from 'node:crypto';
import path from 'node:path';
import Ajv from 'ajv';
import addFormats from 'ajv-formats';
import { parseStrictJSON } from './strict-json.mjs';

// Keep the exact once-read bytes used for AJV compilation private.
const registrySchemaBytes = await readFile(path.resolve('schema/tags.schema.json'));
const storySchemaBytes = await readFile(path.resolve('schema/story.schema.json'));
const registrySchema = parseStrictJSON(registrySchemaBytes);
const storySchema = parseStrictJSON(storySchemaBytes);
const ajv = new Ajv({allErrors:true,strict:true});
addFormats(ajv);
const registryStructural = ajv.compile(registrySchema);
const storyStructural = ajv.compile(storySchema);
const labelStructural = ajv.compile(registrySchema.properties.tags.items.properties.label);
const storyIdStructural = ajv.compile(storySchema.properties.id);
const tagSlugsStructural = ajv.compile(storySchema.properties.tagSlugs);
const submittedAtStructural = ajv.compile({type:'string',format:'date-time',pattern:'Z$'});
const requireThat = (condition, message) => { if (!condition) throw new Error(message); };

// Each caller owns its copies; exported buffers cannot mutate the snapshots.
export function tagSchemaSourceBytes() {
  return {tags:Buffer.from(registrySchemaBytes),story:Buffer.from(storySchemaBytes)};
}

export function normalizeLabel(label) {
  requireThat(labelStructural(label), `Invalid tag label: ${ajv.errorsText(labelStructural.errors)}`);
  const key = label.normalize('NFC').trim().replace(/\s+/gu, ' ').toLowerCase();
  requireThat(key.length > 0, 'tag label must not be blank');
  return key;
}

// Public records are canonical already: validate without sorting or changing bytes.
function requireSorted(values, label) {
  requireThat(values.every((value, index) => index === 0 || values[index - 1] < value), `${label} must be sorted ascending`);
}

export function validateRegistry(value) {
  requireThat(registryStructural(value), `Invalid tag registry schema: ${ajv.errorsText(registryStructural.errors)}`);
  const slugs = new Set(), labels = new Set();
  for (const tag of value.tags) {
    requireThat(tag.label === tag.label.trim(), 'tag label must already be trimmed');
    requireThat(tag.label === tag.label.normalize('NFC'), 'tag label must already be Unicode NFC');
    const key = normalizeLabel(tag.label);
    requireThat(!slugs.has(tag.slug), `duplicate tag slug: ${tag.slug}`);
    requireThat(!labels.has(key), `duplicate tag label: ${tag.label}`);
    slugs.add(tag.slug); labels.add(key);
  }
  requireSorted(value.tags.map(tag => tag.slug), 'registry tags');
  return value;
}

export function validateStoryRecord(value, registry) {
  requireThat(storyStructural(value), `Invalid story record schema: ${ajv.errorsText(storyStructural.errors)}`);
  const slugs = new Set(validateRegistry(registry).tags.map(tag => tag.slug));
  for (const slug of value.tagSlugs) requireThat(slugs.has(slug), `unknown tag reference: ${slug}`);
  requireSorted(value.tagSlugs, 'story tagSlugs');
  requireSorted(value.issueIds, 'story issueIds');
  return value;
}

export function projectStories(issues, records, registry) {
  requireThat(Array.isArray(issues) && Array.isArray(records), 'issues and records must be arrays');
  validateRegistry(registry);
  const current = new Map(), references = new Map();
  for (const value of issues) {
    const issue = validateIssue(value);
    requireThat(!current.has(issue.issueId), `duplicate issue ID: ${issue.issueId}`);
    current.set(issue.issueId, issue);
    for (const id of Object.keys(issue.items)) {
      if (!references.has(id)) references.set(id, new Set());
      references.get(id).add(issue.issueId);
    }
  }
  const byId = new Map();
  for (const value of records) {
    const record = validateStoryRecord(value, registry);
    requireThat(!byId.has(record.id), `duplicate story record ID: ${record.id}`);
    byId.set(record.id, record);
  }
  const missing = [...references.keys()].filter(id => !byId.has(id));
  const extra = [...byId.keys()].filter(id => !references.has(id));
  requireThat(!missing.length && !extra.length, `story coverage mismatch: missing [${missing.join(', ')}]; extra [${extra.join(', ')}]`);

  const usedTags = new Set(), stories = [];
  for (const [id, appearances] of references) {
    const record = byId.get(id), issueIds = [...appearances].sort();
    requireThat(record.issueIds.length === issueIds.length && record.issueIds.every((value, index) => value === issueIds[index]), `story ${id} issue references do not match current issues`);
    const sourceIssueId = issueIds.at(-1);
    for (const slug of record.tagSlugs) usedTags.add(slug);
    stories.push({item:current.get(sourceIssueId).items[id], tagSlugs:[...record.tagSlugs], issueIds, sourceIssueId});
  }
  for (const tag of registry.tags) requireThat(usedTags.has(tag.slug), `unused registry tag: ${tag.slug}`);
  return stories;
}

export const sha256 = bytes => createHash('sha256').update(bytes).digest('hex');
export const registrySha256 = registry => sha256(stableJSON(validateRegistry(registry)));

const isObject = value => value !== null && typeof value === 'object' && !Array.isArray(value);
function exactKeys(value, keys, label) {
  requireThat(isObject(value) && Object.keys(value).length === keys.length && keys.every(key => Object.hasOwn(value, key)), `Invalid ${label} fields`);
}
export function validateTaggingIssueId(issueId) {
  requireThat(typeof issueId === 'string' && /^\d{4}-\d{2}-\d{2}(?![\s\S])/u.test(issueId), 'Invalid tagging issue ID');
  const date = new Date(`${issueId}T08:00:00Z`);
  requireThat(Number.isFinite(date.valueOf()) && date.toISOString().slice(0, 10) === issueId && date.getUTCDay() === 5, 'tagging issue must be a valid Friday');
  return issueId;
}

// A structurally valid review digest is only a binding carried forward here.
// The private runtime must compare it to the actual frozen review bytes later.
export function validateTaggingInput(input) {
  exactKeys(input, ['schemaVersion','issueId','reviewedSha256','registrySha256','registry','stories'], 'tagging input');
  requireThat(input.schemaVersion === 1, 'Invalid tagging schema version');
  validateTaggingIssueId(input.issueId);
  const digest = value => typeof value === 'string' && /^[a-f0-9]{64}(?![\s\S])/u.test(value);
  requireThat(digest(input.reviewedSha256) && digest(input.registrySha256), 'Invalid tagging SHA-256 binding');
  validateRegistry(input.registry);
  requireThat(input.registrySha256 === registrySha256(input.registry), 'tagging registry digest mismatch');
  requireThat(Array.isArray(input.stories) && input.stories.length > 0, 'tagging stories must be a nonempty array');
  const ids = new Set(), slugs = new Set(input.registry.tags.map(tag => tag.slug));
  for (const story of input.stories) {
    exactKeys(story, ['id','title','story','discussion','existingTagSlugs'], 'tagging story');
    requireThat(storyIdStructural(story.id), 'Invalid literal tagging story ID');
    requireThat(!ids.has(story.id), 'duplicate tagging story ID'); ids.add(story.id);
    for (const key of ['title','story','discussion']) requireThat(typeof story[key] === 'string' && story[key].trim().length > 0, `Invalid tagging ${key} text`);
    requireThat(Array.isArray(story.existingTagSlugs) && story.existingTagSlugs.every(slug => typeof slug === 'string' && slugs.has(slug)), 'Invalid existing tag assignments');
    requireSorted(story.existingTagSlugs, 'existing tag assignments');
  }
  return input;
}

export function slugForLabel(label) {
  normalizeLabel(label);
  const slug = label.normalize('NFKD').replace(/\p{M}/gu, '').toLowerCase()
    .replace(/\+/gu, '-plus-').replace(/#/gu, '-sharp-')
    .replace(/[^a-z0-9]+/gu, '-').replace(/^-|-$/gu, '');
  requireThat(slug.length > 0 && slug.length <= 80, 'tag label cannot produce a safe bounded slug');
  return slug;
}

export function validateTagging(input, response) {
  validateTaggingInput(input);
  exactKeys(response, ['assignments'], 'tagging response');
  requireThat(isObject(response.assignments), 'Invalid tagging assignments object');
  const ids = new Set(input.stories.map(story => story.id)), keys = Object.keys(response.assignments);
  requireThat(keys.length === ids.size && keys.every(id => ids.has(id)), 'tagging response must cover exact requested IDs');
  const known = new Map(input.registry.tags.map(tag => [normalizeLabel(tag.label), tag]));
  const bySlug = new Map(input.registry.tags.map(tag => [tag.slug, tag]));
  const added = new Map(), assignments = {};
  // Input/response property ordering never determines an addition's identity.
  for (const story of [...input.stories].sort((a, b) => BigInt(a.id) < BigInt(b.id) ? -1 : 1)) {
    const labels = response.assignments[story.id];
    requireThat(Array.isArray(labels) && labels.length > 0, 'tagging assignment must be a nonempty label array');
    const seen = new Set(), slugs = [];
    for (const label of labels) {
      const key = normalizeLabel(label);
      requireThat(!seen.has(key), 'duplicate equivalent tag labels for story'); seen.add(key);
      requireThat(!/^[a-z][a-z0-9+.-]*:\S|:\/\/|\.\.[/\\]|^[/\\]/iu.test(label.trim()), 'tag labels cannot be URLs or paths');
      let tag = known.get(key);
      if (!tag) {
        requireThat(label === label.trim() && label === label.normalize('NFC'), 'new tag label must already be trimmed and NFC');
        tag = added.get(key);
        if (tag) requireThat(tag.label === label, 'duplicate mechanically equivalent new tag labels');
        else {
          tag = {slug:slugForLabel(label), label};
          requireThat(!bySlug.has(tag.slug), 'new tag slug collision');
          added.set(key, tag); bySlug.set(tag.slug, tag);
        }
      }
      slugs.push(tag.slug);
    }
    slugs.sort();
    requireThat(!story.existingTagSlugs.length || stableJSON(slugs) === stableJSON(story.existingTagSlugs), 'existing story assignments cannot change');
    assignments[story.id] = slugs;
  }
  const additions = [...added.values()].sort((a, b) => a.slug < b.slug ? -1 : a.slug > b.slug ? 1 : 0);
  validateRegistry({schemaVersion:1,tags:[...input.registry.tags, ...additions].sort((a, b) => a.slug < b.slug ? -1 : a.slug > b.slug ? 1 : 0)});
  return {schemaVersion:1,issueId:input.issueId,reviewedSha256:input.reviewedSha256,registrySha256:input.registrySha256,
    registry:structuredClone(input.registry),assignments,additions};
}

// Validate a frozen result independently of the model input. Review bytes are
// verified by the private runtime; this public layer verifies carried bindings,
// the complete registry snapshot, and exact current-issue assignment coverage.
function validateFrozenTagging(frozen, issue) {
  exactKeys(frozen, ['schemaVersion','issueId','reviewedSha256','registrySha256','registry','assignments','additions'], 'frozen tagging');
  requireThat(frozen.schemaVersion === 1 && frozen.issueId === issue.issueId, 'frozen tagging issue/version mismatch');
  validateTaggingIssueId(frozen.issueId);
  const digest = value => typeof value === 'string' && /^[a-f0-9]{64}(?![\s\S])/u.test(value);
  requireThat(digest(frozen.reviewedSha256) && digest(frozen.registrySha256), 'Invalid frozen tagging SHA-256 binding');
  requireThat(frozen.registrySha256 === registrySha256(frozen.registry), 'frozen registry digest mismatch');
  validateRegistry({schemaVersion:1,tags:frozen.additions});
  const registry = validateRegistry({schemaVersion:1,tags:[...frozen.registry.tags,...frozen.additions].sort((a,b) => a.slug < b.slug ? -1 : a.slug > b.slug ? 1 : 0)});
  const ids = Object.keys(issue.items);
  requireThat(isObject(frozen.assignments) && Object.keys(frozen.assignments).length === ids.length && ids.length > 0 && ids.every(id => Object.hasOwn(frozen.assignments,id)), 'frozen assignments must cover exact current issue IDs');
  const used = new Set();
  for (const id of ids) {
    validateStoryRecord({schemaVersion:1,id,tagSlugs:frozen.assignments[id],issueIds:[issue.issueId]},registry);
    for (const slug of frozen.assignments[id]) used.add(slug);
  }
  requireThat(frozen.additions.every(tag => used.has(tag.slug)), 'unused frozen registry addition');
}

export function applyTagging(archive, frozen, newIssue) {
  validateIssue(newIssue);
  requireThat(newIssue.revision === 1, 'weekly tagging cannot apply a historical correction');
  validateFrozenTagging(frozen,newIssue);
  validateRegistry(archive.registry);
  const current = new Map(archive.registry.tags.map(tag => [tag.slug,tag]));
  // Compare ALL frozen mappings, including tags not assigned in this issue.
  for (const tag of frozen.registry.tags) requireThat(current.get(tag.slug)?.label === tag.label, 'frozen registry mapping removed or changed');
  const labels = new Map(archive.registry.tags.map(tag => [normalizeLabel(tag.label),tag.slug]));
  for (const tag of frozen.additions) {
    requireThat(!current.has(tag.slug) || current.get(tag.slug).label === tag.label, 'frozen addition slug conflicts with current registry');
    requireThat(!labels.has(normalizeLabel(tag.label)) || labels.get(normalizeLabel(tag.label)) === tag.slug, 'frozen addition label moved to another slug');
    current.set(tag.slug,tag);
  }
  // A caller must supply a complete base or a complete already-applied archive,
  // never relax historical coverage to accommodate an imported untagged issue.
  projectStories(archive.issues,archive.records,archive.registry);
  const matching = archive.issues.filter(issue => issue.issueId === newIssue.issueId);
  requireThat(!matching.length || stableJSON(matching[0]) === stableJSON(newIssue), 'cannot replace an existing issue during tagging');
  const registry = structuredClone({schemaVersion:1,tags:[...current.values()].sort((a,b) => a.slug < b.slug ? -1 : a.slug > b.slug ? 1 : 0)});
  const records = new Map(archive.records.map(record => [record.id,structuredClone(record)]));
  for (const id of Object.keys(newIssue.items)) {
    const existing = records.get(id), assigned = frozen.assignments[id];
    requireThat(!existing || stableJSON(existing.tagSlugs) === stableJSON(assigned), 'existing story assignments cannot change');
    records.set(id,{schemaVersion:1,id,tagSlugs:[...assigned],issueIds:[...new Set([...(existing?.issueIds ?? []),newIssue.issueId])].sort()});
  }
  const nextRecords = [...records.values()].sort((a,b) => BigInt(a.id) < BigInt(b.id) ? -1 : BigInt(a.id) > BigInt(b.id) ? 1 : 0);
  projectStories(matching.length ? archive.issues : [...archive.issues,newIssue],nextRecords,registry);
  return {registry,records:nextRecords};
}

export const pageSize = 20;

const newestFirst = (a, b) => {
  const time = Date.parse(b.item.submittedAt) - Date.parse(a.item.submittedAt);
  if (time) return time;
  const aId = BigInt(a.item.id), bId = BigInt(b.item.id);
  return aId === bId ? 0 : aId > bId ? -1 : 1;
};
const pageUrl = (slug, page) => `/tags/${slug}/${page === 1 ? '' : `${page}/`}`;

export function tagArchivePages(stories, registry) {
  requireThat(Array.isArray(stories), 'stories must be an array');
  validateRegistry(registry);
  const slugs = new Set(registry.tags.map(tag => tag.slug));
  // Validate the fields consumed here; projectStories owns reviewed issue facts
  // and exact references/coverage, including rejection of orphan registry tags.
  for (const story of stories) {
    requireThat(storyIdStructural(story?.item?.id), 'Invalid resolved story ID');
    requireThat(submittedAtStructural(story.item.submittedAt) && Number.isFinite(Date.parse(story.item.submittedAt)), 'Invalid story submittedAt timestamp');
    requireThat(tagSlugsStructural(story.tagSlugs), 'Invalid story tagSlugs');
    requireSorted(story.tagSlugs, 'story tagSlugs');
    for (const slug of story.tagSlugs) requireThat(slugs.has(slug), `unknown tag reference: ${slug}`);
  }

  const pages = [];
  for (const tag of registry.tags) {
    const unique = new Map();
    for (const story of stories) {
      if (story.tagSlugs.includes(tag.slug) && !unique.has(story.item.id)) unique.set(story.item.id, story);
    }
    const ordered = [...unique.values()].sort(newestFirst);
    const storyCount = ordered.length, pageCount = Math.ceil(storyCount / pageSize);
    for (let page = 1; page <= pageCount; page++) {
      const offset = (page - 1) * pageSize;
      pages.push({
        tag, page, pageCount, storyCount,
        stories:ordered.slice(offset, offset + pageSize),
        url:pageUrl(tag.slug, page),
        previousUrl:page === 1 ? null : pageUrl(tag.slug, page - 1),
        nextUrl:page === pageCount ? null : pageUrl(tag.slug, page + 1)
      });
    }
  }
  return pages;
}

export async function loadStoryArchive(dataDir = process.env.TWIHN_DATA_DIR || path.resolve('data'), {includeSourceBytes = false} = {}) {
  const {issues, revisions} = await loadArchive(dataDir);
  const registryPath = path.join(dataDir, 'tags.json');
  requireThat((await lstat(registryPath)).isFile(), 'tag registry path must be a regular file');
  // Validate from the same once-read bytes used by artifact export/parity checks.
  const registryBytes = await readFile(registryPath);
  const registry = validateRegistry(parseStrictJSON(registryBytes));
  const sourceBytes = includeSourceBytes ? {registry:registryBytes,records:new Map()} : undefined;
  const storiesDir = path.join(dataDir, 'stories');
  requireThat((await lstat(storiesDir)).isDirectory(), 'stories path must be a regular directory');
  const records = [];
  for (const entry of await readdir(storiesDir, {withFileTypes:true})) {
    // The negative lookahead anchors the absolute end, unlike $ before a newline.
    requireThat(entry.isFile() && /^[1-9][0-9]*\.json(?![\s\S])/u.test(entry.name), `unexpected story path: ${entry.name}`);
    const bytes = await readFile(path.join(storiesDir, entry.name));
    const record = validateStoryRecord(parseStrictJSON(bytes), registry);
    requireThat(entry.name === `${record.id}.json`, `filename/story ID mismatch: ${entry.name}`);
    records.push(record);
    if (sourceBytes) sourceBytes.records.set(record.id,bytes);
  }
  const stories = projectStories(issues, records, registry);
  const archive = {issues, revisions, registry, records, stories};
  return sourceBytes ? {...archive,sourceBytes} : archive;
}

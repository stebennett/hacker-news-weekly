import { readFile, readdir, lstat } from 'node:fs/promises';
import { loadArchive, validateIssue } from './content.mjs';
import path from 'node:path';
import Ajv from 'ajv';
import addFormats from 'ajv-formats';

const registrySchema = JSON.parse(await readFile(path.resolve('schema/tags.schema.json'), 'utf8'));
const storySchema = JSON.parse(await readFile(path.resolve('schema/story.schema.json'), 'utf8'));
const ajv = new Ajv({allErrors:true,strict:true});
addFormats(ajv);
const registryStructural = ajv.compile(registrySchema);
const storyStructural = ajv.compile(storySchema);
const labelStructural = ajv.compile(registrySchema.properties.tags.items.properties.label);
const storyIdStructural = ajv.compile(storySchema.properties.id);
const tagSlugsStructural = ajv.compile(storySchema.properties.tagSlugs);
const submittedAtStructural = ajv.compile({type:'string',format:'date-time',pattern:'Z$'});
const requireThat = (condition, message) => { if (!condition) throw new Error(message); };

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

export async function loadStoryArchive(dataDir = process.env.TWIHN_DATA_DIR || path.resolve('data')) {
  const {issues, revisions} = await loadArchive(dataDir);
  const registryPath = path.join(dataDir, 'tags.json');
  requireThat((await lstat(registryPath)).isFile(), 'tag registry path must be a regular file');
  const registry = validateRegistry(JSON.parse(await readFile(registryPath, 'utf8')));
  const storiesDir = path.join(dataDir, 'stories');
  requireThat((await lstat(storiesDir)).isDirectory(), 'stories path must be a regular directory');
  const records = [];
  for (const entry of await readdir(storiesDir, {withFileTypes:true})) {
    // The negative lookahead anchors the absolute end, unlike $ before a newline.
    requireThat(entry.isFile() && /^[1-9][0-9]*\.json(?![\s\S])/u.test(entry.name), `unexpected story path: ${entry.name}`);
    const record = validateStoryRecord(JSON.parse(await readFile(path.join(storiesDir, entry.name), 'utf8')), registry);
    requireThat(entry.name === `${record.id}.json`, `filename/story ID mismatch: ${entry.name}`);
    records.push(record);
  }
  const stories = projectStories(issues, records, registry);
  return {issues, revisions, registry, records, stories};
}

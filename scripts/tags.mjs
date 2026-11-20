import { readFile } from 'node:fs/promises';
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

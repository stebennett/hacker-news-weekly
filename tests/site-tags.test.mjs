import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import Ajv from 'ajv';
import addFormats from 'ajv-formats';
import * as tags from '../scripts/tags.mjs';

const registry = {schemaVersion:1,tags:[{slug:'ai',label:'AI'}]};

test('registry rejects mechanically duplicated labels', () => {
  assert.equal(typeof tags.validateRegistry, 'function');
  assert.throws(() => tags.validateRegistry({schemaVersion:1,tags:[
    ...registry.tags,{slug:'artificial-intelligence',label:'ai'}
  ]}), /duplicate/i);
});

const makeRegistry = entries => ({schemaVersion:1,tags:entries});
const record = () => ({schemaVersion:1,id:'12345678',tagSlugs:['ai'],issueIds:['2026-11-20']});
const multiple = makeRegistry([
  {slug:'ai',label:'AI'},
  {slug:'security',label:'Security'}
]);

test('label comparison uses NFC, whitespace collapse and lowercase without losing punctuation', () => {
  assert.equal(tags.normalizeLabel('  CAFE\u0301\u00a0  Research  '), 'café research');
  assert.deepEqual(['C','C++','C#'].map(tags.normalizeLabel), ['c','c++','c#']);
});

test('label comparison rejects non-text, controls, empty and oversized input before normalization', () => {
  for (const label of [null, undefined, 42, {}, [], '', '   ', 'a'.repeat(81), 'x\ny', 'x\u0085y']) {
    assert.throws(() => tags.normalizeLabel(label), /label/i);
  }
});

test('empty registry is valid', () => {
  const empty = makeRegistry([]);
  assert.equal(tags.validateRegistry(empty), empty);
});

test('multiple tags and punctuation-distinct labels preserve accepted label bytes', () => {
  const value = makeRegistry([
    {slug:'c',label:'C'}, {slug:'c-plus-plus',label:'C++'}, {slug:'c-sharp',label:'C#'},
    {slug:'cafe',label:'Café'}, {slug:'machine-learning',label:'Machine  Learning'},
    {slug:'security',label:'Security <&>'}
  ]);
  const before = structuredClone(value);
  assert.equal(tags.validateRegistry(value), value);
  assert.deepEqual(value, before);
});

test('registry rejects whitespace-equivalent labels under distinct slugs', () => {
  assert.throws(() => tags.validateRegistry(makeRegistry([
    {slug:'machine-learning',label:'Machine Learning'},
    {slug:'ml',label:'machine\u00a0  learning'}
  ])), /duplicate/i);
});

test('registry rejects invalid, unsafe and oversized slugs', () => {
  for (const slug of ['', '../ai', '/ai', 'ai/security', 'ai\\security', 'AI', ' ai', 'ai ',
    '-ai', 'ai-', 'ai--research', 'ai_research', 'a.b', 'a\n', 'a'.repeat(81), 1, null]) {
    assert.throws(() => tags.validateRegistry(makeRegistry([{slug,label:'AI'}])), `slug: ${JSON.stringify(slug)}`);
  }
});

test('registry rejects duplicate slugs including identical entries', () => {
  for (const label of ['AI', 'Different']) {
    assert.throws(() => tags.validateRegistry(makeRegistry([
      {slug:'ai',label:'AI'}, {slug:'ai',label}
    ])), /duplicate/i);
  }
});

test('registry rejects untrimmed, non-NFC, empty and invalid label types', () => {
  for (const label of ['', ' ', ' AI', 'AI ', '\u00a0AI', 'AI\u00a0', 'Cafe\u0301', 1, null, {}, []]) {
    assert.throws(() => tags.validateRegistry(makeRegistry([{slug:'ai',label}])), `label: ${JSON.stringify(label)}`);
  }
});

test('registry rejects all C0 and C1 controls in label text', () => {
  const codes = [...Array.from({length:32}, (_, i) => i), ...Array.from({length:33}, (_, i) => 127 + i)];
  for (const code of codes) {
    assert.throws(() => tags.validateRegistry(makeRegistry([{slug:'ai',label:`A${String.fromCodePoint(code)}I`}])), `control: ${code}`);
  }
});

test('registry enforces label and slug length boundaries without an arbitrary tag count cap', () => {
  const boundary = makeRegistry([{slug:'a'.repeat(80),label:'😀'.repeat(80)}]);
  assert.equal(tags.validateRegistry(boundary), boundary);
  assert.throws(() => tags.validateRegistry(makeRegistry([{slug:'ai',label:'😀'.repeat(81)}])));
  const many = makeRegistry(Array.from({length:200}, (_, i) => ({slug:`topic-${String(i).padStart(3,'0')}`,label:`Topic ${i}`})));
  assert.equal(tags.validateRegistry(many), many);
});

test('registry rejects malformed roots, missing fields, wrong versions and unknown fields', () => {
  for (const value of [null, [], {}, {tags:[]}, {schemaVersion:2,tags:[]}, {schemaVersion:'1',tags:[]},
    {schemaVersion:1,tags:{}}, {...registry,private:true}, makeRegistry([{}]),
    makeRegistry([{slug:'ai'}]), makeRegistry([{label:'AI'}]),
    makeRegistry([{slug:'ai',label:'AI',url:'https://example.com'}])]) {
    assert.throws(() => tags.validateRegistry(value));
  }
});

test('registry requires canonical slug order without silently sorting inputs', () => {
  const value = makeRegistry([...multiple.tags].reverse());
  const before = structuredClone(value);
  assert.throws(() => tags.validateRegistry(value), /sort|order/i);
  assert.deepEqual(value, before);
});

test('story accepts multiple registered tags, ascending issue IDs and literal large HN IDs', () => {
  const value = {...record(), id:'9007199254740993123456789', tagSlugs:['ai','security'], issueIds:['2026-11-13','2026-11-20']};
  const before = structuredClone(value);
  assert.equal(tags.validateStoryRecord(value, multiple), value);
  assert.deepEqual(value, before);
});

test('story rejects invalid literal HN IDs without coercing or trimming', () => {
  for (const id of ['', '0', '01', '+1', '-1', '1.0', '1e3', ' 1', '1 ', '1\n', '../1', '١', 1, null]) {
    assert.throws(() => tags.validateStoryRecord({...record(), id}, registry), `id: ${JSON.stringify(id)}`);
  }
});

test('story requires nonempty unique safe tag references', () => {
  for (const tagSlugs of [[], ['ai','ai'], ['../ai'], ['AI'], ['ai\n'], ['a'.repeat(81)], [1], 'ai', null]) {
    assert.throws(() => tags.validateStoryRecord({...record(), tagSlugs}, registry));
  }
});

test('story rejects dangling tag references and invalid registries', () => {
  assert.throws(() => tags.validateStoryRecord({...record(),tagSlugs:['security']}, registry), /unknown|reference/i);
  assert.throws(() => tags.validateStoryRecord(record(), makeRegistry([])), /unknown|reference/i);
  assert.throws(() => tags.validateStoryRecord(record(), {...registry,private:true}), /registry/i);
});

test('story requires nonempty unique valid date-only issue IDs', () => {
  for (const issueIds of [[], ['2026-11-20','2026-11-20'], ['2026-99-99'], ['2026-02-29'],
    ['2026-11-31'], ['2026-1-2'], ['2026-11-20T08:00:00Z'], ['../2026-11-20'], ['2026-11-20\n'], [20261120], '2026-11-20', null]) {
    assert.throws(() => tags.validateStoryRecord({...record(),issueIds}, registry));
  }
  assert.doesNotThrow(() => tags.validateStoryRecord({...record(),issueIds:['2024-02-29']}, registry));
});

test('story requires canonical tag and issue order without mutating inputs', () => {
  for (const value of [
    {...record(),tagSlugs:['security','ai']},
    {...record(),issueIds:['2026-11-20','2026-11-13']}
  ]) {
    const before = structuredClone(value);
    assert.throws(() => tags.validateStoryRecord(value, multiple), /sort|order/i);
    assert.deepEqual(value, before);
  }
});

test('story rejects malformed roots, missing fields, wrong versions and unknown fields', () => {
  const missing = Object.keys(record()).map(key => {
    const value = record(); delete value[key]; return value;
  });
  for (const value of [null, [], {}, ...missing, {...record(),schemaVersion:2},
    {...record(),schemaVersion:'1'}, {...record(),title:'Not duplicated here'}]) {
    assert.throws(() => tags.validateStoryRecord(value, registry));
  }
});

test('published schemas independently enforce structural constraints with AJV', async () => {
  const ajv = new Ajv({allErrors:true,strict:true}); addFormats(ajv);
  const registrySchema = JSON.parse(await readFile(new URL('../schema/tags.schema.json', import.meta.url), 'utf8'));
  const storySchema = JSON.parse(await readFile(new URL('../schema/story.schema.json', import.meta.url), 'utf8'));
  const checkRegistry = ajv.compile(registrySchema), checkStory = ajv.compile(storySchema);
  assert.equal(checkRegistry(registry), true);
  assert.equal(checkRegistry(makeRegistry([])), true);
  for (const value of [{...registry,private:true}, makeRegistry([{slug:'../ai',label:'AI'}]),
    makeRegistry([{slug:'ai',label:'bad\nlabel'}]), makeRegistry([{slug:'ai',label:'a'.repeat(81)}])]) {
    assert.equal(checkRegistry(value), false);
  }
  assert.equal(checkStory(record()), true);
  for (const value of [{...record(),id:'01'}, {...record(),id:'1\n'}, {...record(),private:true},
    {...record(),tagSlugs:[]}, {...record(),tagSlugs:['ai','ai']},
    {...record(),issueIds:[]}, {...record(),issueIds:['2026-11-20','2026-11-20']},
    {...record(),issueIds:['2026-02-29']}]) {
    assert.equal(checkStory(value), false);
  }
});

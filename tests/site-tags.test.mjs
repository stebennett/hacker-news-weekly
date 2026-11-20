import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile, writeFile, rename, rm, mkdir, symlink } from 'node:fs/promises';
import path from 'node:path';
import { fixture } from './site-fixtures.mjs';
import { recordsFor, taggedFixture, writeTagArchive } from './site-tag-fixtures.mjs';
import { stableJSON, validateIssue } from '../scripts/content.mjs';
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

test('each published ID needs exactly one independent tagged record', () => {
  assert.throws(() => tags.projectStories([fixture()], [], registry), /coverage.*missing \[1, 2\]; extra \[\]/i);
});

test('projection rejects extra records and duplicate record IDs', () => {
  const {issues,records} = taggedFixture();
  assert.throws(() => tags.projectStories(issues, [...records,{...records[0],id:'99'}], registry), /coverage.*missing \[\]; extra \[99\]/i);
  assert.throws(() => tags.projectStories(issues, [records[0],{...records[1],id:'99'}], registry), /coverage.*missing \[2\]; extra \[99\]/i);
  assert.throws(() => tags.projectStories(issues, [...records,records[0]], registry), /duplicate/i);
});

test('projection requires the exact current issue reference set', () => {
  const {issues,records} = taggedFixture();
  for (const issueIds of [['2026-11-06'], ['2026-11-06','2026-11-13']]) {
    assert.throws(() => tags.projectStories(issues, [{...records[0],issueIds},records[1]], registry), /issue.*reference|reference.*issue/i);
  }
});

test('projection validates its registry, records and current issues at the boundary', () => {
  const {issues,records} = taggedFixture();
  assert.throws(() => tags.projectStories(issues, [{...records[0],tagSlugs:['missing']},records[1]], registry), /unknown tag/i);
  assert.throws(() => tags.projectStories(issues, [{...records[0],title:'Duplicated facts'},records[1]], registry), /schema/i);
  assert.throws(() => tags.projectStories(issues, records, {...registry,private:true}), /registry/i);
  const invalid = fixture(); invalid.items['1'].submittedAt = invalid.window.end;
  assert.throws(() => tags.projectStories([invalid], records, registry), /submission outside window/i);
  assert.throws(() => tags.projectStories([issues[0],issues[0]], records, registry), /duplicate issue/i);
  for (const args of [[null,records,registry], [issues,null,registry]]) {
    assert.throws(() => tags.projectStories(...args), /array/i);
  }
});

test('projection rejects unused registry entries including on an empty site', () => {
  const {issues,records} = taggedFixture();
  assert.throws(() => tags.projectStories(issues, records, multiple), /unused/i);
  assert.throws(() => tags.projectStories([], [], registry), /unused/i);
  assert.deepEqual(tags.projectStories([], [], makeRegistry([])), []);
});

test('projection includes ordinary, Ask HN and Show HN stories without copying editable facts', () => {
  const issue = fixture();
  issue.items['3'] = {...structuredClone(issue.items['1']),id:'3',kind:'show',title:'Show HN: test only',
    discussionUrl:'https://news.ycombinator.com/item?id=3',sources:[{kind:'discussion',url:'https://news.ycombinator.com/item?id=3'}]};
  issue.sections[3] = {id:'show',itemIds:['3'],qualifyingCount:1};
  validateIssue(issue);
  const records = recordsFor([issue]);
  const before = stableJSON({issue,records,registry});
  const stories = tags.projectStories([issue], records, registry);
  assert.deepEqual(stories.map(story => story.item.id).sort(), ['1','2','3']);
  for (const story of stories) {
    assert.deepEqual(Object.keys(story).sort(), ['issueIds','item','sourceIssueId','tagSlugs']);
    assert.equal(story.item, issue.items[story.item.id]);
    assert.deepEqual(story.tagSlugs, ['ai']);
    assert.deepEqual(story.issueIds, [issue.issueId]);
    assert.equal(story.sourceIssueId, issue.issueId);
  }
  assert.equal(stableJSON({issue,records,registry}), before);
});

function crossRankingIssues() {
  const older = fixture();
  older.issueId = '2026-11-06'; older.scheduledAt = '2026-11-06T08:00:00Z';
  older.window = {start:'2026-10-30T08:00:00Z',end:older.scheduledAt};
  older.collectedAt = '2026-11-06T08:05:00Z';
  for (const item of Object.values(older.items)) item.submittedAt = '2026-11-03T12:00:00Z';
  const latest = fixture();
  // The same ID is in votes in the older issue and comments in the latest.
  // Both are valid current issues: no overlapping rankings or invalid windows.
  const voteIds = Array.from({length:10}, (_, i) => String(i + 3));
  for (const id of voteIds) latest.items[id] = {...structuredClone(latest.items['1']),id,
    discussionUrl:`https://news.ycombinator.com/item?id=${id}`,sources:[{kind:'discussion',url:`https://news.ycombinator.com/item?id=${id}`}]};
  latest.sections[0] = {id:'votes',itemIds:[...voteIds].sort((a,b) => b.localeCompare(a)),qualifyingCount:11};
  latest.sections[1] = {id:'comments',itemIds:['1'],qualifyingCount:11};
  latest.items['1'].story = 'Latest current reviewed story, test only.';
  latest.items['1'].score = 123;
  validateIssue(older); validateIssue(latest);
  return [older,latest];
}

test('duplicate ID across rankings is projected once with facts from the latest current issue', () => {
  const issues = crossRankingIssues(), records = recordsFor(issues);
  const before = stableJSON({issues,records});
  for (const input of [issues,[...issues].reverse()]) {
    const stories = tags.projectStories(input, records, registry);
    assert.equal(stories.length, Object.keys(issues[1].items).length);
    const matches = stories.filter(story => story.item.id === '1');
    assert.equal(matches.length, 1);
    assert.equal(matches[0].item, issues[1].items['1']);
    assert.equal(matches[0].sourceIssueId, '2026-11-13');
    assert.deepEqual(matches[0].issueIds, ['2026-11-06','2026-11-13']);
  }
  const missingReference = records.map(record => record.id === '1' ? {...record,issueIds:['2026-11-13']} : record);
  assert.throws(() => tags.projectStories(issues, missingReference, registry), /issue.*reference|reference.*issue/i);
  assert.equal(stableJSON({issues,records}), before);
});

test('story loader returns validated archive, registry, records and resolved stories', async t => {
  const archive = taggedFixture(), dataDir = await writeTagArchive(t, archive);
  const loaded = await tags.loadStoryArchive(dataDir);
  assert.deepEqual(Object.keys(loaded).sort(), ['issues','records','registry','revisions','stories']);
  assert.deepEqual(loaded.issues, archive.issues);
  assert.deepEqual(loaded.revisions, []);
  assert.deepEqual(loaded.registry, archive.registry);
  assert.deepEqual([...loaded.records].sort((a,b) => a.id.localeCompare(b.id)), archive.records);
  assert.deepEqual(loaded.stories.map(story => story.item.id).sort(), ['1','2']);
  assert.equal(loaded.stories.find(story => story.item.id === '1').item, loaded.issues[0].items['1']);
});

test('story loader honors TWIHN_DATA_DIR when no explicit directory is supplied', async t => {
  const dataDir = await writeTagArchive(t);
  const previous = process.env.TWIHN_DATA_DIR;
  t.after(() => { if (previous === undefined) delete process.env.TWIHN_DATA_DIR; else process.env.TWIHN_DATA_DIR = previous; });
  process.env.TWIHN_DATA_DIR = dataDir;
  assert.deepEqual(await tags.loadStoryArchive(), await tags.loadStoryArchive(dataDir));
});

test('story loader accepts an explicitly empty site', async t => {
  const dataDir = await writeTagArchive(t, {issues:[],revisions:[],registry:makeRegistry([]),records:[]});
  assert.deepEqual(await tags.loadStoryArchive(dataDir), {issues:[],revisions:[],registry:makeRegistry([]),records:[],stories:[]});
});

test('story loader has no fallback for missing registry, records or incomplete coverage', async t => {
  for (const target of ['tags.json','stories','stories/1.json']) {
    const dataDir = await writeTagArchive(t);
    await rm(path.join(dataDir, target), {recursive:true});
    await assert.rejects(tags.loadStoryArchive(dataDir), /ENOENT|missing|coverage/i);
  }
});

test('story loader rejects filename and record ID mismatch', async t => {
  const dataDir = await writeTagArchive(t);
  await rename(path.join(dataDir, 'stories/1.json'), path.join(dataDir, 'stories/99.json'));
  await assert.rejects(tags.loadStoryArchive(dataDir), /filename.*id|id.*mismatch/i);
});

test('story loader rejects unexpected story filenames and directories', async t => {
  for (const name of ['01.json','0.json','+1.json','1.JSON','1.json.bak','notes.txt','.gitkeep','3.json\n','3.json']) {
    const dataDir = await writeTagArchive(t);
    const target = path.join(dataDir, 'stories', name);
    if (name === '3.json') await mkdir(target); else await writeFile(target, '{}');
    await assert.rejects(tags.loadStoryArchive(dataDir), /unexpected|regular|story path/i, name);
  }
});

test('story loader rejects symlinked story records and stories directory', async t => {
  for (const directory of [false,true]) {
    const dataDir = await writeTagArchive(t);
    const target = path.join(dataDir, directory ? 'stories' : 'stories/1.json');
    const moved = `${target}-real`;
    await rename(target, moved);
    await symlink(moved, target);
    await assert.rejects(tags.loadStoryArchive(dataDir), /unexpected|regular|director|story path/i);
  }
});

test('story loader requires the exact registry path to be a regular file', async t => {
  for (const kind of ['symlink','directory']) {
    const dataDir = await writeTagArchive(t), target = path.join(dataDir, 'tags.json');
    await rename(target, `${target}-real`);
    if (kind === 'symlink') await symlink(`${target}-real`, target); else await mkdir(target);
    await assert.rejects(tags.loadStoryArchive(dataDir), /regular|registry|tag.*path/i);
  }
});

test('story loader validates parsed JSON and exact semantic coverage', async t => {
  for (const [name,value,expected] of [
    ['tags.json','not JSON', /JSON|Unexpected/i],
    ['stories/1.json','not JSON', /JSON|Unexpected/i],
    ['stories/1.json',stableJSON({...taggedFixture().records[0],tagSlugs:['dangling']}), /unknown tag/i],
    ['stories/99.json',stableJSON({...taggedFixture().records[0],id:'99'}), /extra|coverage/i],
    ['stories/1.json',stableJSON({...taggedFixture().records[0],issueIds:['2026-11-06']}), /issue.*reference|reference.*issue/i],
    ['tags.json',stableJSON(multiple), /unused/i]
  ]) {
    const dataDir = await writeTagArchive(t);
    await writeFile(path.join(dataDir, name), value);
    await assert.rejects(tags.loadStoryArchive(dataDir), expected);
  }
});

test('retained old revisions supply no extra stories or stale facts and preserve source bytes', async t => {
  const old = fixture();
  const current = structuredClone(old);
  current.revision = 2;
  current.correction = {previousRevision:1,correctedAt:'2026-11-14T10:00:00Z',note:'Synthetic correction, test only.'};
  current.items['1'].story = 'Corrected reviewed facts, test only.';
  delete current.items['2'];
  current.sections[2] = {id:'ask',itemIds:[],qualifyingCount:0};
  const dataDir = await writeTagArchive(t, {issues:[current],revisions:[old],registry,records:recordsFor([current])});
  const names = ['tags.json','stories/1.json',`issues/${current.issueId}.json`,`revisions/${old.issueId}/v1.json`];
  const before = await Promise.all(names.map(name => readFile(path.join(dataDir, name))));
  const loaded = await tags.loadStoryArchive(dataDir);
  assert.deepEqual(loaded.revisions, [old]);
  assert.equal(loaded.stories.length, 1);
  assert.equal(loaded.stories[0].item.story, current.items['1'].story);
  assert.deepEqual(loaded.stories[0].issueIds, [current.issueId]);
  assert.deepEqual(await Promise.all(names.map(name => readFile(path.join(dataDir, name)))), before);
  await rm(path.join(dataDir, `revisions/${old.issueId}/v1.json`));
  await assert.rejects(tags.loadStoryArchive(dataDir), /missing retained revision/i);
});

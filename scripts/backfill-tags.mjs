import { constants } from 'node:fs';
import { mkdir, readdir, lstat, open, link, unlink, rmdir } from 'node:fs/promises';
import { randomUUID } from 'node:crypto';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { TextDecoder } from 'node:util';
import { stableJSON } from './content.mjs';
import { validateTagging, validateTaggingInput, validateRegistry, validateStoryRecord,
  projectStories, loadStoryArchive, registrySha256, sha256 } from './tags.mjs';
import { prepareArchiveTaggingInput, loadMigrationArchive, readPrivateJSONBytes,
  noSymlinks, checkDataTree, readFrozenSidecar, writeCandidate, atomicPublicJSON, maxCandidateBytes } from './tagging.mjs';

const repository = fileURLToPath(new URL('..',import.meta.url));
const requireThat = (condition,message) => { if (!condition) throw new Error(message); };
const same = (a,b) => stableJSON(a) === stableJSON(b);
const within = (child,parent) => child === parent || child.startsWith(`${parent}${path.sep}`);
const progressName = n => `progress-${String(n).padStart(6,'0')}.json`;
const maxArchiveBytes = 128 * 1024 * 1024;

// JSON.parse alone silently overwrites duplicate assignment keys. Validate JSON
// syntax first, then inspect object member tokens (including escaped key names).
function parse(bytes) {
  let text, value;
  try { text = new TextDecoder('utf-8',{fatal:true}).decode(bytes); value = JSON.parse(text); }
  catch { throw new Error('Invalid UTF-8 JSON'); }
  const stack = [], token = /"(?:[^"\\]|\\[\s\S])*"|[{}\[\]]/gu;
  for (const match of text.matchAll(token)) {
    const t = match[0];
    if (t === '{') stack.push(new Set());
    else if (t === '[') stack.push(null);
    else if (t === '}' || t === ']') stack.pop();
    else if (/^\s*:/u.test(text.slice(match.index+t.length))) {
      const key = JSON.parse(t), seen = stack.at(-1);
      requireThat(seen && !seen.has(key),'Duplicate JSON member'); seen.add(key);
    }
  }
  return value;
}
async function syncDirectory(directory) {
  const handle = await open(directory,constants.O_RDONLY | constants.O_DIRECTORY | constants.O_NOFOLLOW);
  try { await handle.sync(); } finally { await handle.close(); }
}
async function immutableJSON(work,name,value,dataDir) {
  const temporary = path.join(work,`.backfill-${randomUUID()}.tmp`);
  try {
    await writeCandidate(temporary,value,[dataDir]);
    await link(temporary,path.join(work,name));
    await unlink(temporary); await syncDirectory(work);
  } finally { await unlink(temporary).catch(error => { if (error.code !== 'ENOENT') throw error; }); }
}
async function privateWork(directory,dataDir,create = false) {
  const work = path.resolve(directory);
  requireThat(![path.resolve(repository),path.resolve(dataDir)].some(parent => within(work,parent)), 'Work must be outside repository and archive');
  await noSymlinks(path.dirname(work),true);
  if (create) {
    try { await mkdir(work,{mode:0o700}); await syncDirectory(path.dirname(work)); }
    catch (error) { if (error.code !== 'EEXIST') throw error; }
  }
  await noSymlinks(work,true);
  const info = await lstat(work);
  requireThat(info.uid === process.getuid() && (info.mode & 0o7777) === 0o700,'Work must be owned mode 0700');
  return work;
}
async function withLock(work,callback) {
  const lock = path.join(work,'.lock');
  await mkdir(lock,{mode:0o700});
  try { return await callback(); } finally { await rmdir(lock); }
}
async function sourceSnapshot(dataDir) {
  await checkDataTree(dataDir,true);
  const files = {}, directories = [];
  async function visit(dir) {
    directories.push(path.relative(dataDir,dir));
    for (const name of (await readdir(dir)).sort()) {
      const file = path.join(dir,name), info = await lstat(file);
      if (info.isDirectory()) await visit(file);
      else files[path.relative(dataDir,file)] = sha256(await readPrivateJSONBytes(file));
    }
  }
  await visit(dataDir);
  return {directories:directories.sort(),files};
}
function registryWith(registry,additions) {
  return validateRegistry({schemaVersion:1,tags:[...registry.tags,...additions].sort((a,b) => a.slug < b.slug ? -1 : a.slug > b.slug ? 1 : 0)});
}
function pending(snapshot,registry,assignments) {
  const stories = (snapshot.input?.stories ?? []).filter(story => !Object.hasOwn(assignments,story.id)).slice(0,snapshot.batchSize);
  return stories.length ? validateTaggingInput({...snapshot.input,registry,registrySha256:registrySha256(registry),stories}) : null;
}
function validateResponse(input,bytes) {
  const response = parse(bytes);
  requireThat(response && !Array.isArray(response) && Object.keys(response).length === 2 && Object.hasOwn(response,'assignments') && response.inputSha256 === sha256(stableJSON(input)), 'Response must bind the exact pending input');
  return validateTagging(input,{assignments:response.assignments});
}
async function readArtifact(filename,dataDir) {
  const value = await readFrozenSidecar(filename,dataDir);
  requireThat((await readPrivateJSONBytes(filename)).equals(Buffer.from(stableJSON(value))), 'Immutable artifact canonical bytes changed');
  return value;
}
async function readSession(work) {
  const snapshot = await readArtifact(path.join(work,'input.json'),repository);
  requireThat(snapshot.schemaVersion === 1 && Number.isSafeInteger(snapshot.batchSize) && snapshot.batchSize >= 1 && snapshot.batchSize <= 1000 && typeof snapshot.dataDir === 'string','Invalid backfill input');
  requireThat(!within(work,path.resolve(snapshot.dataDir)),'Work must be outside archive');
  validateRegistry(snapshot.registry);
  if (snapshot.input) {
    validateTaggingInput(snapshot.input);
    requireThat(same(snapshot.registry,snapshot.input.registry),'Input registry mismatch');
  }
  const assignments = {};
  for (const record of snapshot.records) {
    validateStoryRecord(record,snapshot.registry);
    requireThat(!Object.hasOwn(assignments,record.id),'Duplicate existing record');
    assignments[record.id] = record.tagSlugs;
  }
  let registry = snapshot.registry, input = pending(snapshot,registry,assignments);
  const names = (await readdir(work)).filter(name => name !== '.lock');
  requireThat(names.every(name => name === 'input.json' || /^progress-\d{6,}\.json$/u.test(name)), 'Unexpected backfill work entry; inspect interrupted work');
  const count = names.length-1;
  requireThat(count >= 1,'Missing initial progress; inspect interrupted prepare');
  let previousSha256 = sha256(stableJSON(snapshot));
  const acceptedDigests = new Set();
  for (let n = 0; n < count; n++) {
    const checkpoint = await readArtifact(path.join(work,progressName(n)),snapshot.dataDir);
    let accepted = null;
    if (n > 0) {
      requireThat(input && checkpoint.accepted && typeof checkpoint.accepted.responseText === 'string','Invalid accepted progress');
      const bytes = Buffer.from(checkpoint.accepted.responseText);
      const candidate = validateResponse(input,bytes);
      accepted = {responseSha256:sha256(bytes),responseText:bytes.toString(),candidate};
      requireThat(!acceptedDigests.has(accepted.responseSha256),'Duplicate accepted response');
      acceptedDigests.add(accepted.responseSha256);
      Object.assign(assignments,candidate.assignments);
      registry = registryWith(registry,candidate.additions);
      input = pending(snapshot,registry,assignments);
    }
    const expected = {schemaVersion:1,previousSha256,accepted,input};
    requireThat(same(checkpoint,expected),'Immutable progress does not match validated input/results');
    previousSha256 = sha256(stableJSON(checkpoint));
  }
  return {snapshot,assignments,registry,input,previousSha256,acceptedDigests,count};
}
async function prepare(o) {
  const dataDir = await noSymlinks(o['--data-dir'],true), batchSize = Number(o['--batch-size'] ?? '50');
  requireThat(/^[1-9][0-9]*$/u.test(o['--batch-size'] ?? '50') && Number.isSafeInteger(batchSize) && batchSize <= 1000,'Batch size must be 1–1000');
  const work = await privateWork(o['--work-dir'],dataDir,true);
  await withLock(work,async () => {
    const names = (await readdir(work)).filter(name => name !== '.lock');
    if (names.length) {
      const session = await readSession(work);
      requireThat(session.snapshot.dataDir === dataDir && session.snapshot.batchSize === batchSize && same(session.snapshot.source,await sourceSnapshot(dataDir)),'Prepared source or batch size changed');
      return;
    }
    const source = await sourceSnapshot(dataDir), archive = await loadMigrationArchive(dataDir);
    const issues = [...archive.issues].sort((a,b) => a.issueId.localeCompare(b.issueId));
    const input = issues.length ? prepareArchiveTaggingInput(archive,issues.at(-1).issueId) : null;
    const snapshot = {schemaVersion:1,dataDir,batchSize,source,registry:archive.registry,records:archive.records,input};
    requireThat(same(source,await sourceSnapshot(dataDir)),'Archive changed during prepare');
    // Write-once files only. A crash orphan is refused for human inspection,
    // never interpreted as valid progress or overwritten by a hidden reset.
    await immutableJSON(work,'input.json',snapshot,dataDir);
    const assignments = Object.fromEntries(archive.records.map(r => [r.id,r.tagSlugs]));
    await immutableJSON(work,progressName(0),{schemaVersion:1,previousSha256:sha256(stableJSON(snapshot)),accepted:null,input:pending(snapshot,archive.registry,assignments)},dataDir);
  });
}
async function accept(o) {
  const work = await privateWork(o['--work-dir'],repository);
  await withLock(work,async () => {
    const s = await readSession(work), bytes = await readPrivateJSONBytes(o['--response']);
    if (s.acceptedDigests.has(sha256(bytes))) return;
    requireThat(s.input,'No pending batch');
    const candidate = validateResponse(s.input,bytes);
    const registry = registryWith(s.registry,candidate.additions), assignments = {...s.assignments,...candidate.assignments};
    const checkpoint = {schemaVersion:1,previousSha256:s.previousSha256,accepted:{responseSha256:sha256(bytes),responseText:bytes.toString('utf8'),candidate},input:pending(s.snapshot,registry,assignments)};
    await immutableJSON(work,progressName(s.count),checkpoint,s.snapshot.dataDir);
  });
}
async function apply(o) {
  const dataDir = await noSymlinks(o['--data-dir'],true), work = await privateWork(o['--work-dir'],dataDir);
  await withLock(work,async () => {
    const s = await readSession(work);
    requireThat(s.snapshot.dataDir === dataDir && s.input === null,'Backfill source differs or coverage is incomplete');
    const before = await sourceSnapshot(dataDir), archive = await loadMigrationArchive(dataDir);
    const records = Object.entries(s.assignments).map(([id,tagSlugs]) => ({schemaVersion:1,id,tagSlugs,issueIds:archive.issues.filter(issue => Object.hasOwn(issue.items,id)).map(issue => issue.issueId).sort()}));
    projectStories(archive.issues,records,s.registry);
    const known = new Map(s.snapshot.records.map(r => [r.id,r]));
    const expected = structuredClone(s.snapshot.source), changes = [];
    if (!expected.directories.includes('stories')) expected.directories.push('stories');
    expected.directories.sort();
    for (const record of records) {
      const relative = `stories/${record.id}.json`;
      if (known.has(record.id)) { requireThat(same(record,known.get(record.id)),'Known assignments/references changed'); continue; }
      const bytes = stableJSON(record); expected.files[relative] = sha256(bytes); changes.push({relative,bytes});
    }
    if (!same(s.registry,s.snapshot.registry) || !Object.hasOwn(expected.files,'tags.json')) {
      const bytes = stableJSON(s.registry); expected.files['tags.json'] = sha256(bytes); changes.push({relative:'tags.json',bytes});
    }
    requireThat(same(before,s.snapshot.source) || same(before,expected),'Exact source digests changed or prior apply is partial');
    if (same(before,expected)) { await loadStoryArchive(dataDir); return; }
    // Validate byte budgets and the entire projection before the first write.
    let totalBytes = (await checkDataTree(dataDir,true)).totalBytes;
    for (const change of changes) {
      requireThat(Buffer.byteLength(change.bytes) <= maxCandidateBytes,'Public JSON byte limit');
      const prior = Object.hasOwn(before.files,change.relative) ? await readPrivateJSONBytes(path.join(dataDir,change.relative)) : null;
      change.previous = prior; totalBytes += Buffer.byteLength(change.bytes) - (prior?.length ?? 0);
    }
    requireThat(totalBytes <= maxArchiveBytes,'Public archive byte limit');
    requireThat(same(before,await sourceSnapshot(dataDir)),'Archive changed during preflight');
    if (!before.directories.includes('stories')) { await mkdir(path.join(dataDir,'stories'),{mode:0o755}); await syncDirectory(dataDir); }
    // Not a multi-file transaction: an interrupted apply is detected and refused.
    // Restore/inspect the checkout rather than auto-merging partial public state.
    for (const change of changes) await atomicPublicJSON(path.join(dataDir,change.relative),change.bytes,change.previous);
    requireThat(same(expected,await sourceSnapshot(dataDir)),'Applied archive differs from expected exact bytes');
    await loadStoryArchive(dataDir);
  });
}
function argumentsFor(argv) {
  const [command,...args] = argv;
  const required = command === 'prepare' || command === 'apply' ? ['--data-dir','--work-dir'] : command === 'accept' ? ['--work-dir','--response'] : [];
  requireThat(required.length,'Usage: backfill-tags.mjs prepare|accept|apply [options]');
  const allowed = [...required,...(command === 'prepare' ? ['--batch-size'] : [])], options = {};
  for (let n = 0; n < args.length; n += 2) {
    const key = args[n], value = args[n+1];
    requireThat(allowed.includes(key) && !Object.hasOwn(options,key) && typeof value === 'string' && value.length && !value.startsWith('--'),'Unknown, duplicate or missing backfill argument');
    options[key] = value;
  }
  requireThat(required.every(key => options[key]),'Missing backfill arguments');
  return {command,options};
}
export async function main(argv = process.argv.slice(2)) {
  const {command,options} = argumentsFor(argv);
  await ({prepare,accept,apply}[command])(options);
}
if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  main().catch(() => {
    console.error('Backfill failed; inspect private progress and source checkout before retrying.');
    process.exitCode = 1;
  });
}

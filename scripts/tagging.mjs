import { constants } from 'node:fs';
import { open, lstat, readdir, rename, link, unlink } from 'node:fs/promises';
import { randomUUID } from 'node:crypto';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { TextDecoder } from 'node:util';
import { HEADINGS, stableJSON, loadArchive, validateIssue } from './content.mjs';
import { loadStoryArchive, validateRegistry, validateStoryRecord, validateTagging,
  validateTaggingInput, validateTaggingIssueId, sha256, registrySha256, projectStories, applyTagging } from './tags.mjs';

// Bounded local JSON only; this module never calls a model or the network.
export const maxCandidateBytes = 16 * 1024 * 1024;
const maxArchiveBytes = 128 * 1024 * 1024;
const repository = path.resolve(fileURLToPath(new URL('..', import.meta.url)));
const requireThat = (condition, message) => { if (!condition) throw new Error(message); };
const isObject = value => value !== null && typeof value === 'object' && !Array.isArray(value);
function keys(value, required, optional = []) {
  requireThat(isObject(value) && required.every(key => Object.hasOwn(value,key)) && Object.keys(value).every(key => required.includes(key) || optional.includes(key)), 'Invalid reviewed fields');
}
const numericId = id => typeof id === 'string' && /^[1-9][0-9]*(?![\s\S])/u.test(id);
const idOrder = (a,b) => BigInt(a.id) < BigInt(b.id) ? -1 : BigInt(a.id) > BigInt(b.id) ? 1 : 0;

function parseJSON(bytes) {
  // JSON.parse's diagnostic may contain input bytes; never pass it to CLI logs.
  try { return JSON.parse(new TextDecoder('utf-8', {fatal:true}).decode(bytes)); }
  catch { throw new Error('Invalid UTF-8 JSON'); }
}
function reviewStories(reviewed, issueId) {
  keys(reviewed, ['start','end','sections']);
  const start = Date.parse(reviewed.start), end = Date.parse(reviewed.end);
  requireThat(typeof reviewed.start === 'string' && typeof reviewed.end === 'string' && Number.isFinite(start) && Number.isFinite(end) && end-start === 7*86400000, 'Invalid reviewed window');
  const local = Object.fromEntries(new Intl.DateTimeFormat('en-CA', {timeZone:'Europe/London',year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit',hourCycle:'h23'}).formatToParts(new Date(end)).map(part => [part.type,part.value]));
  requireThat(`${local.year}-${local.month}-${local.day}` === issueId && `${local.hour}:${local.minute}:${local.second}` === '08:00:00' && end % 1000 === 0, 'reviewed window does not match intended issue');
  requireThat(Array.isArray(reviewed.sections) && reviewed.sections.length === 4, 'four reviewed sections required');
  const unique = new Map();
  for (const [index, section] of reviewed.sections.entries()) {
    keys(section, ['heading','items']);
    requireThat(section.heading === HEADINGS[index] && Array.isArray(section.items), 'Invalid reviewed section');
    const inSection = new Set();
    for (const item of section.items) {
      keys(item, ['id','title','score','comments','created','url','story','discussion'], ['tags']);
      requireThat(numericId(item.id) && !inSection.has(item.id), 'Invalid or duplicate literal reviewed ID'); inSection.add(item.id);
      requireThat(['title','story','discussion'].every(key => typeof item[key] === 'string' && item[key].trim().length > 0), 'reviewed text required');
      requireThat(['score','comments'].every(key => Number.isSafeInteger(item[key]) && item[key] >= 0), 'Invalid reviewed metrics');
      const created = typeof item.created === 'string' ? Date.parse(item.created) : NaN;
      requireThat(Number.isFinite(created) && created >= start && created < end, 'Invalid reviewed submission time');
      requireThat(typeof item.url === 'string', 'Invalid reviewed URL');
      if (item.url) {
        let url; try { url = new URL(item.url); } catch { throw new Error('Invalid reviewed URL'); }
        requireThat(['http:','https:'].includes(url.protocol) && url.hostname && !url.username && !url.password && !/[\s\x00-\x1f\\]/u.test(item.url), 'Invalid reviewed URL');
      }
      requireThat(item.tags === undefined || Array.isArray(item.tags) && item.tags.every(tag => typeof tag === 'string'), 'Invalid collector tags');
      requireThat(!unique.has(item.id) || unique.get(item.id).index < 2 && index < 2 && stableJSON(unique.get(item.id).item) === stableJSON(item), 'Inconsistent duplicate reviewed story');
      unique.set(item.id, {item,index});
    }
  }
  return [...unique.values()].map(({item}) => item);
}
function envelope(items, registry, records, issueId, reviewedBytes) {
  const previous = new Map(records.map(record => [record.id,record.tagSlugs]));
  return validateTaggingInput({schemaVersion:1,issueId:validateTaggingIssueId(issueId),reviewedSha256:sha256(reviewedBytes),registrySha256:registrySha256(registry),registry:structuredClone(registry),
    stories:[...items].sort(idOrder).map(item => ({id:item.id,title:item.title,story:item.story,discussion:item.discussion,existingTagSlugs:[...(previous.get(item.id) ?? [])]}))});
}

export function prepareTaggingInput(reviewedBytes, archive, issueId) {
  validateTaggingIssueId(issueId);
  return envelope(reviewStories(parseJSON(reviewedBytes), issueId), archive.registry, archive.records, issueId, reviewedBytes);
}
export function prepareArchiveTaggingInput(archive, issueId) {
  const issues = [...archive.issues].sort((a,b) => a.issueId.localeCompare(b.issueId));
  const items = new Map();
  for (const issue of issues) for (const item of Object.values(issue.items)) items.set(item.id,item);
  // Archive migration has no single legacy review. Bind the complete canonical
  // array of current issues (ascending issue ID), never superseded revisions.
  return envelope([...items.values()], archive.registry, archive.records, issueId, stableJSON(issues));
}

async function noSymlinks(target, directory = false) {
  const absolute = path.resolve(target), root = path.parse(absolute).root;
  let current = root;
  for (const component of absolute.slice(root.length).split(path.sep).filter(Boolean)) {
    current = path.join(current, component);
    const info = await lstat(current);
    requireThat(!info.isSymbolicLink(), 'Symlink path is not allowed');
    requireThat(current === absolute && !directory ? info.isFile() : info.isDirectory(), 'Path must be a regular file or directory');
  }
  return absolute;
}
export async function readPrivateJSONBytes(filename) {
  const absolute = await noSymlinks(filename);
  const handle = await open(absolute, constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK);
  try {
    const info = await handle.stat();
    requireThat(info.isFile() && info.size <= maxCandidateBytes, 'JSON file exceeds size limit or is not regular');
    const buffer = Buffer.alloc(Math.min(info.size + 1, maxCandidateBytes + 1));
    let offset = 0;
    while (offset < buffer.length) {
      const {bytesRead} = await handle.read(buffer,offset,buffer.length-offset,null);
      if (!bytesRead) break;
      offset += bytesRead;
    }
    const after = await handle.stat();
    requireThat(offset === info.size && info.size === after.size && info.mtimeMs === after.mtimeMs && info.ctimeMs === after.ctimeMs, 'JSON file changed while reading');
    return buffer.subarray(0,offset);
  } finally { await handle.close(); }
}
export async function checkDataTree(dataDir, publicApplication = false) {
  await noSymlinks(dataDir, true);
  let total = 0;
  const snapshot = [];
  async function visit(dir) {
    const directory = await lstat(dir);
    if (publicApplication) {
      requireThat(directory.uid === process.getuid() && (directory.mode & 0o022) === 0, 'Public archive directories must be owned and not group/world writable');
      snapshot.push([dir,directory.ino,directory.mode,directory.mtimeMs,directory.ctimeMs]);
    }
    for (const entry of (await readdir(dir, {withFileTypes:true})).sort((a,b) => a.name < b.name ? -1 : a.name > b.name ? 1 : 0)) {
      const filename = path.join(dir,entry.name), info = await lstat(filename);
      requireThat(!info.isSymbolicLink(), 'Symlink archive path is not allowed');
      if (publicApplication && dir === dataDir) {
        requireThat(['issues','revisions','stories','tags.json'].includes(entry.name), 'Unexpected public archive root entry');
        requireThat(entry.name === 'tags.json' ? info.isFile() : info.isDirectory(), 'Invalid public archive root entry type');
      }
      if (publicApplication && entry.name === '.gitkeep') requireThat(info.isFile(), 'Archive placeholder must be a regular file');
      if (info.isDirectory()) await visit(filename);
      else {
        requireThat(info.isFile() && info.size <= maxCandidateBytes, 'Archive file exceeds size limit or is not regular');
        if (publicApplication) {
          requireThat(info.uid === process.getuid() && (info.mode & 0o7777) === 0o644 && info.nlink === 1, 'Public archive files must be owned regular unaliased mode 0644');
          snapshot.push([filename,info.ino,info.mode,info.nlink,info.size,info.mtimeMs,info.ctimeMs]);
        }
        total += info.size;
        requireThat(total <= maxArchiveBytes, 'Archive exceeds byte limit');
      }
    }
  }
  await visit(dataDir);
  return {entries:snapshot,totalBytes:total};
}
async function exists(filename) {
  try { await lstat(filename); return true; } catch (error) { if (error.code === 'ENOENT') return false; throw error; }
}
async function loadMigrationArchive(dataDir) {
  const {issues,revisions} = await loadArchive(dataDir);
  const registryPath = path.join(dataDir,'tags.json');
  const registry = await exists(registryPath) ? validateRegistry(parseJSON(await readPrivateJSONBytes(registryPath))) : {schemaVersion:1,tags:[]};
  const records = [], storiesDir = path.join(dataDir,'stories');
  if (await exists(storiesDir)) {
    await noSymlinks(storiesDir,true);
    for (const entry of await readdir(storiesDir, {withFileTypes:true})) {
      requireThat(entry.isFile() && /^[1-9][0-9]*\.json(?![\s\S])/u.test(entry.name), 'Unexpected migration story path');
      const record = validateStoryRecord(parseJSON(await readPrivateJSONBytes(path.join(storiesDir,entry.name))), registry);
      requireThat(entry.name === `${record.id}.json`, 'Migration story filename/ID mismatch');
      const references = issues.filter(issue => Object.hasOwn(issue.items,record.id)).map(issue => issue.issueId).sort();
      requireThat(stableJSON(references) === stableJSON(record.issueIds), 'Migration story references do not match current issues');
      records.push(record);
    }
  }
  return {issues,revisions,registry,records};
}
const within = (target, parent) => target === parent || target.startsWith(`${parent}${path.sep}`);
export async function writeCandidate(filename, value, forbiddenDirs = []) {
  const target = path.resolve(filename), parent = path.dirname(target);
  // Private artifacts are not source files, even if a directory is gitignored.
  requireThat(![repository,...forbiddenDirs.map(dir => path.resolve(dir))].some(dir => within(target,dir)), 'Candidate must be outside repository and public data directories');
  await noSymlinks(parent,true);
  const info = await lstat(parent);
  requireThat(info.uid === process.getuid() && (info.mode & 0o077) === 0, 'Candidate parent must be a private owned directory (0700)');
  const bytes = stableJSON(value);
  requireThat(Buffer.byteLength(bytes) <= maxCandidateBytes, 'Candidate exceeds byte limit');
  const handle = await open(target, constants.O_WRONLY | constants.O_CREAT | constants.O_EXCL | constants.O_NOFOLLOW, 0o600);
  try { await handle.chmod(0o600); await handle.writeFile(bytes); await handle.sync(); }
  finally { await handle.close(); }
}

export async function readFrozenSidecar(filename, dataDir) {
  const absolute = path.resolve(filename);
  requireThat(![repository,dataDir].some(dir => within(absolute,dir)), 'Sidecar must be outside repository and public archive');
  await noSymlinks(absolute);
  const parent = await lstat(path.dirname(absolute)), info = await lstat(absolute);
  requireThat(parent.uid === process.getuid() && (parent.mode & 0o077) === 0 && info.uid === process.getuid() && (info.mode & 0o7777) === 0o600 && info.nlink === 1, 'Frozen sidecar must be private and owned (0700/0600)');
  return parseJSON(await readPrivateJSONBytes(absolute));
}

async function loadApplicationArchive(dataDir, newIssue) {
  const {issues,revisions} = await loadArchive(dataDir);
  const imported = issues.find(issue => issue.issueId === newIssue.issueId);
  requireThat(imported && stableJSON(imported) === stableJSON(newIssue), 'Apply requires the exact already-imported current issue');
  const registry = validateRegistry(parseJSON(await readPrivateJSONBytes(path.join(dataDir,'tags.json'))));
  const records = [], storiesDir = await noSymlinks(path.join(dataDir,'stories'),true);
  for (const entry of await readdir(storiesDir,{withFileTypes:true})) {
    requireThat(entry.isFile() && /^[1-9][0-9]*\.json(?![\s\S])/u.test(entry.name), 'Unexpected application story path');
    const record = validateStoryRecord(parseJSON(await readPrivateJSONBytes(path.join(storiesDir,entry.name))),registry);
    requireThat(entry.name === `${record.id}.json`, 'Application story filename/ID mismatch');
    records.push(record);
  }
  // Only this CLI boundary can exclude the exact pending imported issue. A
  // partial prior write fails BOTH projections and is never silently repaired.
  let baseIssues = issues;
  try { projectStories(issues,records,registry); }
  catch {
    baseIssues = issues.filter(issue => issue.issueId !== newIssue.issueId);
    projectStories(baseIssues,records,registry);
  }
  return {issues:baseIssues,revisions,registry,records};
}

async function atomicPublicJSON(filename, bytes, previous) {
  const parent = await noSymlinks(path.dirname(filename),true);
  const temporary = path.join(parent,`.tagging-${randomUUID()}.tmp`);
  try {
    const handle = await open(temporary,constants.O_WRONLY | constants.O_CREAT | constants.O_EXCL | constants.O_NOFOLLOW,0o644);
    try { await handle.chmod(0o644); await handle.writeFile(bytes); await handle.sync(); }
    finally { await handle.close(); }
    await noSymlinks(parent,true);
    if (previous === null) {
      // Atomic create-if-absent, not rename-overwrite of a concurrent new record.
      await link(temporary,filename); await unlink(temporary);
    } else {
      requireThat((await readPrivateJSONBytes(filename)).equals(previous), 'Public target changed during application');
      await rename(temporary,filename);
    }
    const directory = await open(parent,constants.O_RDONLY | constants.O_DIRECTORY | constants.O_NOFOLLOW);
    try { await directory.sync(); } finally { await directory.close(); }
  } finally {
    await unlink(temporary).catch(error => { if (error.code !== 'ENOENT') throw error; });
  }
}

async function applyFrozenTagging(sidecarPath, issuePath, directory) {
  const dataDir = await noSymlinks(directory,true);
  const frozen = await readFrozenSidecar(sidecarPath,dataDir);
  const before = await checkDataTree(dataDir,true);
  const issue = validateIssue(parseJSON(await readPrivateJSONBytes(issuePath)));
  const archive = await loadApplicationArchive(dataDir,issue);
  const next = applyTagging(archive,frozen,issue);
  // The entire next projection is valid before even staging a write. Preserve
  // untouched historical bytes (including noncanonical whitespace), not merely
  // their JSON meaning. Only changed registry/records receive canonical bytes.
  const previousRecords = new Map(archive.records.map(record => [record.id,record]));
  const changes = [];
  for (const record of next.records) {
    const previous = previousRecords.get(record.id);
    if (previous && stableJSON(previous) === stableJSON(record)) continue;
    const filename = path.join(dataDir,'stories',`${record.id}.json`);
    changes.push({filename,bytes:stableJSON(record),previous:previous ? await readPrivateJSONBytes(filename) : null});
  }
  if (stableJSON(archive.registry) !== stableJSON(next.registry)) {
    const filename = path.join(dataDir,'tags.json');
    changes.push({filename,bytes:stableJSON(next.registry),previous:await readPrivateJSONBytes(filename)});
  }
  let nextBytes = before.totalBytes;
  for (const change of changes) {
    const size = Buffer.byteLength(change.bytes);
    requireThat(size <= maxCandidateBytes, 'Computed public JSON exceeds byte limit');
    nextBytes += size - (change.previous?.length ?? 0);
  }
  requireThat(nextBytes <= maxArchiveBytes, 'Computed public archive exceeds byte limit');
  requireThat(stableJSON(before) === stableJSON(await checkDataTree(dataDir,true)), 'Archive changed during application preflight');
  // This is intentionally not a multi-file transaction. The caller owns its
  // checkout; interrupted writes leave an invalid projection or crash temporary
  // file that a retry refuses, not an apparently successful partial migration.
  for (const change of changes) await atomicPublicJSON(change.filename,change.bytes,change.previous);
  await checkDataTree(dataDir,true);
  await loadStoryArchive(dataDir);
}

function argumentsFor(argv) {
  const [command,...rest] = argv;
  const allowed = command === 'input' ? ['--archive','--reviewed','--data-dir','--issue','--output'] : command === 'validate' ? ['--data-dir','--input','--response','--output'] : command === 'apply' ? ['--sidecar','--issue','--data-dir'] : [];
  requireThat(allowed.length > 0, 'Usage: node scripts/tagging.mjs input|validate|apply [options]');
  const options = {};
  for (let n = 0; n < rest.length; n++) {
    const key = rest[n];
    requireThat(allowed.includes(key) && !Object.hasOwn(options,key), 'Unknown or duplicate tagging argument');
    if (key === '--archive') options[key] = true;
    else {
      requireThat(typeof rest[n+1] === 'string' && !rest[n+1].startsWith('--'), 'Missing tagging argument value');
      options[key] = rest[++n];
    }
  }
  const required = command === 'input' ? ['--data-dir','--issue','--output'] : command === 'validate' ? ['--data-dir','--input','--response','--output'] : ['--sidecar','--issue','--data-dir'];
  requireThat(required.every(key => options[key]), 'Missing required tagging arguments');
  if (command === 'input') requireThat(Boolean(options['--archive']) !== Boolean(options['--reviewed']), 'Use exactly one of --reviewed or --archive');
  return {command,options};
}
export async function main(argv = process.argv.slice(2)) {
  const {command,options:o} = argumentsFor(argv);
  if (command === 'input') {
    validateTaggingIssueId(o['--issue']);
    await checkDataTree(o['--data-dir']);
    const archive = o['--archive'] ? await loadMigrationArchive(o['--data-dir']) : await loadStoryArchive(o['--data-dir']);
    const input = o['--archive'] ? prepareArchiveTaggingInput(archive,o['--issue']) : prepareTaggingInput(await readPrivateJSONBytes(o['--reviewed']),archive,o['--issue']);
    await writeCandidate(o['--output'],input,[o['--data-dir']]);
  } else if (command === 'apply') {
    await applyFrozenTagging(o['--sidecar'],o['--issue'],o['--data-dir']);
  } else {
    const dataDir = await noSymlinks(o['--data-dir'],true);
    const input = parseJSON(await readPrivateJSONBytes(o['--input']));
    const response = parseJSON(await readPrivateJSONBytes(o['--response']));
    await writeCandidate(o['--output'],validateTagging(input,response),[dataDir]);
  }
}
if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  main().catch(error => {
    // Do not echo parser, schema, path or model diagnostics: they may include
    // private input bytes. Detailed semantic errors remain available to callers.
    const limit = /limit|exceeds/iu.test(error.message) ? ' (byte limit)' : '';
    console.error(process.argv[2] === 'apply'
      ? `Tagging application failed${limit}; inspect or restore the checkout before retrying.`
      : `Tagging failed${limit}; no valid candidate was prepared.`);
    process.exitCode = 1;
  });
}

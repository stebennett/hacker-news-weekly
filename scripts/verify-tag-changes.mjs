import { execFileSync } from 'node:child_process';
import { mkdtemp, mkdir, writeFile, rm, lstat } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { stableJSON, validateIssue } from './content.mjs';
import { loadStoryArchive, applyTagging } from './tags.mjs';
import { checkDataTree, readFrozenSidecar, readPrivateJSONBytes, parsePrivateJSONBytes, maxCandidateBytes } from './tagging.mjs';

// Local Git objects only. No refs are fetched or resolved from the network, and
// no general database/history API is exposed to content callers.
const maxArchiveBytes = 128 * 1024 * 1024;
const requireThat = (condition, message) => { if (!condition) throw new Error(message); };
const splitZ = bytes => bytes.toString('utf8').split('\0').filter(Boolean);
function localGit(repository, ...args) {
  return execFileSync('git',['--no-replace-objects','-c','protocol.allow=never',...args],{cwd:repository,
    timeout:15000,maxBuffer:20*1024*1024,stdio:['ignore','pipe','pipe'],
    env:{PATH:process.env.PATH,HOME:process.env.HOME,GIT_CONFIG_NOSYSTEM:'1',GIT_CONFIG_GLOBAL:'/dev/null',GIT_TERMINAL_PROMPT:'0',GIT_NO_LAZY_FETCH:'1'}});
}
async function materializeBase(repository, base, destination) {
  const entries = splitZ(localGit(repository,'ls-tree','-rz','--full-tree',base,'--','data'));
  const files = new Map(); let total = 0;
  for (const entry of entries) {
    const match = /^100644 blob ([a-f0-9]{40})\t(data\/[^\0]+)$/u.exec(entry);
    requireThat(match, 'Base data must contain only regular mode 0644 blobs');
    const [,blob,relative] = match;
    requireThat(!relative.includes('\\') && relative.split('/').every(part => part && part !== '.' && part !== '..'), 'Invalid base data path');
    const bytes = localGit(repository,'cat-file','blob',blob);
    requireThat(bytes.length <= maxCandidateBytes, 'Base file exceeds byte limit');
    total += bytes.length; requireThat(total <= maxArchiveBytes, 'Base archive exceeds byte limit');
    files.set(relative,bytes);
    const filename = path.join(destination,relative);
    await mkdir(path.dirname(filename),{recursive:true});
    await writeFile(filename,bytes,{mode:0o644});
  }
  for (const directory of ['issues','revisions','stories']) await mkdir(path.join(destination,'data',directory),{recursive:true});
  return files;
}
async function actualData(repository) {
  const root = path.join(repository,'data'), before = await checkDataTree(root,true), files = new Map();
  for (const entry of before.entries) {
    if (entry.length !== 7) continue; // Directory snapshots have five fields.
    files.set(path.relative(repository,entry[0]).split(path.sep).join('/'),await readPrivateJSONBytes(entry[0]));
  }
  requireThat(stableJSON(before) === stableJSON(await checkDataTree(root,true)), 'Archive changed during diff validation');
  return files;
}
async function changedPaths(repository,base,issue,idempotent) {
  const raw = splitZ(localGit(repository,'diff','--no-ext-diff','--no-renames','--name-status','-z',base,'--'));
  requireThat(raw.length % 2 === 0, 'Invalid changed-path readback');
  const paths = new Set();
  for (let n=0;n<raw.length;n+=2) {
    requireThat(['A','M'].includes(raw[n]), 'Deletion/rename/nonregular change refused');
    paths.add(raw[n+1]);
  }
  for (const name of splitZ(localGit(repository,'ls-files','--others','--exclude-standard','-z'))) paths.add(name);
  if (idempotent) requireThat(paths.size === 0, 'Idempotent published checkout must be unchanged');
  else requireThat(paths.has(`data/issues/${issue.issueId}.json`) && [...paths].some(name=>name.startsWith('dist/')), 'Changes must include current issue and generated dist');
  const ids = new Set(Object.keys(issue.items));
  for (const name of paths) {
    const story = /^data\/stories\/([1-9][0-9]*)\.json(?![\s\S])/u.exec(name);
    requireThat(name.split('/').every(part=>part && part!=='.' && part!=='..') && !name.includes('\\') &&
      (name === `data/issues/${issue.issueId}.json` || name === 'data/tags.json' || story && ids.has(story[1]) || name.startsWith('dist/')), 'Unauthorized non-content or unjustified story path');
    // Validate worktree modes as well as Git's executable/symlink bits: Git
    // alone cannot distinguish a public 0600 file from the required 0644 mode.
    await readPrivateJSONBytes(path.join(repository,name));
    const info = await lstat(path.join(repository,name));
    requireThat(info.isFile() && (info.mode & 0o7777) === 0o644 && info.nlink === 1, 'Changed content must be regular unaliased mode 0644');
    if ((name.startsWith('dist/data/issues/') || name.startsWith('dist/data/revisions/')) && name !== `dist/data/issues/${issue.issueId}.json`) {
      requireThat(localGit(repository,'ls-tree',base,'--',name).length === 0, 'Historic generated issue/revision changed');
    }
  }
}

export async function verifyTagChanges({repository = process.cwd(),base,sidecar,issuePath,idempotent = false}) {
  requireThat(typeof base === 'string' && /^[a-f0-9]{40}(?![\s\S])/u.test(base), 'Explicit exact base SHA required');
  repository = path.resolve(repository);
  requireThat(localGit(repository,'rev-parse','--show-toplevel').toString().trim() === repository, 'Exact repository root required');
  requireThat(localGit(repository,'rev-parse','HEAD').toString().trim() === base, 'Stale base: checkout HEAD differs from validated base');
  const issueBytes = await readPrivateJSONBytes(issuePath), issue = validateIssue(parsePrivateJSONBytes(issueBytes));
  const frozen = await readFrozenSidecar(sidecar,path.join(repository,'data'));
  const temporary = await mkdtemp(path.join(tmpdir(),'twihn-tag-base-'));
  try {
    const expectedFiles = await materializeBase(repository,base,temporary);
    const baseData = path.join(temporary,'data');
    await checkDataTree(baseData,true);
    const archive = await loadStoryArchive(baseData);
    const canonical = `data/issues/${issue.issueId}.json`;
    requireThat(expectedFiles.has(canonical) === idempotent, 'Current issue must be new; existing publication requires explicit idempotent validation');
    const next = applyTagging(archive,frozen,issue);
    // Preserve the exact original bytes of EVERY unchanged file. Changed
    // records/registry alone use Task 6 canonical serialization.
    if (!idempotent) expectedFiles.set(canonical,issueBytes);
    const oldRecords = new Map(archive.records.map(record=>[record.id,record]));
    for (const record of next.records) {
      const previous = oldRecords.get(record.id);
      if (!previous || stableJSON(previous) !== stableJSON(record)) expectedFiles.set(`data/stories/${record.id}.json`,Buffer.from(stableJSON(record)));
    }
    if (stableJSON(archive.registry) !== stableJSON(next.registry)) expectedFiles.set('data/tags.json',Buffer.from(stableJSON(next.registry)));
    await changedPaths(repository,base,issue,idempotent);
    const actual = await actualData(repository);
    requireThat(actual.size === expectedFiles.size && [...actual.keys()].every(name=>expectedFiles.has(name)), 'Expected complete record/history file set differs');
    for (const [name,bytes] of expectedFiles) requireThat(actual.get(name)?.equals(bytes), 'Expected registry/record/imported issue/history bytes differ');
    await loadStoryArchive(path.join(repository,'data'));
    requireThat(localGit(repository,'rev-parse','HEAD').toString().trim() === base, 'Base changed during validation');
  } finally { await rm(temporary,{recursive:true,force:true}); }
}

export async function main(argv = process.argv.slice(2)) {
  const options = {}, names = {'--repository':'repository','--base':'base','--sidecar':'sidecar','--issue':'issuePath','--idempotent':'idempotent'};
  for (let n=0;n<argv.length;n++) {
    const name = Object.hasOwn(names,argv[n]) ? names[argv[n]] : undefined;
    requireThat(name && !Object.hasOwn(options,name), 'Unknown or duplicate diff argument');
    if (name === 'idempotent') options[name] = true;
    else { requireThat(argv[n+1] && !argv[n+1].startsWith('--'), 'Missing diff argument'); options[name] = argv[++n]; }
  }
  requireThat(['base','sidecar','issuePath'].every(name=>options[name]), 'Missing required diff argument');
  await verifyTagChanges(options);
}
if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  main().catch(()=>{ console.error('Tag content diff validation failed; no publication authorized.'); process.exitCode = 1; });
}

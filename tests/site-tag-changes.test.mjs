import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, mkdir, cp, writeFile, readFile, chmod, symlink, rm, rename } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { execFileSync, spawnSync } from 'node:child_process';
import childProcess from 'node:child_process';
import { syncBuiltinESMExports } from 'node:module';
import { stableJSON } from '../scripts/content.mjs';
import { applyTagging, registrySha256, sha256 } from '../scripts/tags.mjs';
import { taggedFixture, writeTagArchive } from './site-tag-fixtures.mjs';

const moduleURL = new URL('../scripts/verify-tag-changes.mjs', import.meta.url);
const git = (root, ...args) => execFileSync('git', args, {cwd:root,encoding:'utf8',timeout:15000});
async function setup(t, {additions = true, existing = false} = {}) {
  const root = await mkdtemp(path.join(tmpdir(),'twihn-tag-changes-'));
  t.after(() => rm(root,{recursive:true,force:true}));
  const repo = path.join(root,'checkout'), privateDir = path.join(root,'private');
  await mkdir(repo); await mkdir(privateDir,{mode:0o700});
  const archive = taggedFixture(), previous = structuredClone(archive.issues[0]);
  archive.issues[0].revision = 2;
  archive.issues[0].correction = {previousRevision:1,correctedAt:'2026-11-14T08:00:00Z',note:'Retained correction.'};
  archive.revisions.push(previous);
  const dataDir = path.join(repo,'data'); await cp(await writeTagArchive(t,archive),dataDir,{recursive:true});
  // Preserve even deliberately noncanonical unchanged historical bytes.
  await writeFile(path.join(dataDir,'stories/2.json'),` ${JSON.stringify(archive.records[1])}\n\n`);
  await writeFile(path.join(dataDir,'tags.json'),JSON.stringify(archive.registry));
  await writeFile(path.join(repo,'.gitignore'),'dist/\n');
  await writeFile(path.join(repo,'source.mjs'),'// human source\n');
  const issue = structuredClone(previous);
  issue.issueId = '2026-11-20'; issue.scheduledAt = '2026-11-20T08:00:00Z'; issue.collectedAt = issue.scheduledAt;
  issue.window = {start:'2026-11-13T08:00:00Z',end:issue.scheduledAt};
  issue.items = Object.fromEntries(Object.values(issue.items).map(item => {
    const id = item.id === '2' ? '3' : item.id;
    return [id,{...item,id,submittedAt:'2026-11-17T12:00:00Z',discussionUrl:`https://news.ycombinator.com/item?id=${id}`,sources:[{kind:'discussion',url:`https://news.ycombinator.com/item?id=${id}`}]}];
  }));
  issue.sections.forEach(section => { section.itemIds = section.itemIds.map(id => id === '2' ? '3' : id); });
  const frozen = {schemaVersion:1,issueId:issue.issueId,reviewedSha256:sha256('actual fixture review'),registrySha256:registrySha256(archive.registry),registry:archive.registry,
    assignments:{'1':['ai'],'3':[additions ? 'security' : 'ai']},additions:additions ? [{slug:'security',label:'Security'}] : []};
  const next = applyTagging(archive,frozen,issue), sidecar = path.join(privateDir,'tagging.json'), issuePath = path.join(privateDir,'canonical.json');
  await writeFile(sidecar,stableJSON(frozen),{mode:0o600}); await writeFile(issuePath,stableJSON(issue));
  const apply = async () => {
    await writeFile(path.join(dataDir,'issues',`${issue.issueId}.json`),stableJSON(issue));
    if (stableJSON(next.registry) !== stableJSON(archive.registry)) await writeFile(path.join(dataDir,'tags.json'),stableJSON(next.registry));
    for (const record of next.records) {
      const old = archive.records.find(value => value.id === record.id);
      if (!old || stableJSON(old) !== stableJSON(record)) await writeFile(path.join(dataDir,'stories',`${record.id}.json`),stableJSON(record));
    }
  };
  if (existing) await apply();
  git(repo,'init','--quiet'); git(repo,'add','-A');
  git(repo,'-c','user.name=Test','-c','user.email=test@example.invalid','commit','--quiet','-m','base');
  const base = git(repo,'rev-parse','HEAD').trim();
  if (!existing) await apply();
  assert.equal(git(repo,'ls-files','dist').trim(),'');
  const options = {repository:repo,base,sidecar,issuePath,idempotent:existing};
  return {root,repo,dataDir,archive,issue,frozen,next,sidecar,issuePath,options};
}
async function verify(s) {
  const module = await import(moduleURL);
  assert.equal(typeof module.verifyTagChanges,'function');
  return module.verifyTagChanges(s.options);
}
async function replaceJSON(s, relative, mutate) {
  const filename=path.join(s.repo,relative), value=JSON.parse(await readFile(filename,'utf8'));
  mutate(value); await writeFile(filename,stableJSON(value));
}

test('authorize complete expected registry/record changes, retaining exact historical bytes', async t => {
  const s=await setup(t), before=await readFile(path.join(s.dataDir,'stories/2.json'));
  await verify(s);
  assert.deepEqual(await readFile(path.join(s.dataDir,'stories/2.json')),before);
});
test('new records may reuse known labels with no registry diff', async t => {
  const s=await setup(t,{additions:false});
  const before=await readFile(path.join(s.dataDir,'tags.json')); await verify(s);
  assert.deepEqual(await readFile(path.join(s.dataDir,'tags.json')),before);
  assert.equal(git(s.repo,'diff','--name-only').includes('data/tags.json'),false);
});
test('already-published compatible tags validate read-only even when no records change', async t => {
  const s=await setup(t,{existing:true}); await verify(s);
});
test('Git object validation forbids lazy fetching and all network protocols', async t => {
  const s=await setup(t), original=childProcess.execFileSync;
  childProcess.execFileSync=(command,args,options)=>{
    assert.equal(command,'git');
    assert.equal(options.env.GIT_NO_LAZY_FETCH,'1');
    assert.ok(args.includes('protocol.allow=never'));
    return original(command,args,options);
  };
  syncBuiltinESMExports();
  try { await verify(s); }
  finally { childProcess.execFileSync=original; syncBuiltinESMExports(); }
});
for (const [name,mutate,pattern] of [
  ['old label edit',s=>replaceJSON(s,'data/tags.json',v=>{v.tags[0].label='Reinterpreted';}),/registry|expected/i],
  ['old label remove',s=>replaceJSON(s,'data/tags.json',v=>{v.tags.shift();}),/registry|reference/i],
  ['unrelated story create',s=>writeFile(path.join(s.dataDir,'stories/99.json'),stableJSON({schemaVersion:1,id:'99',tagSlugs:['ai'],issueIds:[s.issue.issueId]})),/unjustified|coverage|expected/i],
  ['existing assignment edit',s=>replaceJSON(s,'data/stories/1.json',v=>{v.tagSlugs=['security'];}),/expected|assignment/i],
  ['unrelated assignment edit',s=>replaceJSON(s,'data/stories/2.json',v=>{v.tagSlugs=['security'];}),/expected|unjustified|histor/i],
  ['orphan reference',s=>replaceJSON(s,'data/stories/1.json',v=>{v.issueIds.push('2026-11-27');}),/expected|reference/i],
  ['reference removal',s=>replaceJSON(s,'data/stories/1.json',v=>{v.issueIds.shift();}),/expected|reference/i],
  ['unknown record field',s=>replaceJSON(s,'data/stories/3.json',v=>{v.private='PRIVATE';}),/schema|expected/i],
  ['history whitespace rewrite',s=>replaceJSON(s,'data/stories/2.json',()=>{}),/bytes|histor|expected|unjustified/i],
  ['historic issue edit',s=>replaceJSON(s,'data/issues/2026-11-13.json',v=>{v.items['1'].story='Changed';}),/historic|unauthorized|non-content/i],
  ['retained revision edit',s=>replaceJSON(s,'data/revisions/2026-11-13/v1.json',v=>{v.items['1'].story='Changed';}),/historic|unauthorized|non-content/i],
  ['staged generated issue',async s=>{await mkdir(path.join(s.repo,'dist/data/issues'),{recursive:true});await writeFile(path.join(s.repo,'dist/data/issues/2026-11-13.json'),'{}');git(s.repo,'add','-f','dist');},/unauthorized|non-content/i],
  ['source path',s=>writeFile(path.join(s.repo,'source.mjs'),'changed'),/unauthorized|non-content/i],
  ['new source path',s=>writeFile(path.join(s.repo,'extra.mjs'),'new source'),/unauthorized|non-content/i],
  ['delete',s=>rm(path.join(s.dataDir,'stories/2.json')),/delet|missing|expected/i],
  ['rename',s=>rename(path.join(s.dataDir,'stories/2.json'),path.join(s.dataDir,'stories/99.json')),/delet|rename|unjustified|filename|expected/i],
  ['symlink',async s=>{const file=path.join(s.dataDir,'stories/3.json');await rm(file);await symlink(s.issuePath,file);},/symlink|regular/i],
  ['executable mode',s=>chmod(path.join(s.dataDir,'stories/3.json'),0o755),/mode|regular/i],
  ['private source mode',s=>chmod(path.join(s.dataDir,'stories/2.json'),0o600),/mode|regular/i],
  ['staged generated symlink',async s=>{await mkdir(path.join(s.repo,'dist'),{recursive:true});await symlink(s.issuePath,path.join(s.repo,'dist/index.html'));git(s.repo,'add','-f','dist');},/unauthorized|non-content/i],
  ['stale base',async s=>{git(s.repo,'add','-A');git(s.repo,'-c','user.name=Test','-c','user.email=test@example.invalid','commit','--quiet','-m','moved');},/base|HEAD/i],
  ['imported issue tamper',s=>replaceJSON(s,`data/issues/${s.issue.issueId}.json`,v=>{v.collectedAt='2026-11-20T09:00:00Z';}),/imported|issue|expected/i],
]) test(`record authorization rejects ${name}`,async t=>{
  const s=await setup(t); await mutate(s);
  // Import and assert the validator before the rejection assertion.
  const module=await import(moduleURL); assert.equal(typeof module.verifyTagChanges,'function');
  await assert.rejects(module.verifyTagChanges(s.options),pattern);
});

test('full historical base must already have complete tagging; weekly is not a backfill', async t => {
  const s=await setup(t); git(s.repo,'reset','--hard',s.options.base);
  await rm(path.join(s.dataDir,'stories/3.json'));
  await rm(path.join(s.dataDir,'stories/2.json')); git(s.repo,'add','-A');
  git(s.repo,'-c','user.name=Test','-c','user.email=test@example.invalid','commit','--quiet','-m','incomplete base');
  s.options.base=git(s.repo,'rev-parse','HEAD').trim();
  await writeFile(path.join(s.dataDir,'issues',`${s.issue.issueId}.json`),stableJSON(s.issue));
  for(const record of s.next.records) await writeFile(path.join(s.dataDir,'stories',`${record.id}.json`),stableJSON(record));
  await writeFile(path.join(s.dataDir,'tags.json'),stableJSON(s.next.registry));
  const module=await import(moduleURL); assert.equal(typeof module.verifyTagChanges,'function');
  await assert.rejects(module.verifyTagChanges(s.options),/coverage|base/i);
});
test('idempotent path rejects published assignments incompatible with frozen tags', async t => {
  const s=await setup(t,{existing:true}); s.frozen.assignments['1']=['security'];
  await writeFile(s.sidecar,stableJSON(s.frozen));
  const module=await import(moduleURL); assert.equal(typeof module.verifyTagChanges,'function');
  await assert.rejects(module.verifyTagChanges(s.options),/assignment/i);
});
for (const escaped of [false,true]) for (const boundary of ['sidecar root','sidecar assignment','sidecar registry','issue item']) {
  test(`CLI verifier rejects duplicate private JSON ${boundary} (${escaped ? 'escaped-equivalent' : 'ordinary'}) without authorizing publication`, async t => {
    const s=await setup(t), args=['scripts/verify-tag-changes.mjs','--repository',s.repo,'--base',s.options.base,'--sidecar',s.sidecar,'--issue',s.issuePath];
    const cli=()=>spawnSync(process.execPath,args,{encoding:'utf8',timeout:15000});
    const module=await import(moduleURL); assert.equal(typeof module.verifyTagChanges,'function');
    await module.verifyTagChanges(s.options);
    assert.equal(cli().status,0,'valid control authorizes the exact fixture');
    const target=boundary === 'issue item' ? s.issuePath : s.sidecar;
    const [key,first] = {
      'sidecar root':['assignments',s.frozen.assignments], 'sidecar assignment':['3',['ai']],
      'sidecar registry':['label','PRIVATE_SENTINEL discarded label'], 'issue item':['title','PRIVATE_SENTINEL discarded title'],
    }[boundary];
    const raw=JSON.stringify(boundary === 'issue item' ? s.issue : s.frozen), token=JSON.stringify(key);
    const next=escaped ? `"\\u${key.charCodeAt(0).toString(16).padStart(4,'0')}${key.slice(1)}"` : token;
    assert.ok(raw.includes(`${token}:`));
    await writeFile(target,raw.replace(`${token}:`,`${token}:${JSON.stringify(first)},${next}:`));
    // Match the supplied issue bytes to the actual new imported file so a
    // byte-parity guard cannot accidentally mask permissive JSON parsing.
    if (boundary === 'issue item') await writeFile(path.join(s.dataDir,'issues',`${s.issue.issueId}.json`),await readFile(target));
    const before=git(s.repo,'diff','--binary',s.options.base), sidecarBytes=await readFile(s.sidecar), issueBytes=await readFile(s.issuePath);
    const result=cli(); assert.equal(result.status,1,result.stderr); assert.equal(result.stdout,'');
    assert.equal(result.stderr,'Tag content diff validation failed; no publication authorized.\n');
    await assert.rejects(module.verifyTagChanges(s.options),/^SyntaxError: Duplicate JSON property name$/);
    assert.equal(git(s.repo,'diff','--binary',s.options.base),before);
    assert.deepEqual(await readFile(s.sidecar),sidecarBytes); assert.deepEqual(await readFile(s.issuePath),issueBytes);
  });
}

test('CLI verifier accepts BOM and escaped nonduplicate private JSON without rewriting the frozen bytes', async t => {
  const s=await setup(t);
  const sidecarBytes=Buffer.from('\ufeff'+JSON.stringify(s.frozen).replace('"assignments":','"\\u0061ssignments":').replace('"3":','"\\u0033":'));
  // Public issue files remain JSON-only; private sidecars retain BOM support.
  const issueBytes=Buffer.from(JSON.stringify(s.issue).replace('"items":','"\\u0069tems":'));
  await writeFile(s.sidecar,sidecarBytes); await writeFile(s.issuePath,issueBytes);
  await writeFile(path.join(s.dataDir,'issues',`${s.issue.issueId}.json`),issueBytes);
  const result=spawnSync(process.execPath,['scripts/verify-tag-changes.mjs','--repository',s.repo,'--base',s.options.base,'--sidecar',s.sidecar,'--issue',s.issuePath],{encoding:'utf8',timeout:15000});
  assert.equal(result.status,0,result.stderr); assert.equal(result.stdout,'');
  assert.deepEqual(await readFile(s.sidecar),sidecarBytes); assert.deepEqual(await readFile(s.issuePath),issueBytes);
});

test('ignored local build output is allowed, but any force-staged generated path is forbidden', async t => {
  const s=await setup(t); await mkdir(path.join(s.repo,'dist'));
  await writeFile(path.join(s.repo,'dist/index.html'),'locally built output');
  await verify(s);
  git(s.repo,'add','-f','dist/index.html');
  await assert.rejects(verify(s),/unauthorized|non-content/i);
});

test('missing canonical issue cannot be authorized by registry and story changes alone', async t => {
  const s=await setup(t); await rm(path.join(s.dataDir,'issues',`${s.issue.issueId}.json`));
  await assert.rejects(verify(s),/include current issue/i);
});

test('CLI uses explicit local exact base, has strict arguments and sanitized diagnostics', async t => {
  const s=await setup(t), args=['scripts/verify-tag-changes.mjs','--repository',s.repo,'--base',s.options.base,'--sidecar',s.sidecar,'--issue',s.issuePath];
  const cli=(argv)=>spawnSync(process.execPath,argv,{encoding:'utf8',timeout:15000});
  const ok=cli(args); assert.equal(ok.status,0,ok.stderr);assert.equal(ok.stdout,'');
  for(const extra of [['--base','main'],['--unknown','PRIVATE'],['--sidecar',s.sidecar],['__proto__','PRIVATE'],['constructor','PRIVATE']]) {
    const result=cli([...args,...extra]); assert.equal(result.status,1);assert.equal(result.stdout,'');assert.doesNotMatch(result.stderr,/PRIVATE/);
  }
  await writeFile(s.sidecar,'{PRIVATE'); const result=cli(args);
  assert.equal(result.status,1);assert.doesNotMatch(result.stderr,/PRIVATE/);
});

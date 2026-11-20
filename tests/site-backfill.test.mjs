import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, mkdir, writeFile, readFile, readdir, lstat, rm, chmod, symlink, link } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { spawnSync } from 'node:child_process';
import { stableJSON, validateIssue } from '../scripts/content.mjs';
import { sha256, loadStoryArchive } from '../scripts/tags.mjs';
import { taggedFixture, writeTagArchive } from './site-tag-fixtures.mjs';

async function tree(root) {
  const result = {};
  async function walk(dir) {
    for (const name of (await readdir(dir)).sort()) {
      const file = path.join(dir,name), s = await lstat(file), key = path.relative(root,file);
      result[key] = {mode:s.mode & 0o777,ino:s.ino,mtime:s.mtimeMs};
      if (s.isDirectory()) await walk(file);
      else if (s.isFile()) result[key].bytes = (await readFile(file)).toString('base64');
    }
  }
  await walk(root); return result;
}
function cli(...args) {
  return spawnSync(process.execPath,['scripts/backfill-tags.mjs',...args],{encoding:'utf8',timeout:15000});
}
function ok(result) { assert.equal(result.status,0,result.stderr || result.error?.message); }
async function setup(t, {known = false, empty = false} = {}) {
  const root = await mkdtemp(path.join(tmpdir(),'twihn-backfill-'));
  t.after(() => rm(root,{recursive:true,force:true}));
  const work = path.join(root,'private'), archive = taggedFixture();
  if (!known) { archive.records = []; archive.registry.tags = []; }
  else archive.records = archive.records.slice(0,1);
  if (empty) { archive.issues = []; archive.records = []; archive.registry.tags = []; }
  if (!empty) {
    const old = structuredClone(archive.issues[0]);
    // Retain an ID present ONLY in a superseded revision, not an extra input.
    old.items['9'] = {...old.items['2'],id:'9',discussionUrl:'https://news.ycombinator.com/item?id=9',sources:[{kind:'discussion',url:'https://news.ycombinator.com/item?id=9'}]};
    delete old.items['2']; old.sections = old.sections.map(s => ({...s,itemIds:s.itemIds.map(id => id === '2' ? '9' : id)}));
    archive.revisions = [old]; archive.issues[0].revision = 2;
    archive.issues[0].correction = {previousRevision:1,correctedAt:'2026-11-14T08:00:00Z',note:'Reviewed correction.'};
  }
  const data = await writeTagArchive(t,archive);
  for (const issue of archive.issues) await writeFile(path.join(data,'issues',`${issue.issueId}.json`),` ${JSON.stringify(issue)}\n\n`);
  for (const issue of archive.revisions) await writeFile(path.join(data,'revisions',issue.issueId,'v1.json'),`\n${JSON.stringify(issue)}\n`);
  const prepare = ['prepare','--data-dir',data,'--work-dir',work,'--batch-size','1'];
  return {root,work,data,archive,prepare};
}
async function progress(s) {
  const names = (await readdir(s.work)).filter(n => /^progress-\d{6}\.json$/.test(n)).sort();
  return JSON.parse(await readFile(path.join(s.work,names.at(-1)),'utf8'));
}
async function response(s, label = 'Security', filename = 'response.json') {
  const p = await progress(s), input = p.input;
  assert.ok(input,'a pending batch exists');
  const value = {inputSha256:sha256(stableJSON(input)),assignments:Object.fromEntries(input.stories.map(story => [story.id,[label]]))};
  const file = path.join(s.root,filename); await writeFile(file,stableJSON(value),{mode:0o600});
  return {file,value,input};
}
async function accept(s, label, filename) {
  const r = await response(s,label,filename); ok(cli('accept','--work-dir',s.work,'--response',r.file)); return r;
}
const applyArgs = s => ['apply','--data-dir',s.data,'--work-dir',s.work];
const withoutStories = issue => validateIssue({...issue,items:{},sections:issue.sections.map(section => ({...section,itemIds:[],qualifyingCount:0}))});

test('prepare freezes unique CURRENT inputs, actual canonical archive digest, exact source digests and private modes; repeat is read-only', async t => {
  const s = await setup(t), before = await tree(s.data); ok(cli(...s.prepare));
  const snapshot = JSON.parse(await readFile(path.join(s.work,'input.json'),'utf8'));
  assert.deepEqual(snapshot.input.stories.map(x => x.id),['1','2']);
  assert.equal(snapshot.input.reviewedSha256,sha256(stableJSON(s.archive.issues)));
  assert.ok(!JSON.stringify(snapshot.input).includes('"9"'));
  assert.deepEqual((await progress(s)).input.stories.map(x => x.id),['1']);
  assert.equal((await lstat(s.work)).mode & 0o777,0o700);
  for (const name of await readdir(s.work)) assert.equal((await lstat(path.join(s.work,name))).mode & 0o777,0o600);
  const privateBefore = await tree(s.work); ok(cli(...s.prepare));
  assert.deepEqual(await tree(s.work),privateBefore); assert.deepEqual(await tree(s.data),before);
});

test('accepted batches are immutable, later inputs see prior labels, identical response retries never consume next batch', async t => {
  const s = await setup(t); ok(cli(...s.prepare)); const first = await accept(s,'Security','first.json');
  const before = await tree(s.work), pending = await progress(s);
  assert.deepEqual(pending.input.registry.tags,[{slug:'security',label:'Security'}]);
  assert.deepEqual(pending.input.stories.map(x => x.id),['2']);
  ok(cli('accept','--work-dir',s.work,'--response',first.file)); assert.deepEqual(await tree(s.work),before);
  await writeFile(first.file,stableJSON({...first.value,assignments:{'1':['Databases']}}));
  assert.equal(cli('accept','--work-dir',s.work,'--response',first.file).status,1); assert.deepEqual(await tree(s.work),before);
  await accept(s,'Security','second.json'); const after = await tree(s.work);
  for (const [name,value] of Object.entries(before)) assert.deepEqual(after[name],value,name);
  assert.equal((await progress(s)).input,null);
  await writeFile(first.file,stableJSON(first.value));
  ok(cli('accept','--work-dir',s.work,'--response',first.file)); assert.deepEqual(await tree(s.work),after,'old accepted digest remains read-only after later acceptance');
});

test('partial backfill cannot apply or pass the strict production loader', async t => {
  const s = await setup(t); ok(cli(...s.prepare)); await accept(s,'Security'); const before = await tree(s.data);
  assert.equal(cli(...applyArgs(s)).status,1); assert.deepEqual(await tree(s.data),before);
  await assert.rejects(loadStoryArchive(s.data),/coverage/);
});

test('complete apply preserves every issue/revision byte, writes only tags/records 0644 and retries without public/private writes', async t => {
  const s = await setup(t); ok(cli(...s.prepare)); await accept(s,'Security','first.json'); await accept(s,'Security','second.json');
  const before = await tree(s.data), privateBefore = await tree(s.work); ok(cli(...applyArgs(s)));
  const after = await tree(s.data);
  for (const name of Object.keys(before).filter(n => /^(issues|revisions)(\/|$)/.test(n))) assert.deepEqual(after[name],before[name],name);
  const archive = await loadStoryArchive(s.data); assert.deepEqual(archive.records.map(r => r.id).sort(),['1','2']);
  for (const name of ['tags.json','stories/1.json','stories/2.json']) assert.equal(after[name].mode,0o644);
  ok(cli(...applyArgs(s))); assert.deepEqual(await tree(s.data),after); assert.deepEqual(await tree(s.work),privateBefore);
});

test('known partial migration assignments are reused without retagging or rewriting their bytes', async t => {
  const s = await setup(t,{known:true}), known = path.join(s.data,'stories','1.json');
  await writeFile(known,` ${JSON.stringify(s.archive.records[0])}\n`); const before = await tree(s.data);
  ok(cli(...s.prepare)); assert.deepEqual((await progress(s)).input.stories.map(x => x.id),['2']);
  assert.deepEqual((await progress(s)).input.registry,s.archive.registry);
  await accept(s,'Security'); ok(cli(...applyArgs(s))); assert.deepEqual((await tree(s.data))['stories/1.json'],before['stories/1.json']);
});

for (const [name,mutate] of [
  ['missing',r => { r.assignments = {}; }],
  ['extra',r => { r.assignments['99'] = ['Security']; }],
  ['nonliteral',r => { r.assignments['01'] = r.assignments['1']; delete r.assignments['1']; }],
  ['empty labels',r => { r.assignments['1'] = []; }],
  ['wrong input digest',r => { r.inputSha256 = '0'.repeat(64); }],
  ['unknown field',r => { r.secret = 'PRIVATE_SENTINEL'; }],
]) test(`accept refuses ${name} without mutation`,async t => {
  const s = await setup(t); ok(cli(...s.prepare)); const r = await response(s); mutate(r.value); await writeFile(r.file,stableJSON(r.value));
  const before = await tree(s.work);
  const {main} = await import('../scripts/backfill-tags.mjs'); assert.equal(typeof main,'function');
  const args = ['accept','--work-dir',s.work,'--response',r.file];
  const expected = ['missing','extra','nonliteral'].includes(name) ? /cover exact requested IDs/ : name === 'empty labels' ? /nonempty label array/ : /bind the exact pending input/;
  await assert.rejects(main(args),expected);
  const result = cli(...args);
  assert.equal(result.status,1); assert.doesNotMatch(result.stderr,/PRIVATE_SENTINEL/); assert.deepEqual(await tree(s.work),before);
});

test('duplicate JSON story keys are rejected rather than silently overwritten', async t => {
  const s = await setup(t); ok(cli(...s.prepare)); const r = await response(s);
  await writeFile(r.file,`{"inputSha256":"${r.value.inputSha256}","assignments":{"1":["AI"],"1":["Security"]}}`);
  const before = await tree(s.work); assert.equal(cli('accept','--work-dir',s.work,'--response',r.file).status,1); assert.deepEqual(await tree(s.work),before);
});

for (const relative of ['tags.json','issues/2026-11-13.json','revisions/2026-11-13/v1.json']) test(`apply rejects exact source byte changes: ${relative}`,async t => {
  const s = await setup(t); ok(cli(...s.prepare)); await accept(s,'Security','one.json'); await accept(s,'Security','two.json');
  const target = path.join(s.data,relative); await writeFile(target,` ${await readFile(target,'utf8')}`); const before = await tree(s.data);
  assert.equal(cli(...applyArgs(s)).status,1); assert.deepEqual(await tree(s.data),before); assert.equal(cli(...s.prepare).status,1);
});

test('empty archive completes knowingly without fabricated topics or AI input', async t => {
  const s = await setup(t,{empty:true}); await rm(path.join(s.data,'tags.json')); await rm(path.join(s.data,'stories'),{recursive:true});
  ok(cli(...s.prepare)); assert.equal((await progress(s)).input,null); ok(cli(...applyArgs(s)));
  const a = await loadStoryArchive(s.data); assert.deepEqual(a.registry.tags,[]); assert.deepEqual(a.records,[]);
});

for (const missing of [false,true]) test(`zero-story current issue completes without AI output; tag artifacts initially ${missing ? 'absent' : 'present'}`,async t => {
  const s = await setup(t), issue = withoutStories(s.archive.issues[0]);
  await writeFile(path.join(s.data,'issues',`${issue.issueId}.json`),` ${JSON.stringify(issue)}\n\n`);
  if (missing) { await rm(path.join(s.data,'tags.json')); await rm(path.join(s.data,'stories'),{recursive:true}); }
  const before = await tree(s.data), {main} = await import('../scripts/backfill-tags.mjs');
  assert.equal(typeof main,'function'); await main(s.prepare);
  const snapshot = JSON.parse(await readFile(path.join(s.work,'input.json'),'utf8')), p = await progress(s);
  assert.equal(snapshot.input,null); assert.deepEqual(snapshot.records,[]);
  assert.deepEqual(snapshot.source.files,Object.fromEntries(Object.entries(before).filter(([,value]) => Object.hasOwn(value,'bytes')).map(([name,value]) => [name,sha256(Buffer.from(value.bytes,'base64'))])));
  assert.deepEqual(p,{schemaVersion:1,previousSha256:sha256(stableJSON(snapshot)),accepted:null,input:null});
  assert.deepEqual((await readdir(s.work)).sort(),['input.json','progress-000000.json']);
  const privateBefore = await tree(s.work);
  assert.equal((await lstat(s.work)).mode & 0o777,0o700);
  for (const value of Object.values(privateBefore)) assert.equal(value.mode,0o600);
  ok(cli(...s.prepare)); assert.deepEqual(await tree(s.work),privateBefore); assert.deepEqual(await tree(s.data),before);
  ok(cli(...applyArgs(s))); const after = await tree(s.data), archive = await loadStoryArchive(s.data);
  assert.equal(archive.issues.length,1); assert.deepEqual(archive.issues[0].items,{});
  assert.equal(archive.revisions.length,1); assert.ok(Object.keys(archive.revisions[0].items).length > 0,'retained stories are not current inputs');
  assert.deepEqual(archive.registry,{schemaVersion:1,tags:[]}); assert.deepEqual(archive.records,[]); assert.deepEqual(archive.stories,[]);
  assert.deepEqual(await readdir(path.join(s.data,'stories')),[]); assert.equal(after['tags.json'].mode,0o644);
  for (const [name,value] of Object.entries(before)) assert.deepEqual(after[name],value,name);
  ok(cli(...applyArgs(s))); assert.deepEqual(await tree(s.data),after); assert.deepEqual(await tree(s.work),privateBefore);
});

test('mixed populated and empty current issues retain story coverage and the complete archive binding',async t => {
  const s = await setup(t), next = withoutStories(structuredClone(s.archive.issues[0]));
  next.issueId = '2026-11-20'; next.scheduledAt = '2026-11-20T08:00:00Z'; next.collectedAt = '2026-11-20T08:05:00Z';
  next.window = {start:'2026-11-13T08:00:00Z',end:next.scheduledAt}; next.revision = 1; delete next.correction;
  validateIssue(next);
  await writeFile(path.join(s.data,'issues',`${next.issueId}.json`),`\n${JSON.stringify(next)}\n`);
  const before = await tree(s.data); ok(cli(...s.prepare));
  const snapshot = JSON.parse(await readFile(path.join(s.work,'input.json'),'utf8'));
  assert.deepEqual(snapshot.input.stories.map(story => story.id),['1','2']); assert.equal(snapshot.input.issueId,next.issueId);
  assert.equal(snapshot.input.reviewedSha256,sha256(stableJSON([...s.archive.issues,next])));
  await accept(s,'Security','one.json'); await accept(s,'Security','two.json'); ok(cli(...applyArgs(s)));
  const archive = await loadStoryArchive(s.data), after = await tree(s.data);
  assert.equal(archive.issues.length,2);
  assert.deepEqual(archive.records.map(record => [record.id,record.issueIds]),[['1',['2026-11-13']],['2',['2026-11-13']]]);
  for (const name of Object.keys(before).filter(n => /^(issues|revisions)(\/|$)/.test(n))) assert.deepEqual(after[name],before[name],name);
});

for (const kind of ['work symlink','archive symlink','response symlink','private modes','hardlink checkpoint','nonregular response','work inside archive','work inside repository']) test(`safe filesystem boundary: ${kind}`,async t => {
  const s = await setup(t);
  const {main} = await import('../scripts/backfill-tags.mjs'); assert.equal(typeof main,'function');
  if (kind === 'work inside archive' || kind === 'work inside repository') {
    const work = kind === 'work inside archive' ? path.join(s.data,'private') : path.resolve('private-backfill-must-not-create');
    const args = ['prepare','--data-dir',s.data,'--work-dir',work];
    await assert.rejects(main(args),/outside repository and archive/);
    assert.equal(cli(...args).status,1); await assert.rejects(lstat(work),{code:'ENOENT'}); return;
  }
  if (kind === 'archive symlink') { const alias = path.join(s.root,'alias'); await symlink(s.data,alias); const args = ['prepare','--data-dir',alias,'--work-dir',s.work]; await assert.rejects(main(args),/Symlink/); assert.equal(cli(...args).status,1); return; }
  if (kind === 'work symlink') { const target = path.join(s.root,'target'); await mkdir(target,{mode:0o700}); await symlink(target,s.work); await assert.rejects(main(s.prepare),/Symlink/); assert.equal(cli(...s.prepare).status,1); return; }
  ok(cli(...s.prepare)); const r = await response(s);
  if (kind === 'response symlink') { const alias = path.join(s.root,'alias'); await symlink(r.file,alias); r.file = alias; }
  if (kind === 'private modes') await chmod(path.join(s.work,'input.json'),0o644);
  if (kind === 'hardlink checkpoint') await link(path.join(s.work,'progress-000000.json'),path.join(s.root,'alias'));
  if (kind === 'nonregular response') { await rm(r.file); await mkdir(r.file); }
  assert.equal(cli('accept','--work-dir',s.work,'--response',r.file).status,1);
});

test('tampered frozen input/checkpoints fail closed and never replace accepted artifacts',async t => {
  const s = await setup(t); ok(cli(...s.prepare)); await accept(s,'Security');
  const file = path.join(s.work,'progress-000001.json'), value = JSON.parse(await readFile(file,'utf8'));
  value.accepted.candidate.assignments['1'] = ['invented']; await writeFile(file,stableJSON(value));
  const before = await tree(s.work); assert.equal(cli(...s.prepare).status,1); assert.equal(cli(...applyArgs(s)).status,1); assert.deepEqual(await tree(s.work),before);
});

test('offline CLI cannot spawn mail/runtime helpers or perform network I/O',async t => {
  const s = await setup(t), guard = path.join(s.root,'guard.mjs'), sentinel = path.join(s.root,'MAIL_WAS_INVOKED');
  // Restrict process creation/network at the actual child CLI boundary, not a mocked backfill helper.
  await writeFile(guard,`import cp from 'node:child_process'; import net from 'node:net'; import http from 'node:http'; import https from 'node:https'; import dgram from 'node:dgram'; import {syncBuiltinESMExports} from 'node:module';\nconst fail=()=>{throw Error('FORBIDDEN_EXTERNAL_CALL');}; for(const k of ['spawn','spawnSync','exec','execSync','execFile','execFileSync','fork']) cp[k]=fail; net.connect=net.createConnection=net.Socket.prototype.connect=fail; http.request=http.get=https.request=https.get=dgram.createSocket=globalThis.fetch=fail; syncBuiltinESMExports();`);
  const bin = path.join(s.root,'bin'); await mkdir(bin);
  for (const name of ['sendmail','mail','himalaya','python','python3','curl','wget']) await writeFile(path.join(bin,name),`#!/bin/sh\nprintf invoked > '${sentinel}'\nexit 99\n`,{mode:0o755});
  const run = args => spawnSync(process.execPath,['--import',guard,'scripts/backfill-tags.mjs',...args],{encoding:'utf8',timeout:15000,env:{...process.env,PATH:bin}});
  ok(run(s.prepare)); const first = await response(s); ok(run(['accept','--work-dir',s.work,'--response',first.file]));
  const second = await response(s,'Security','second.json'); ok(run(['accept','--work-dir',s.work,'--response',second.file])); ok(run(applyArgs(s)));
  const empty = await setup(t), issue = withoutStories(empty.archive.issues[0]);
  await writeFile(path.join(empty.data,'issues',`${issue.issueId}.json`),stableJSON(issue));
  await rm(path.join(empty.data,'tags.json')); await rm(path.join(empty.data,'stories'),{recursive:true});
  ok(run(empty.prepare)); ok(run(empty.prepare)); ok(run(applyArgs(empty))); ok(run(applyArgs(empty)));
  await assert.rejects(lstat(sentinel),{code:'ENOENT'});
  const source = await readFile('scripts/backfill-tags.mjs','utf8'); assert.doesNotMatch(source,/runtime\/(weekly|delivery|orchestrator)|child_process|https?:|fetch\(/);
});

test('UTF-8 BOM response bytes remain exactly resumable and retain the original digest',async t => {
  const s = await setup(t); ok(cli(...s.prepare)); const r = await response(s);
  const bytes = Buffer.concat([Buffer.from([0xef,0xbb,0xbf]),Buffer.from(stableJSON(r.value))]);
  await writeFile(r.file,bytes); ok(cli('accept','--work-dir',s.work,'--response',r.file));
  const p = await progress(s); assert.equal(p.accepted.responseSha256,sha256(bytes));
  assert.deepEqual(Buffer.from(p.accepted.responseText),bytes);
  const before = await tree(s.work); ok(cli('accept','--work-dir',s.work,'--response',r.file)); assert.deepEqual(await tree(s.work),before);
});

test('default/invalid/duplicate CLI options are strict and a changed batch size cannot resume',async t => {
  const s = await setup(t); ok(cli(...s.prepare)); const before = await tree(s.work);
  for (const suffix of [['--force','yes'],['--batch-size','2'],['--work-dir',s.work]]) assert.equal(cli(...s.prepare,...suffix).status,1);
  assert.equal(cli(...s.prepare.slice(0,-1),'2').status,1);
  assert.deepEqual(await tree(s.work),before);
  for (const size of ['0','1001','1.5','01','Infinity','-1']) {
    const work = path.join(s.root,`invalid-${size}`);
    assert.equal(cli('prepare','--data-dir',s.data,'--work-dir',work,'--batch-size',size).status,1);
    await assert.rejects(lstat(work),{code:'ENOENT'});
  }
});

test('missing/gapped/orphan checkpoints and modified unique inputs cannot resume as successful progress',async t => {
  for (const mode of ['missing','gap','orphan','duplicate input']) {
    const s = await setup(t); ok(cli(...s.prepare));
    if (mode === 'missing') await rm(path.join(s.work,'progress-000000.json'));
    if (mode === 'gap') await writeFile(path.join(s.work,'progress-000002.json'),await readFile(path.join(s.work,'progress-000000.json')),{mode:0o600});
    if (mode === 'orphan') await writeFile(path.join(s.work,'.backfill-interrupted.tmp'),'partial',{mode:0o600});
    if (mode === 'duplicate input') {
      const file = path.join(s.work,'input.json'), value = JSON.parse(await readFile(file,'utf8'));
      value.input.stories.push(value.input.stories[0]); await writeFile(file,stableJSON(value));
      const {main} = await import('../scripts/backfill-tags.mjs'); assert.equal(typeof main,'function');
      await assert.rejects(main(s.prepare),/duplicate tagging story ID/);
    }
    const before = await tree(s.work); assert.equal(cli(...s.prepare).status,1); assert.equal(cli(...applyArgs(s)).status,1); assert.deepEqual(await tree(s.work),before);
  }
});

test('a second current issue contributes its own unique inputs and complete issue references',async t => {
  const s = await setup(t), next = structuredClone(s.archive.issues[0]);
  next.issueId = '2026-11-20'; next.scheduledAt = '2026-11-20T08:00:00Z'; next.collectedAt = '2026-11-20T08:05:00Z';
  next.window = {start:'2026-11-13T08:00:00Z',end:next.scheduledAt}; next.revision = 1; delete next.correction;
  next.items = Object.fromEntries(Object.values(next.items).map(item => {
    const id = item.id === '2' ? '3' : item.id;
    return [id,{...item,id,title:`Current reviewed ${id}`,submittedAt:'2026-11-17T12:00:00Z',discussionUrl:`https://news.ycombinator.com/item?id=${id}`,sources:[{kind:'discussion',url:`https://news.ycombinator.com/item?id=${id}`}]}];
  }));
  next.sections = next.sections.map(section => ({...section,itemIds:section.itemIds.map(id => id === '2' ? '3' : id)}));
  await writeFile(path.join(s.data,'issues',`${next.issueId}.json`),stableJSON(next),{mode:0o644});
  ok(cli(...s.prepare)); const snapshot = JSON.parse(await readFile(path.join(s.work,'input.json'),'utf8'));
  assert.deepEqual(snapshot.input.stories.map(x => x.id),['1','2','3']);
  assert.equal(snapshot.input.stories[0].title,'Current reviewed 1');
  assert.equal(snapshot.input.reviewedSha256,sha256(stableJSON([...s.archive.issues,next])));
  for (const filename of ['one.json','two.json','three.json']) await accept(s,'Security',filename);
  ok(cli(...applyArgs(s))); const a = await loadStoryArchive(s.data);
  assert.deepEqual(a.records.find(r => r.id === '1').issueIds,['2026-11-13','2026-11-20']);
});

test('canonical private artifact bytes are immutable, including whitespace-only checkpoint/input edits',async t => {
  for (const name of ['input.json','progress-000000.json']) {
    const s = await setup(t); ok(cli(...s.prepare)); const file = path.join(s.work,name);
    await writeFile(file,` ${await readFile(file,'utf8')}`); const before = await tree(s.work);
    const {main} = await import('../scripts/backfill-tags.mjs'); assert.equal(typeof main,'function');
    await assert.rejects(main(s.prepare),/canonical.*bytes/i);
    assert.deepEqual(await tree(s.work),before);
  }
});

test('failed atomic accept retains all accepted progress and a retry can succeed',async t => {
  const s = await setup(t); ok(cli(...s.prepare)); const r = await response(s), before = await tree(s.work);
  const args = ['accept','--work-dir',s.work,'--response',r.file];
  const script = `import fs from 'node:fs/promises'; import {syncBuiltinESMExports} from 'node:module';
    const link=fs.link; fs.link=async (...args)=>{if(args[1].endsWith('progress-000001.json'))throw Error('simulated I/O failure'); return link(...args);};
    syncBuiltinESMExports(); const {main}=await import('./scripts/backfill-tags.mjs'); await main(${JSON.stringify(args)}).then(()=>{process.exitCode=0;},()=>{process.exitCode=1;});`;
  const result = spawnSync(process.execPath,['--input-type=module','-e',script],{encoding:'utf8',timeout:15000});
  assert.equal(result.status,1,result.stderr); assert.deepEqual(await tree(s.work),before);
  ok(cli(...args)); assert.deepEqual((await progress(s)).input.stories.map(x => x.id),['2']);
});

test('interrupted public apply is refused on retry, never auto-completed or treated as complete',async t => {
  const s = await setup(t); ok(cli(...s.prepare)); await accept(s,'Security','one.json'); await accept(s,'Security','two.json');
  const before = await tree(s.data), args = applyArgs(s);
  const script = `import fs from 'node:fs/promises'; import {syncBuiltinESMExports} from 'node:module';
    const link=fs.link; fs.link=async (...args)=>{await link(...args); throw Error('simulated I/O failure after first write');};
    syncBuiltinESMExports(); const {main}=await import('./scripts/backfill-tags.mjs'); await main(${JSON.stringify(args)}).then(()=>{process.exitCode=0;},()=>{process.exitCode=1;});`;
  const result = spawnSync(process.execPath,['--input-type=module','-e',script],{encoding:'utf8',timeout:15000});
  assert.equal(result.status,1,result.stderr); const partial = await tree(s.data);
  assert.ok(partial['stories/1.json'],'a real public file was committed before the failure');
  assert.deepEqual(partial['tags.json'],before['tags.json']);
  assert.equal(cli(...args).status,1); assert.deepEqual(await tree(s.data),partial);
  for (const name of Object.keys(before).filter(n => /^(issues|revisions)(\/|$)/.test(n))) assert.deepEqual(partial[name],before[name],name);
});

test('backfill prompt documents the exact batch-bound three-step workflow and immutable progress',async t => {
  const text = await readFile('runtime/TAGGING_PROMPT.md','utf8');
  for (const pattern of [/backfill-tags\.mjs prepare/,/backfill-tags\.mjs accept/,/backfill-tags\.mjs apply/,/inputSha256/,/progress-000000\.json/,/--batch-size/,/human.reviewed/i,/never.*email/i]) assert.match(text,pattern);
  const s = await setup(t); ok(cli(...s.prepare.slice(0,-2)));
  assert.equal(JSON.parse(await readFile(path.join(s.work,'input.json'),'utf8')).batchSize,50);
  const source = text.match(/<<'JS'\n([\s\S]*?)\nJS/u)?.[1]; assert.equal(typeof source,'string');
  const result = spawnSync(process.execPath,['--input-type=module','-',path.join(s.work,'progress-000000.json')],{input:source,encoding:'utf8',timeout:15000});
  ok(result); const printed = JSON.parse(result.stdout), p = await progress(s);
  assert.deepEqual(printed,{inputSha256:sha256(stableJSON(p.input)),input:p.input});
});

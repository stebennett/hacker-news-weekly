import test from 'node:test';
import assert from 'node:assert/strict';
import { cp, mkdtemp, mkdir, readFile, rm, symlink, writeFile } from 'node:fs/promises';
import { spawnSync } from 'node:child_process';
import path from 'node:path';
import { tmpdir } from 'node:os';
import { pathToFileURL } from 'node:url';
import { snapshotArtifacts, compareSnapshots, verifyReproducible } from '../scripts/verify-reproducible.mjs';

async function temporary(t) {
  const root = await mkdtemp(path.join(tmpdir(),'twihn-reproducible-'));
  t.after(() => rm(root,{recursive:true,force:true}));
  return root;
}

async function artifactTrees(t) {
  const root = await temporary(t);
  const left = path.join(root,'left'), right = path.join(root,'right');
  for (const tree of [left,right]) {
    await mkdir(path.join(tree,'nested'),{recursive:true});
    await writeFile(path.join(tree,'nested/index.html'),Buffer.from([0,1,128,255]));
    await writeFile(path.join(tree,'build-manifest.json'),'{"sourceRevision":"unversioned"}\n');
  }
  assert.equal(typeof snapshotArtifacts,'function');
  assert.equal(typeof compareSnapshots,'function');
  const baseline = await snapshotArtifacts(left);
  assert.equal(baseline.length,2);
  compareSnapshots(baseline,await snapshotArtifacts(right));
  return {left,right,baseline};
}

test('snapshot API accepts identical complete inventories and sorts paths',async t => {
  const {left,baseline} = await artifactTrees(t);
  assert.deepEqual(baseline.map(file=>file.path),['build-manifest.json','nested/index.html']);
  assert.deepEqual(await snapshotArtifacts(left),baseline);
});

for (const name of ['changed bytes','changed manifest','extra file','missing file','renamed file']) {
  test(`snapshot comparison rejects ${name}`,async t => {
    const {right,baseline} = await artifactTrees(t);
    if (name==='changed bytes') await writeFile(path.join(right,'nested/index.html'),Buffer.from([0,1,128,254]));
    if (name==='changed manifest') await writeFile(path.join(right,'build-manifest.json'),'{"sourceRevision":"changed"}\n');
    if (name==='extra file') await writeFile(path.join(right,'extra.json'),'extra');
    if (name==='missing file' || name==='renamed file') await rm(path.join(right,'nested/index.html'));
    if (name==='renamed file') await writeFile(path.join(right,'nested/renamed.html'),Buffer.from([0,1,128,255]));
    const changed = await snapshotArtifacts(right);
    assert.throws(()=>compareSnapshots(baseline,changed),/Artifact snapshots differ/);
  });
}

for (const kind of ['file symlink','directory symlink','fifo']) {
  test(`snapshot rejects nonregular ${kind}`,async t => {
    const {left,right} = await artifactTrees(t);
    const target = path.join(right,'nonregular');
    if (kind==='fifo') {
      const result=spawnSync('mkfifo',[target],{encoding:'utf8',timeout:5000});
      assert.equal(result.status,0,result.stderr);
    } else await symlink(kind==='file symlink' ? path.join(left,'build-manifest.json') : left,target);
    await assert.rejects(snapshotArtifacts(right),/Nonregular artifact/);
  });
}

const inputNames=['src','scripts','schema','public','data','astro.config.mjs','tsconfig.json','package.json','package-lock.json','.node-version'];
async function sourceRepository(t) {
  const root = await temporary(t);
  for (const name of inputNames) await cp(name,path.join(root,name),{recursive:true});
  await symlink(path.resolve('node_modules'),path.join(root,'node_modules'),'dir');
  return root;
}

test('import exposes APIs without running builds or CLI',async () => {
  const url=pathToFileURL(path.resolve('scripts/verify-reproducible.mjs')).href;
  const result=spawnSync(process.execPath,['--input-type=module','-e',`
    import cp from 'node:child_process';
    import {syncBuiltinESMExports} from 'node:module';
    cp.spawnSync=()=>{throw Error('BUILD_ON_IMPORT');}; syncBuiltinESMExports();
    const api=await import(${JSON.stringify(url)});
    if (typeof api.verifyReproducible!=='function') throw Error('missing API');
    console.log('IMPORT_ONLY');
  `],{encoding:'utf8',timeout:15000});
  assert.equal(result.status,0,result.stdout+result.stderr);
  assert.equal(result.stdout.trim(),'IMPORT_ONLY');
});

test('CLI verifies two real clean isolated source-only builds without Git output', {timeout:180000}, async t => {
  const root=await sourceRepository(t);
  const git=spawnSync('git',['init','--quiet'],{cwd:root,encoding:'utf8',timeout:5000});
  assert.equal(git.status,0,git.stderr);
  const tracked=spawnSync('git',['ls-files','dist'],{cwd:root,encoding:'utf8',timeout:5000});
  assert.equal(tracked.status,0,tracked.stderr);
  assert.equal(tracked.stdout,'');
  // These poisoned local outputs/caches must neither be consumed nor modified.
  for (const name of ['dist','.astro','.cache']) {
    await mkdir(path.join(root,name));
    await writeFile(path.join(root,name,'sentinel'),'local state must survive');
  }
  const before=await Promise.all(inputNames.map(async name=>name.includes('.') ? await readFile(path.join(root,name)) : await snapshotArtifacts(path.join(root,name))));
  const result=spawnSync(process.execPath,['scripts/verify-reproducible.mjs'],{
    cwd:root,encoding:'utf8',timeout:170000,
    env:{...process.env,TWIHN_DATA_DIR:'/nonexistent/ambient-data',TWIHN_OUT_DIR:'/nonexistent/ambient-dist',TWIHN_SOURCE_REVISION:''}
  });
  assert.equal(result.status,0,result.stdout+result.stderr);
  assert.match(result.stdout,/Reproducible: [1-9][0-9]* artifacts identical across two clean isolated builds \(including build-manifest.json\)/);
  const after=await Promise.all(inputNames.map(async name=>name.includes('.') ? await readFile(path.join(root,name)) : await snapshotArtifacts(path.join(root,name))));
  assert.deepEqual(after,before,'building must not mutate source inputs');
  for (const name of ['dist','.astro','.cache']) assert.equal(await readFile(path.join(root,name,'sentinel'),'utf8'),'local state must survive');
});

for (const [name,source,pattern,timeout] of [
  ['exit failure',"console.error('INTENDED_BUILD_FAILURE'); process.exit(7);",/Build 1 failed.*exit 7/s,15000],
  ['signal failure',"process.kill(process.pid,'SIGTERM');",/Build 1 failed.*SIGTERM/s,15000],
  ['timeout',"setInterval(()=>{},1000);",/Build 1 failed.*ETIMEDOUT/s,100]
]) {
  test(`reproducibility propagates build ${name}`, {timeout:30000}, async t => {
    const root=await sourceRepository(t);
    await writeFile(path.join(root,'scripts/build.mjs'),source);
    assert.equal(typeof verifyReproducible,'function');
    await assert.rejects(verifyReproducible({repository:root,timeout}),pattern);
  });
}

test('reproducibility rejects differing real child output and uses independent roots',async t => {
  const root=await sourceRepository(t);
  await writeFile(path.join(root,'scripts/build.mjs'),`
    import {mkdir,writeFile,access} from 'node:fs/promises';
    import assert from 'node:assert/strict';
    import path from 'node:path';
    await assert.rejects(access('.astro'));
    assert.equal(process.env.TWIHN_DATA_DIR,path.join(process.cwd(),'data'));
    assert.equal(process.env.TWIHN_OUT_DIR,path.join(process.cwd(),'dist'));
    await mkdir('.astro');
    await mkdir('dist');
    await writeFile('dist/build-manifest.json',process.cwd());
  `);
  await assert.rejects(verifyReproducible({repository:root}),/Artifact snapshots differ/);
});

import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { cp, lstat, mkdtemp, mkdir, readFile, rm, symlink, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { pathToFileURL } from 'node:url';
import { inventory, digest } from './artifacts.mjs';

export async function snapshotArtifacts(root) {
  assert.ok((await lstat(root)).isDirectory(),'Nonregular artifact root');
  return Promise.all((await inventory(root)).map(async file => ({
    path:file, sha256:digest(await readFile(path.join(root,file)))
  })));
}

export function compareSnapshots(left,right) {
  assert.deepEqual(right,left,'Artifact snapshots differ (file inventory or bytes)');
}

async function prepareBuild(repository,root) {
  await mkdir(root);
  // Copy source inputs only: never reuse existing output or Astro/Vite state.
  for (const name of ['src','scripts','schema','public','data','package.json','package-lock.json','.node-version','tsconfig.json']) {
    await cp(path.join(repository,name),path.join(root,name),{recursive:true});
  }
  try { await cp(path.join(repository,'.npmrc'),path.join(root,'.npmrc')); }
  catch (error) { if (error.code!=='ENOENT') throw error; }
  await symlink(path.join(repository,'node_modules'),path.join(root,'node_modules'),'dir');
  await cp(path.join(repository,'astro.config.mjs'),path.join(root,'base-astro.config.mjs'));
  // node_modules is shared read-only input; Vite must not put its cache there.
  await writeFile(path.join(root,'astro.config.mjs'),`import config from './base-astro.config.mjs';\nexport default {...config,cacheDir:'./.cache/astro',vite:{...config.vite,cacheDir:'.cache/vite'}};\n`);
}

export async function verifyReproducible({repository=process.cwd(),timeout=180000}={}) {
  repository=path.resolve(repository);
  const temporary=await mkdtemp(path.join(tmpdir(),'twihn-repeat-build-'));
  try {
    const snapshots=[];
    for (const number of [1,2]) {
      const root=path.join(temporary,`build-${number}`);
      await prepareBuild(repository,root);
      const dist=path.join(root,'dist');
      const result=spawnSync(process.execPath,['scripts/build.mjs'],{
        cwd:root,stdio:'inherit',timeout,
        env:{...process.env,TWIHN_DATA_DIR:path.join(root,'data'),TWIHN_OUT_DIR:dist,
          TWIHN_SOURCE_REVISION:process.env.TWIHN_SOURCE_REVISION || 'unversioned'}
      });
      if (result.error || result.status!==0) {
        throw new Error(`Build ${number} failed: ${result.error?.code || result.signal || `exit ${result.status}`}`,{cause:result.error});
      }
      // The manifest is intentionally included, not merely its artifact digests.
      snapshots.push(await snapshotArtifacts(dist));
    }
    compareSnapshots(...snapshots);
    return snapshots[0];
  } finally {
    await rm(temporary,{recursive:true,force:true});
  }
}

if (process.argv[1] && import.meta.url===pathToFileURL(process.argv[1]).href) {
  const snapshot=await verifyReproducible();
  console.log(`Reproducible: ${snapshot.length} artifacts identical across two clean isolated builds (including build-manifest.json)`);
}

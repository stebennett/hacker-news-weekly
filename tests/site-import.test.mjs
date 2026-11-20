import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, writeFile, readFile, rm } from 'node:fs/promises';
import { spawnSync } from 'node:child_process';
import path from 'node:path';
import { tmpdir } from 'node:os';
import { legacy, metadata, fixture } from './site-fixtures.mjs';

test('legacy import CLI validates then exclusively creates public output', async () => {
  const tmp=await mkdtemp(path.join(tmpdir(),'twihn-import-'));
  try {
    const input=path.join(tmp,'reviewed.json'), meta=path.join(tmp,'metadata.json'), output=path.join(tmp,'issue.json');
    const reviewed=legacy(); reviewed.sections[1].items=[];
    await writeFile(input,JSON.stringify(reviewed)); await writeFile(meta,JSON.stringify(metadata()));
    const invoke=()=>spawnSync(process.execPath,['scripts/import-legacy.mjs',input,meta,output],{encoding:'utf8',timeout:10000});
    const result=invoke(); assert.equal(result.status,0,result.stderr); assert.deepEqual(JSON.parse(await readFile(output,'utf8')),fixture());
    const first=await readFile(output,'utf8'); assert.notEqual(invoke().status,0,'existing revision must never silently overwrite'); assert.equal(await readFile(output,'utf8'),first);
  } finally { await rm(tmp,{recursive:true,force:true}); }
});

import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, mkdir, writeFile, readFile, rm } from 'node:fs/promises';
import { spawnSync } from 'node:child_process';
import { createServer } from 'node:http';
import path from 'node:path';
import { tmpdir } from 'node:os';
import { fixture } from './site-fixtures.mjs';
import { stableJSON, loadArchive, validateIssue } from '../scripts/content.mjs';
import { inventory } from '../scripts/artifacts.mjs';
import { verifyDist } from '../scripts/verify-dist.mjs';

test('fixture build: served routes, escaping, revisions, archive ordering, repeatable bytes and tamper rejection', {timeout:180000}, async () => {
  const tmp = await mkdtemp(path.join(tmpdir(),'twihn-site-'));
  const data = path.join(tmp,'data'), dist = path.join(tmp,'dist');
  const previous = {data:process.env.TWIHN_DATA_DIR,revision:process.env.TWIHN_SOURCE_REVISION};
  let server;
  try {
    await mkdir(`${data}/issues`,{recursive:true});
    await mkdir(`${data}/revisions/2026-11-06`,{recursive:true});
    const latest = fixture();
    const old = fixture(); old.issueId='2026-11-06'; old.scheduledAt='2026-11-06T08:00:00Z'; old.window={start:'2026-10-30T08:00:00Z',end:old.scheduledAt}; old.collectedAt='2026-11-06T08:05:00Z';
    for (const item of Object.values(old.items)) item.submittedAt='2026-11-03T12:00:00Z';
    validateIssue(old);
    await writeFile(`${data}/revisions/${old.issueId}/v1.json`,stableJSON(old));
    old.revision=2; old.correction={previousRevision:1,correctedAt:'2026-11-14T10:00:00Z',note:'Synthetic correction, test only.'};
    await writeFile(`${data}/issues/${old.issueId}.json`,stableJSON(old));
    await writeFile(`${data}/issues/${latest.issueId}.json`,stableJSON(latest));
    process.env.TWIHN_DATA_DIR=data; process.env.TWIHN_SOURCE_REVISION='fixture-source';
    const build=()=>{const result=spawnSync(process.execPath,['scripts/build.mjs'],{encoding:'utf8',timeout:60000,env:{...process.env,TWIHN_OUT_DIR:dist}}); assert.equal(result.status,0,result.stdout+result.stderr);};
    build(); await verifyDist(dist);
    const first = Object.fromEntries(await Promise.all((await inventory(dist)).map(async name=>[name,await readFile(`${dist}/${name}`,'utf8')])));
    build();
    const second = Object.fromEntries(await Promise.all((await inventory(dist)).map(async name=>[name,await readFile(`${dist}/${name}`,'utf8')])));
    assert.deepEqual(second,first,'clean rebuild must be byte-for-byte reproducible');
    server=createServer(async (req,res)=>{try {const url=new URL(req.url,'http://localhost'); const relative=url.pathname.endsWith('/') ? `${url.pathname}index.html` : url.pathname; const bytes=await readFile(path.join(dist,relative));res.end(bytes);} catch {res.writeHead(404);res.end();}});
    await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
    const base=`http://127.0.0.1:${server.address().port}`;
    for (const route of ['/','/issues/2026-11-13/','/issues/2026-11-06/','/data/issues/2026-11-13.json','/data/revisions/2026-11-06/v1.json','/data/index.json','/data/latest.json','/build-manifest.json','/schema/issue.schema.json']) assert.equal((await fetch(base+route)).status,200,route);
    const html=await (await fetch(base+'/issues/2026-11-13/')).text();
    assert.doesNotMatch(html,/<script\b|<img\b/i); assert.match(html,/&lt;script&gt;/); assert.doesNotMatch(html,/Sources and availability|unverified/); assert.match(html,/None met the 50-point threshold/);
    assert.equal((html.match(/data-hn-id="1"/g)||[]).length,1,'ordinary item is rendered only in its highest-precedence ranking');
    const corrected=await (await fetch(base+'/issues/2026-11-06/')).text(); assert.match(corrected,/Correction — revision 2/);
    assert.deepEqual((await (await fetch(base+'/data/index.json')).json()).map(x=>x.issueId),['2026-11-13','2026-11-06']);
    assert.deepEqual(await (await fetch(base+'/data/latest.json')).json(),latest);
    await writeFile(`${dist}/index.html`,'tampered'); await assert.rejects(verifyDist(dist),/digest\/inventory parity/);
    await rm(`${data}/revisions/2026-11-06/v1.json`); await assert.rejects(loadArchive(data),/missing retained revision/);
  } finally {
    if (server) await new Promise(resolve=>server.close(resolve));
    for (const [name,value] of [['TWIHN_DATA_DIR',previous.data],['TWIHN_SOURCE_REVISION',previous.revision]]) if(value===undefined) delete process.env[name]; else process.env[name]=value;
    await rm(tmp,{recursive:true,force:true});
  }
});

import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, mkdir, writeFile, readFile, rm } from 'node:fs/promises';
import { spawnSync } from 'node:child_process';
import { createServer } from 'node:http';
import path from 'node:path';
import { tmpdir } from 'node:os';
import { fixture } from './site-fixtures.mjs';
import { recordsFor, writeTagData, writeTagArchive } from './site-tag-fixtures.mjs';
import { loadStoryArchive } from '../scripts/tags.mjs';
import { stableJSON, loadArchive, validateIssue } from '../scripts/content.mjs';
import { inventory } from '../scripts/artifacts.mjs';
import { verifyDist } from '../scripts/verify-dist.mjs';
import { isolatedBuildRoot, runBuild } from './site-artifact-fixtures.mjs';

const escape = text => text.replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

function assertWeeklyStories(html, issue, registry, records) {
  const cards = [...html.matchAll(/<li data-hn-id="([1-9][0-9]*)">([\s\S]*?)(?=<li data-hn-id=|<\/ol>)/g)];
  assert.deepEqual(cards.map(([,id]) => id), issue.sections.flatMap(section => section.itemIds), 'ranking precedence and item order are unchanged');
  for (const [,id,card] of cards) {
    const item = issue.items[id];
    const titleAndTags = card.match(/<h3>([\s\S]*?)<\/h3>\s*<ul\b([^>]*)>([\s\S]*?)<\/ul>/);
    assert.ok(titleAndTags, `story ${id} title must be immediately followed by tag links`);
    assert.equal(titleAndTags[1], `<a href="${escape(item.sourceUrl || item.discussionUrl)}" rel="noreferrer">${escape(item.title)}</a>`);
    assert.match(titleAndTags[2], /aria-label="Story tags"/);
    const expectedTags = records.find(record => record.id === id).tagSlugs.map(slug => registry.tags.find(tag => tag.slug === slug));
    assert.deepEqual([...titleAndTags[3].matchAll(/<a href="([^"]+)">([^<]*)<\/a>/g)].map(([,href,label]) => [href,label]), expectedTags.map(tag => [`/tags/${tag.slug}/`,escape(tag.label)]), 'exact validated tag hrefs and escaped labels');
    assert.equal((titleAndTags[3].match(/<li\b/g) || []).length, expectedTags.length, 'one list entry per tag');
    const local = new Intl.DateTimeFormat('en-GB',{timeZone:'Europe/London',dateStyle:'medium',timeStyle:'short'}).format(new Date(item.submittedAt));
    assert.ok(card.includes(`<p class="meta">${item.score} points · ${item.comments} comments · ${local} (Europe/London) · <a href="${item.discussionUrl}">HN discussion</a></p>`));
    for (const field of ['story','discussion']) assert.ok(card.includes(`<p><strong>${field === 'story' ? 'Story' : 'Discussion'}</strong> — ${escape(item[field])}</p>`), `${field} paragraph bytes unchanged for ${id}`);
    assert.doesNotMatch(card, /href="\/issues\//, 'weekly cards must not acquire archive-only source issue links');
  }
  assert.doesNotMatch(html, /<script\b|<img\b|<style\b/i, 'escaped text cannot introduce active elements');
  for (const tag of html.match(/<[^>]+>/g) || []) assert.doesNotMatch(tag, /\sstyle\s*=|\son\w+\s*=/i, 'no inline styles or event attributes');
  assert.doesNotMatch(html, /Independent rankings\. Reviewed summaries\.|Sources and availability|unverified|Correction — revision|Collected|Reviewed JSON|Issue permalink|Synthetic correction, test only\./);
}

test('fixture build: served routes, escaping, revisions, archive ordering, repeatable bytes and tamper rejection', {timeout:180000}, async () => {
  const tmp = await mkdtemp(path.join(tmpdir(),'twihn-site-'));
  const data = path.join(tmp,'data'), dist = path.join(tmp,'dist');
  const previous = {data:process.env.TWIHN_DATA_DIR,revision:process.env.TWIHN_SOURCE_REVISION};
  let server;
  try {
    await mkdir(`${data}/issues`,{recursive:true});
    await mkdir(`${data}/revisions/2026-11-06`,{recursive:true});
    const latest = fixture();
    latest.items['1'].sourceUrl='https://example.com/research?a=1&b=2';
    latest.items['1'].sources.push({kind:'story',url:latest.items['1'].sourceUrl});
    latest.items['1'].availability.story={status:'reviewed',note:'Synthetic reviewed source.'};
    validateIssue(latest);
    const old = fixture(); old.issueId='2026-11-06'; old.scheduledAt='2026-11-06T08:00:00Z'; old.window={start:'2026-10-30T08:00:00Z',end:old.scheduledAt}; old.collectedAt='2026-11-06T08:05:00Z';
    for (const item of Object.values(old.items)) item.submittedAt='2026-11-03T12:00:00Z';
    validateIssue(old);
    await writeFile(`${data}/revisions/${old.issueId}/v1.json`,stableJSON(old));
    old.revision=2; old.correction={previousRevision:1,correctedAt:'2026-11-14T10:00:00Z',note:'Synthetic correction, test only.'};
    old.items['1'].title='Historical corrected title & facts';
    old.items['1'].story='Historical corrected Story <img src=x onerror=alert(1)>.';
    old.items['1'].discussion='Historical corrected discussion & themes.';
    old.items['1'].score=123; old.items['1'].comments=45;
    await writeFile(`${data}/issues/${old.issueId}.json`,stableJSON(old));
    await writeFile(`${data}/issues/${latest.issueId}.json`,stableJSON(latest));
    const registry={schemaVersion:1,tags:[{slug:'ai',label:'AI'},{slug:'security',label:'<script>alert("tag")</script> & Topics'}]};
    const records=recordsFor([latest,old]);
    records.find(record=>record.id==='1').tagSlugs=['ai','security'];
    records.find(record=>record.id==='2').tagSlugs=['security'];
    await writeTagData(data,{registry,records});
    await loadStoryArchive(data); // Complete validated input, not an untagged fixture fallback.
    const sourcePaths=[`issues/${latest.issueId}.json`,`issues/${old.issueId}.json`,`revisions/${old.issueId}/v1.json`];
    const sourceBytes=await Promise.all(sourcePaths.map(name=>readFile(`${data}/${name}`,'utf8')));
    process.env.TWIHN_DATA_DIR=data; process.env.TWIHN_SOURCE_REVISION='fixture-source';
    const root=await isolatedBuildRoot(tmp);
    const build=()=>{const result=runBuild(root,data,dist,'fixture-source'); assert.equal(result.status,0,result.stdout+result.stderr);};
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
    assert.doesNotMatch(html,/Independent rankings\. Reviewed summaries\./); assert.doesNotMatch(html,/<script\b|<img\b/i); assert.match(html,/&lt;script&gt;/); assert.doesNotMatch(html,/Sources and availability|unverified/); assert.match(html,/None met the 50-point threshold/);
    assert.equal((html.match(/data-hn-id="1"/g)||[]).length,1,'ordinary item is rendered only in its highest-precedence ranking');
    const corrected=await (await fetch(base+'/issues/2026-11-06/')).text(); assert.doesNotMatch(corrected,/Correction — revision 2|Collected|Reviewed JSON|Issue permalink/);
    const home=await (await fetch(base+'/')).text();
    assertWeeklyStories(home,latest,registry,records);
    assertWeeklyStories(html,latest,registry,records);
    assertWeeklyStories(corrected,old,registry,records);
    const manifestPath=path.join(dist,'build-manifest.json');
    const {makeManifest}=await import('../scripts/artifacts.mjs');
    for (const route of ['index.html','issues/2026-11-06/index.html']) {
      const target=path.join(dist,route), original=await readFile(target,'utf8');
      try {
        await writeFile(target,original.replace(/<ul\b[^>]*aria-label="Story tags"[^>]*>[\s\S]*?<\/ul>/,''));
        await writeFile(manifestPath,stableJSON(await makeManifest(dist,'fixture-source',[latest,old])));
        await assert.rejects(verifyDist(dist),/HTML|card |tag /i,'historical/home missing tags fail independent of hashes');
      } finally {await writeFile(target,original);}
    }
    await writeFile(manifestPath,stableJSON(await makeManifest(dist,'fixture-source',[latest,old])));
    assert.doesNotMatch(corrected,/&lt;script&gt;alert\(&quot;x&quot;\)/, 'historical card must use historical facts, not the latest resolved item');
    assert.deepEqual(await Promise.all(sourcePaths.map(name=>readFile(`${data}/${name}`,'utf8'))),sourceBytes,'build never rewrites source issues or retained revisions');
    for (let i=0;i<sourcePaths.length;i++) assert.equal(await readFile(`${dist}/data/${sourcePaths[i]}`,'utf8'),sourceBytes[i],'public issue/revision JSON retains exact original bytes');
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

test('explicit empty tag archive retains neutral bootstrap landing', {timeout:60000}, async t => {
  const data=await writeTagArchive(t,{issues:[],revisions:[],registry:{schemaVersion:1,tags:[]},records:[]});
  const dist=path.join(data,'output');
  const root=await isolatedBuildRoot(data);
  const result=runBuild(root,data,dist);
  assert.equal(result.status,0,result.stdout+result.stderr);
  const html=await readFile(`${dist}/index.html`,'utf8');
  assert.match(html,/No issues have been published yet/);
  assert.match(html,/The archive is currently empty/);
  assert.doesNotMatch(html,/data-hn-id=|href="\/tags\/[^"/]/, 'empty homepage has no story cards or invented topic links');
  assert.match(html,/<a href="\/tags\/">Tags<\/a>/, 'shared navigation still exposes the neutral tag directory');
});

test('nonempty weekly build fails closed when story tagging is incomplete', {timeout:60000}, async t => {
  const data=await writeTagArchive(t);
  await loadStoryArchive(data); // Prove valid control input before removing one record.
  await rm(path.join(data,'stories','1.json'));
  const root=await isolatedBuildRoot(data);
  const check=spawnSync('npm',['run','check'],{cwd:root,encoding:'utf8',timeout:60000,env:{...process.env,TWIHN_DATA_DIR:data}});
  assert.notEqual(check.status,0,'npm check data validator must reject incomplete story coverage');
  assert.match(check.stdout+check.stderr,/story coverage mismatch: missing \[1\]/);
  const result=runBuild(root,data,path.join(data,'output'));
  assert.notEqual(result.status,0,'a nonempty issue cannot silently render without complete tags');
  assert.match(result.stdout+result.stderr,/story coverage mismatch: missing \[1\]/);
  await assert.rejects(readFile(path.join(data,'output','index.html')), {code:'ENOENT'}, 'untagged homepage must not be emitted');
});

test('unused registry entries are rejected by check and build preflight without altering output',async t=>{
  const data=await writeTagArchive(t), dist=path.join(data,'output');
  await loadStoryArchive(data);
  const registry=JSON.parse(await readFile(path.join(data,'tags.json'),'utf8'));
  registry.tags.push({slug:'unused',label:'Unused'});
  await writeFile(path.join(data,'tags.json'),stableJSON(registry));
  await mkdir(dist); await writeFile(path.join(dist,'sentinel.txt'),'must survive preflight');
  for (const script of ['validate','build']) {
    const result=spawnSync(process.execPath,[`scripts/${script}.mjs`],{encoding:'utf8',timeout:60000,env:{...process.env,TWIHN_DATA_DIR:data,TWIHN_OUT_DIR:dist}});
    assert.notEqual(result.status,0);
    assert.match(result.stdout+result.stderr,/unused registry tag: unused/);
    assert.equal(await readFile(path.join(dist,'sentinel.txt'),'utf8'),'must survive preflight');
  }
});

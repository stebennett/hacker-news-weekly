import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdir, readFile, rm, writeFile } from 'node:fs/promises';
import { isolatedBuildRoot, runBuild } from './site-artifact-fixtures.mjs';
import { createServer } from 'node:http';
import path from 'node:path';
import { fixture } from './site-fixtures.mjs';
import { recordsFor, writeTagArchive } from './site-tag-fixtures.mjs';
import { validateIssue, stableJSON } from '../scripts/content.mjs';
import { loadStoryArchive } from '../scripts/tags.mjs';
import { inventory, makeManifest } from '../scripts/artifacts.mjs';
import { verifyDist } from '../scripts/verify-dist.mjs';

const escape = text => text.replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const idsIn = html => [...html.matchAll(/data-hn-id="([1-9][0-9]*)"/g)].map(([,id]) => id);
const linksIn = html => [...html.matchAll(/<a\b[^>]*href="([^"]+)"[^>]*>([^<]*)<\/a>/g)].map(([,href,label]) => [href,label]);
const cardsIn = html => [...html.matchAll(/<li data-hn-id="([1-9][0-9]*)">([\s\S]*?)(?=<li data-hn-id=|<\/ol>)/g)];

function fortyOneStories() {
  const issue = fixture();
  const template = issue.items['2'];
  const ids = Array.from({length:41}, (_,n) => String(9007199254740993n + BigInt(n)));
  issue.items = Object.fromEntries(ids.map((id,n) => [id,{
    ...structuredClone(template), id, kind:'show', title:`Show HN: fixture ${id} & archive`,
    // Several timestamp groups test newest-first independently of numeric ID ties.
    submittedAt:`2026-11-${String(7 + n % 5).padStart(2,'0')}T12:00:00Z`,
    score:100, comments:10, discussionUrl:`https://news.ycombinator.com/item?id=${id}`,
    story:`Current reviewed Story ${id} <script>not active</script>.`,
    discussion:`Current reviewed Discussion ${id} & facts.`,
    sources:[{kind:'discussion',url:`https://news.ycombinator.com/item?id=${id}`}]
  }]));
  issue.sections.forEach(section => {section.itemIds=[]; section.qualifyingCount=0;});
  issue.sections[3].itemIds=[...ids].sort().reverse();
  issue.sections[3].qualifyingCount=ids.length;
  validateIssue(issue); // Real Friday window, Show threshold and ranking/count guards stay enabled.
  const revision = structuredClone(issue);
  issue.revision=2;
  issue.correction={previousRevision:1,correctedAt:'2026-11-14T10:00:00Z',note:'Synthetic correction, test only.'};
  const expected = [...Object.values(issue.items)].sort((a,b) => Date.parse(b.submittedAt)-Date.parse(a.submittedAt) || (BigInt(a.id)>BigInt(b.id) ? -1 : 1)).map(item => item.id);
  revision.items[expected[0]].title='Superseded fixture title';
  revision.items[expected[0]].story='Superseded fixture Story.';
  validateIssue(revision); validateIssue(issue);
  const registry={schemaVersion:1,tags:[{slug:'a-zulu',label:'Zulu & <Topics>'},{slug:'ai',label:'AI'},{slug:'z-alpha',label:'Alpha'}]};
  const records=recordsFor([issue]);
  records.find(record => record.id===expected[0]).tagSlugs=['a-zulu','ai','z-alpha'];
  records.find(record => record.id===expected[1]).tagSlugs=['a-zulu','ai'];
  return {issues:[issue],revisions:[revision],registry,records,expected};
}

async function build(data, dist, root) {
  root ??= await isolatedBuildRoot(data);
  const result=runBuild(root,data,dist);
  assert.equal(result.status,0,result.stdout+result.stderr);
  return root;
}

async function serve(t, dist) {
  const server=createServer(async (req,res) => {
    try {
      const url=new URL(req.url,'http://localhost');
      const relative=url.pathname.endsWith('/') ? `${url.pathname}index.html` : url.pathname;
      res.end(await readFile(path.join(dist,relative)));
    } catch {res.writeHead(404);res.end();}
  });
  await new Promise(resolve => server.listen(0,'127.0.0.1',resolve));
  t.after(() => new Promise(resolve => server.close(resolve)));
  const base=`http://127.0.0.1:${server.address().port}`;
  return async route => {
    const response=await fetch(base+route);
    assert.equal(response.status,200,`HTTP readback ${route}`);
    return response.text();
  };
}

function assertSharedShell(html, route) {
  assert.match(html,new RegExp(`<link rel="canonical" href="https://twihn\\.com${route}"`));
  assert.match(html,/<nav\b[^>]*aria-label="Main navigation"[^>]*>[\s\S]*?<a href="\/tags\/">Tags<\/a>[\s\S]*?<\/nav>/);
  assert.doesNotMatch(html,/Independent rankings\. Reviewed summaries\.|<script\b|<style\b/i);
  for (const tag of html.match(/<[^>]+>/g) || []) assert.doesNotMatch(tag,/\sstyle\s*=|\son\w+\s*=/i);
}

test('real static tag routes serve all 41 unique stories in 20/20/1 pages with exact links and facts', {timeout:180000}, async t => {
  const archive=fortyOneStories();
  const data=await writeTagArchive(t,archive), dist=path.join(data,'output');
  const resolved=await loadStoryArchive(data);
  assert.equal(resolved.stories.length,41,'valid, complete unique-story input before building');
  const root=await build(data,dist);
  const get=await serve(t,dist);
  const directory=await get('/tags/');
  assertSharedShell(directory,'/tags/');
  const directoryList=directory.match(/<ul\b[^>]*aria-label="Tags"[^>]*>([\s\S]*?)<\/ul>/)?.[1];
  assert.ok(directoryList,'accessible tag directory');
  assert.deepEqual(linksIn(directoryList),[['/tags/ai/','AI'],['/tags/z-alpha/','Alpha'],['/tags/a-zulu/','Zulu &amp; &lt;Topics&gt;']], 'sort by display label, not registry slug');
  for (const [slug,count] of [['ai',41],['z-alpha',1],['a-zulu',2]]) {
    assert.match(directoryList,new RegExp(`<li[^>]*>[\\s\\S]*?<a href="/tags/${slug}/">[^<]*</a>[^<]*${count} ${count===1?'story':'stories'}[^<]*</li>`));
  }
  const allIds=[];
  for (let page=1;page<=3;page++) {
    const route=`/tags/ai/${page===1?'':`${page}/`}`;
    const html=await get(route);
    assertSharedShell(html,route);
    assert.match(html,/<h1>AI<\/h1>/);
    assert.match(html,/41 stories/);
    const ids=idsIn(html); allIds.push(...ids);
    assert.deepEqual(cardsIn(html).map(([,id])=>id),ids,'card extraction cannot skip HN markers');
    assert.match(html,new RegExp(`<ol start="${(page-1)*20+1}">`));
    assert.match(html,new RegExp(`<span aria-current="page">Page ${page} of 3</span>`));
    if (page>1) assert.match(html,/rel="prev">Previous/);
    if (page<3) assert.match(html,/rel="next">Next/);
    assert.deepEqual(ids,archive.expected.slice((page-1)*20,page*20),`page ${page} exact newest/numeric ID order`);
    const pagination=html.match(/<nav\b[^>]*aria-label="Tag archive pagination"[^>]*>([\s\S]*?)<\/nav>/)?.[1];
    assert.ok(pagination,'accessible native-anchor pagination');
    assert.match(pagination,new RegExp(`Page ${page} of 3`));
    assert.deepEqual(linksIn(pagination),[
      ...(page>1 ? [[`/tags/ai/${page===2?'':`${page-1}/`}`,'Previous']] : []),
      ...(page<3 ? [[`/tags/ai/${page+1}/`,'Next']] : [])
    ],`page ${page} exact boundary navigation`);
    assert.ok(linksIn(html).some(([href,label]) => href==='/tags/' && label==='All tags'));
    for (const [,id,card] of cardsIn(html)) {
      const item=archive.issues[0].items[id];
      for (const field of ['title','story','discussion']) assert.ok(card.includes(escape(item[field])),`${field} resolved current facts for ${id}`);
      assert.match(card,new RegExp(`${item.score} points · ${item.comments} comments`));
      assert.ok(linksIn(card).some(([href,label]) => href===`/issues/${archive.issues[0].issueId}/` && label===`Week of ${archive.issues[0].issueId}`));
      const tagList=card.match(/<ul\b[^>]*aria-label="Story tags"[^>]*>([\s\S]*?)<\/ul>/)?.[1];
      assert.ok(tagList);
      const expectedTags=archive.records.find(record => record.id===id).tagSlugs.map(slug => archive.registry.tags.find(tag => tag.slug===slug));
      assert.deepEqual(linksIn(tagList),expectedTags.map(tag => [`/tags/${tag.slug}/`,escape(tag.label)]));
    }
    assert.doesNotMatch(html,/Superseded fixture/);
  }
  assert.deepEqual(allIds,archive.expected);
  assert.equal(new Set(allIds).size,41,'no lost or duplicate cards between pages');
  for (const [slug,count] of [['a-zulu',2],['z-alpha',1]]) {
    const html=await get(`/tags/${slug}/`);
    assertSharedShell(html,`/tags/${slug}/`);
    assert.equal(idsIn(html).length,count);
    assert.match(html,/Page 1 of 1/);
    assert.doesNotMatch(html,/>Previous<|>Next</);
  }
  const files=await inventory(dist);
  assert.deepEqual(files.filter(file => file.startsWith('tags/') && file.endsWith('.html')),['tags/a-zulu/index.html','tags/ai/2/index.html','tags/ai/3/index.html','tags/ai/index.html','tags/index.html','tags/z-alpha/index.html']);
  for (const route of ['/tags/ai/1/','/tags/ai/4/','/tags/unknown/']) {
    await assert.rejects(readFile(path.join(dist,route,'index.html')), {code:'ENOENT'});
  }
  for (const route of ['/','/issues/2026-11-13/']) assertSharedShell(await get(route),route);
  // Regenerating hashes must never authorize semantically false artifacts.
  const previous={data:process.env.TWIHN_DATA_DIR,revision:process.env.TWIHN_SOURCE_REVISION};
  try {
    process.env.TWIHN_DATA_DIR=data; process.env.TWIHN_SOURCE_REVISION='tag-route-fixture';
    await verifyDist(dist);
    await t.test('complete canonical public JSON and schema exports are inventoried',async()=>{
      const manifest=JSON.parse(await readFile(path.join(dist,'build-manifest.json'),'utf8'));
      for (const name of ['tags.json',...archive.records.map(record=>`stories/${record.id}.json`)]) {
        assert.equal(await readFile(path.join(dist,'data',name),'utf8'),await readFile(path.join(data,name),'utf8'));
        assert.equal(await get(`/data/${name}`),await readFile(path.join(data,name),'utf8'),'served public record parity');
        assert.ok(manifest.artifacts[`data/${name}`]);
      }
      for (const name of ['tags','story']) {
        assert.equal(await readFile(path.join(dist,`schema/${name}.schema.json`),'utf8'),stableJSON(JSON.parse(await readFile(`schema/${name}.schema.json`,'utf8'))));
        assert.equal(await get(`/schema/${name}.schema.json`),stableJSON(JSON.parse(await readFile(`schema/${name}.schema.json`,'utf8'))),'served public schema parity');
        assert.ok(manifest.artifacts[`schema/${name}.schema.json`]);
      }
    });
    for (const name of ['data/tags.json',`data/stories/${archive.expected[0]}.json`,'schema/tags.schema.json','schema/story.schema.json']) await t.test(`reject forged ${name} with fresh manifest`,async()=>{
      const target=path.join(dist,name), original=await readFile(target,'utf8');
      try {
        const value=JSON.parse(original);
        if (name.startsWith('schema/')) value.additionalProperties=true;
        else value.schemaVersion=2;
        await writeFile(target,stableJSON(value));
        assert.notEqual(await readFile(target,'utf8'),original,'mutate the actual public contract');
        await writeFile(path.join(dist,'build-manifest.json'),stableJSON(await makeManifest(dist,'tag-route-fixture',resolved.issues)));
        await assert.rejects(verifyDist(dist),/data parity/);
      } finally {await writeFile(target,original);}
    });
    const route='tags/ai/index.html', first=archive.expected[0], second=archive.expected[1];
    const mutations=[
      ['membership',route,html=>html.replace(`data-hn-id="${first}"`,`data-hn-id="123"`)],
      ['order',route,html=>html.replace(`data-hn-id="${first}"`,'data-hn-id="SWAP"').replace(`data-hn-id="${second}"`,`data-hn-id="${first}"`).replace('data-hn-id="SWAP"',`data-hn-id="${second}"`)],
      ['missing card body',route,html=>html.replace(/<li data-hn-id="[^"]+">[\s\S]*?<\/h3>/,'<li data-hn-id="'+first+'">')],
      ['comment cannot supply summary',route,html=>html.replace(/<p><strong>Story<\/strong>[\s\S]*?<\/p>/,text=>`<!--${text}-->`)],
      ['summary',route,html=>html.replace('Current reviewed Story','Fabricated reviewed Story')],
      ['title',route,html=>html.replace('Show HN: fixture','Fabricated title')],
      ['tag label',route,html=>html.replace('>AI</a>','>Fabricated</a>')],
      ['tag href',route,html=>html.replace('href="/tags/ai/"','href="/tags/unknown/"')],
      ['source href',route,html=>html.replace(/(<h3><a href=")[^"]+/, '$1https://example.com/wrong')],
      ['HN href',route,html=>html.replace(/href="https:\/\/news.ycombinator.com\/item\?id=[^"]+">HN discussion/, 'href="https://example.com/wrong">HN discussion')],
      ['issue URL',route,html=>html.replace('href="/issues/2026-11-13/"','href="/issues/2026-11-06/"')],
      ['metrics',route,html=>html.replace('100 points','999 points')],
      ['archive heading',route,html=>html.replace('<h1>AI</h1>','<h1>Wrong</h1>')],
      ['archive count',route,html=>html.replace('41 stories','40 stories')],
      ['pagination count',route,html=>html.replace('Page 1 of 3','Page 1 of 2')],
      ['pagination href',route,html=>html.replace('href="/tags/ai/2/"','href="/tags/ai/3/"')],
      ['pagination rel',route,html=>html.replace('rel="next"','rel="prev"')],
      ['list start','tags/ai/2/index.html',html=>html.replace('start="21"','start="1"')],
      ['canonical',route,html=>html.replace('https://twihn.com/tags/ai/','https://twihn.com/tags/ai/2/')],
      ['directory canonical','tags/index.html',html=>html.replace('https://twihn.com/tags/','https://twihn.com/')],
      ['directory count','tags/index.html',html=>html.replace('41 stories','40 stories')],
      ['directory label order','tags/index.html',html=>html.replace('>AI</a>','>Zulu</a>')],
      ['directory complete entries reordered','tags/index.html',html=>html.replace(/(<li><a href="\/tags\/ai\/">[\s\S]*?<\/li>)(<li><a href="\/tags\/z-alpha\/">[\s\S]*?<\/li>)/,'$2$1')],
      ['directory missing link','tags/index.html',html=>html.replace(/<li><a href="\/tags\/ai\/">[\s\S]*?<\/li>/,'')],
      ['directory fabricated link','tags/index.html',html=>html.replace('href="/tags/ai/"','href="/tags/fabricated/"')],
      ['home tag link','index.html',html=>html.replace(/<ul\b[^>]*aria-label="Story tags"[^>]*>[\s\S]*?<\/ul>/,'')],
      ['issue tag link','issues/2026-11-13/index.html',html=>html.replace(/<ul\b[^>]*aria-label="Story tags"[^>]*>[\s\S]*?<\/ul>/,'')],
      ['unmarked weekly card','issues/2026-11-13/index.html',html=>html.replace('</article>','<ol><li>Fabricated unmarked card</li></ol></article>')],
      ['attribute injection',route,html=>html.replace('<h3>','<h3 onclick="alert(1)">')],
      ['unescaped label','tags/a-zulu/index.html',html=>html.replace('Zulu &amp; &lt;Topics&gt;','Zulu & <Topics>')]
    ];
    for (const [name,file,mutate] of mutations) await t.test(`reject ${name} even with fresh manifest`,async()=>{
      const target=path.join(dist,file), original=await readFile(target,'utf8');
      const changed=mutate(original); assert.notEqual(changed,original,'mutation must reach generated markup');
      try {
        await writeFile(target,changed);
        await writeFile(path.join(dist,'build-manifest.json'),stableJSON(await makeManifest(dist,'tag-route-fixture',resolved.issues)));
        await assert.rejects(verifyDist(dist),/HTML|CSP|canonical|tag |card |directory|pagination|escaping/i);
      } finally {await writeFile(target,original);}
    });
    for (const name of ['tags/ai/3/index.html','tags/index.html','data/tags.json','schema/story.schema.json']) await t.test(`missing expected ${name} fails despite regenerated manifest`,async()=>{
      const target=path.join(dist,name), original=await readFile(target);
      try {
        await rm(target);
        await writeFile(path.join(dist,'build-manifest.json'),stableJSON(await makeManifest(dist,'tag-route-fixture',resolved.issues)));
        await assert.rejects(verifyDist(dist),/missing public artifact|HTML route inventory/);
      } finally {await writeFile(target,original);}
    });
    for (const name of ['data/tagging.json','data/stories/123.json','tags/fabricated/index.html']) await t.test(`reject unexpected ${name} with fresh manifest`,async()=>{
      const target=path.join(dist,name);
      await mkdir(path.dirname(target),{recursive:true});
      try {
        await writeFile(target,'{}');
        await writeFile(path.join(dist,'build-manifest.json'),stableJSON(await makeManifest(dist,'tag-route-fixture',resolved.issues)));
        await assert.rejects(verifyDist(dist),/unexpected public artifact/);
      } finally {await rm(target);}
    });
    await mkdir(path.join(dist,'tags','ai','4'),{recursive:true});
    await writeFile(path.join(dist,'tags','ai','4','index.html'),await readFile(path.join(dist,'tags','ai','index.html')));
    await writeFile(path.join(dist,'build-manifest.json'),stableJSON(await makeManifest(dist,'tag-route-fixture',resolved.issues)));
    await assert.rejects(verifyDist(dist),/unexpected public artifact tags\/ai\/4\/index.html/);
    await rm(path.join(dist,'tags/ai/4'),{recursive:true});
    await writeFile(path.join(dist,'build-manifest.json'),stableJSON(await makeManifest(dist,'tag-route-fixture',resolved.issues)));
    const snapshot=async()=>Object.fromEntries(await Promise.all((await inventory(dist)).map(async name=>[name,await readFile(path.join(dist,name),'utf8')])));
    const before=await snapshot();
    await build(data,dist,root);
    assert.deepEqual(await snapshot(),before,'complete repeated build byte/inventory parity');
    const keep=new Set(archive.expected.slice(0,20)), issue=archive.issues[0];
    issue.items=Object.fromEntries(Object.entries(issue.items).filter(([id])=>keep.has(id)));
    issue.sections[3].itemIds=issue.sections[3].itemIds.filter(id=>keep.has(id));
    issue.sections[3].qualifyingCount=20; validateIssue(issue);
    await writeFile(path.join(data,'issues',`${issue.issueId}.json`),stableJSON(issue));
    for (const record of archive.records.filter(record=>!keep.has(record.id))) await rm(path.join(data,'stories',`${record.id}.json`));
    await loadStoryArchive(data);
    await build(data,dist,root); await verifyDist(dist);
    const shrunk=await inventory(dist);
    assert.deepEqual(shrunk.filter(name=>name.startsWith('data/stories/')), [...keep].map(id=>`data/stories/${id}.json`).sort(),'no stale public story JSON');
    assert.deepEqual(shrunk.filter(name=>name.startsWith('tags/ai/')),['tags/ai/index.html'],'obsolete pages disappear on same-output rebuild');
    assert.deepEqual(idsIn(await readFile(path.join(dist,route),'utf8')),[...keep]);
  } finally {
    for (const [key,value] of [['TWIHN_DATA_DIR',previous.data],['TWIHN_SOURCE_REVISION',previous.revision]]) if (value===undefined) delete process.env[key]; else process.env[key]=value;
  }
});

test('explicit empty registry serves a neutral tag directory with no invented topic archives', {timeout:60000}, async t => {
  const data=await writeTagArchive(t,{issues:[],revisions:[],registry:{schemaVersion:1,tags:[]},records:[]});
  const dist=path.join(data,'output');
  await loadStoryArchive(data);
  await build(data,dist);
  const get=await serve(t,dist), html=await get('/tags/');
  assertSharedShell(html,'/tags/');
  assert.match(html,/<h1>Tags<\/h1>/);
  assert.match(html,/No tags are available yet\./);
  assert.doesNotMatch(html,/data-hn-id=|href="\/tags\/[^"/]/);
  assert.deepEqual((await inventory(dist)).filter(file => file.startsWith('tags/')),['tags/index.html']);
});

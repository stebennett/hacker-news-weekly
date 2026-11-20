import test from 'node:test';
import assert from 'node:assert/strict';
import { cp, mkdir, readFile, symlink, writeFile } from 'node:fs/promises';
import { spawnSync } from 'node:child_process';
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

async function build(data, dist) {
  // Astro writes .astro/.prerender under its project root, even with a separate
  // outDir. Isolate this suite from concurrently running weekly fixture builds.
  const root=path.join(data,'build-root');
  await mkdir(root);
  for (const name of ['src','scripts','schema','public','package.json','tsconfig.json']) await cp(name,path.join(root,name),{recursive:true});
  await symlink(path.resolve('node_modules'),path.join(root,'node_modules'),'dir');
  await cp('astro.config.mjs',path.join(root,'base-astro.config.mjs'));
  await writeFile(path.join(root,'astro.config.mjs'),`import config from './base-astro.config.mjs';\nexport default {...config,cacheDir:'./.cache/astro',vite:{...config.vite,cacheDir:'.cache/vite'}};\n`);
  const result=spawnSync(process.execPath,['scripts/build.mjs'],{cwd:root,encoding:'utf8',timeout:60000,env:{...process.env,TWIHN_DATA_DIR:data,TWIHN_OUT_DIR:dist,TWIHN_SOURCE_REVISION:'tag-route-fixture'}});
  assert.equal(result.status,0,result.stdout+result.stderr);
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
  await build(data,dist);
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
  // Minimal inter-task inventory hook must not broadly authorize invented tag HTML.
  const previous={data:process.env.TWIHN_DATA_DIR,revision:process.env.TWIHN_SOURCE_REVISION};
  try {
    process.env.TWIHN_DATA_DIR=data; process.env.TWIHN_SOURCE_REVISION='tag-route-fixture';
    await verifyDist(dist);
    await mkdir(path.join(dist,'tags','ai','4'),{recursive:true});
    await writeFile(path.join(dist,'tags','ai','4','index.html'),await readFile(path.join(dist,'tags','ai','index.html')));
    await writeFile(path.join(dist,'build-manifest.json'),stableJSON(await makeManifest(dist,'tag-route-fixture',resolved.issues)));
    await assert.rejects(verifyDist(dist),/unexpected public artifact tags\/ai\/4\/index.html/);
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

import { readFile } from 'node:fs/promises';
import assert from 'node:assert/strict';
import { pathToFileURL } from 'node:url';
import { expectedData, inventory, makeManifest } from './artifacts.mjs';
import { stableJSON } from './content.mjs';

const escape = text => String(text).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const element = (tag, attrs={}, children=[]) => ({tag,attrs,children});
const walk = node => [node,...(node.children ?? []).filter(child=>typeof child!=='string').flatMap(walk)];
const select = (node, predicate) => walk(node).filter(predicate);
const one = (nodes, label) => {assert.equal(nodes.length,1,`HTML requires exactly one ${label}`);return nodes[0];};

// A deliberately restricted parser for OUR generated static HTML, not a general
// browser HTML parser. Require explicit balanced end tags, quoted attributes and
// the emitted element vocabulary. Reject comments/declarations (except doctype),
// malformed markup and duplicate attributes instead of HTML error recovery. Thus
// comments, injected elements or broken nesting cannot supply missing card facts.
function parseGeneratedHTML(html) {
  const root=element('#document'), stack=[root];
  const voids=new Set(['meta','link']);
  const vocabulary=new Set(['html','head','meta','title','link','body','a','header','nav','main','footer','p','article','h1','section','h2','ol','li','h3','ul','strong','span']);
  const tokens=/<!DOCTYPE html>|<\/[a-z][a-z0-9-]*\s*>|<[a-z][a-z0-9-]*(?:\s+[a-zA-Z_:][a-zA-Z0-9_:.-]*="[^"<>]*")*\s*>|[^<]+/gy;
  let offset=0;
  while (offset<html.length) {
    tokens.lastIndex=offset;
    const match=tokens.exec(html);
    assert.ok(match,`HTML unsupported or malformed markup at ${offset}`);
    const token=match[0]; offset=tokens.lastIndex;
    if (token==='<!DOCTYPE html>') {assert.equal(offset,token.length,'HTML doctype position');continue;}
    if (token.startsWith('</')) {
      const tag=token.slice(2,-1).trim();
      assert.ok(stack.length>1 && stack.at(-1).tag===tag,`HTML unbalanced closing ${tag}`);
      stack.pop();
    } else if (token.startsWith('<')) {
      const tag=token.match(/^<([a-z][a-z0-9-]*)/)[1];
      assert.ok(vocabulary.has(tag),`HTML unsupported element ${tag} (CSP/escaping)`);
      const attrs={};
      for (const [,name,value] of token.matchAll(/\s+([a-zA-Z_:][a-zA-Z0-9_:.-]*)="([^"<>]*)"/g)) {
        assert.ok(!Object.hasOwn(attrs,name),`HTML duplicate attribute ${name}`);
        assert.ok(!/^on|^style$/i.test(name),'CSP incompatible attribute');
        attrs[name]=value;
      }
      const node=element(tag,attrs);
      stack.at(-1).children.push(node);
      if (!voids.has(tag)) stack.push(node);
    } else if (token.trim()) stack.at(-1).children.push(token);
  }
  assert.equal(stack.length,1,'HTML unclosed elements');
  one(select(root,node=>node.tag==='html'),'html document');
  return root;
}

const link = (href,label,attrs={}) => element('a',{href:escape(href),...attrs},[escape(label)]);
const paragraph = text => element('p',{},[escape(text)]);
const countText = count => `${count} ${count===1?'story':'stories'}`;

function expectedCard(item, tagSlugs, tagsBySlug, sourceIssueId) {
  const local=new Intl.DateTimeFormat('en-GB',{timeZone:'Europe/London',dateStyle:'medium',timeStyle:'short'}).format(new Date(item.submittedAt));
  return element('li',{'data-hn-id':item.id},[
    element('h3',{},[link(item.sourceUrl || item.discussionUrl,item.title,{rel:'noreferrer'})]),
    element('ul',{class:'tags','aria-label':'Story tags'},tagSlugs.map(slug=>element('li',{},[link(`/tags/${slug}/`,tagsBySlug.get(slug).label)]))),
    element('p',{class:'meta'},[escape(`${item.score} points · ${item.comments} comments · ${local} (Europe/London) · `),link(item.discussionUrl,'HN discussion')]),
    ...['Story','Discussion'].map(label=>element('p',{},[element('strong',{},[label]),escape(` — ${item[label.toLowerCase()]}`)])),
    ...(sourceIssueId ? [element('p',{class:'meta'},[link(`/issues/${sourceIssueId}/`,`Week of ${sourceIssueId}`)])] : [])
  ]);
}

function verifyCards(document, html, expected, tagsBySlug) {
  const markers=select(document,node=>Object.hasOwn(node.attrs,'data-hn-id'));
  const cards=select(document,node=>node.tag==='ol').flatMap(list=>list.children);
  assert.ok(cards.every(card=>card.tag==='li'),'HTML story lists contain only explicit cards');
  assert.deepEqual(cards,markers,'HTML parsed cards must cover every HN marker');
  assert.deepEqual(cards.map(card=>card.attrs['data-hn-id']),[...html.matchAll(/\bdata-hn-id="([^"]*)"/g)].map(match=>match[1]),'HTML parsed cards/all HN markers parity');
  assert.deepEqual(cards.map(card=>card.attrs['data-hn-id']),expected.map(story=>story.item.id),'HTML card membership/order parity');
  for (const [index,story] of expected.entries()) {
    assert.deepEqual(cards[index],expectedCard(story.item,story.tagSlugs,tagsBySlug,story.sourceIssueId),`HTML card facts/labels/hrefs/escaping parity: ${story.item.id}`);
  }
}

export async function verifyDist(dist='dist') {
  const {data,issues,registry,records,pages} = await expectedData();
  const files = await inventory(dist);
  const htmlRoutes=['index.html',...issues.map(issue=>`issues/${issue.issueId}/index.html`),'tags/index.html',...pages.map(page=>`${page.url.slice(1)}index.html`)];
  const required=['_headers','build-manifest.json',...Object.keys(data),...htmlRoutes];
  const allowed=new Set(required), present=new Set(files);
  for (const file of files) assert.ok(allowed.has(file) || /^_astro\/[A-Za-z0-9_.-]+\.css$/.test(file),`unexpected public artifact ${file}`);
  for (const file of required) assert.ok(present.has(file),`missing public artifact ${file}`);
  assert.deepEqual(files.filter(file=>file.endsWith('.html')),htmlRoutes.sort(),'HTML route inventory parity');
  for (const [name,expected] of Object.entries(data)) assert.deepEqual(await readFile(`${dist}/${name}`),Buffer.from(expected),`data parity ${name}`);
  const manifest = JSON.parse(await readFile(`${dist}/build-manifest.json`,'utf8'));
  if (process.env.TWIHN_SOURCE_REVISION) assert.equal(manifest.sourceRevision,process.env.TWIHN_SOURCE_REVISION);
  assert.equal(stableJSON(manifest),stableJSON(await makeManifest(dist,manifest.sourceRevision,issues)),'artifact digest/inventory parity');
  assert.equal(await readFile(`${dist}/_headers`,'utf8'),await readFile('public/_headers','utf8'),'header policy parity');

  const documents=new Map(), htmlByRoute=new Map();
  for (const file of htmlRoutes) {
    const html=await readFile(`${dist}/${file}`,'utf8'), document=parseGeneratedHTML(html);
    documents.set(file,document); htmlByRoute.set(file,html);
    const nodes=walk(document);
    const canonical=one(nodes.filter(node=>node.tag==='link' && node.attrs.rel==='canonical'),'canonical link');
    const route=file==='index.html' ? '/' : `/${file.slice(0,-'index.html'.length)}`;
    assert.equal(canonical.attrs.href,`https://twihn.com${route}`,'HTML canonical path parity');
    one(nodes.filter(node=>node.tag==='meta' && node.attrs.name==='viewport'),'viewport');
    const navigation=one(nodes.filter(node=>node.tag==='nav' && node.attrs['aria-label']==='Main navigation'),'main navigation');
    assert.deepEqual(navigation.children,[link('/tags/','Tags')],'HTML tag navigation parity');
    for (const node of nodes) for (const name of ['src','href']) if (node.attrs[name]) assert.doesNotMatch(node.attrs[name],/^(?:https?:)?\/\/(?!twihn\.com(?:\/|$))[^?]*\.(?:js|css|woff2?)(?:\?|$)/i,'remote asset');
  }
  const tagsBySlug=new Map(registry.tags.map(tag=>[tag.slug,tag])), recordsById=new Map(records.map(record=>[record.id,record]));
  for (const issue of issues) {
    for (const route of [`issues/${issue.issueId}/index.html`,...(issue===issues[0]?['index.html']:[])]) {
      const document=documents.get(route), html=htmlByRoute.get(route);
      assert.deepEqual(one(select(document,node=>node.tag==='h1'),'weekly heading').children,[`Week of ${issue.issueId}`],'HTML issue heading parity');
      // Tags come from independent records, but historical weekly facts remain
      // issue-local, NOT the latest projected facts used on archive cards.
      verifyCards(document,html,issue.sections.flatMap(section=>section.itemIds).map(id=>({item:issue.items[id],tagSlugs:recordsById.get(id).tagSlugs})),tagsBySlug);
      assert.doesNotMatch(html,/Sources and availability/,'source disclosure is not rendered in HTML');
      if (issue.correction) assert.doesNotMatch(html,/Correction — revision|Previous published revision/,'correction metadata is not rendered in HTML');
    }
  }
  for (const page of pages) {
    const route=`${page.url.slice(1)}index.html`, document=documents.get(route);
    verifyCards(document,htmlByRoute.get(route),page.stories,tagsBySlug);
    const main=one(select(document,node=>node.tag==='main'),'tag archive main');
    assert.deepEqual(main.children,[
      element('h1',{},[escape(page.tag.label)]),paragraph(countText(page.storyCount)),
      element('ol',{start:String((page.page-1)*20+1)},page.stories.map(story=>expectedCard(story.item,story.tagSlugs,tagsBySlug,story.sourceIssueId))),
      element('nav',{class:'pagination','aria-label':'Tag archive pagination'},[
        ...(page.previousUrl?[link(page.previousUrl,'Previous',{rel:'prev'})]:[]),
        element('span',{'aria-current':'page'},[`Page ${page.page} of ${page.pageCount}`]),
        ...(page.nextUrl?[link(page.nextUrl,'Next',{rel:'next'})]:[])
      ]),element('p',{},[link('/tags/','All tags')])
    ],`HTML tag archive heading/count/pagination/sequence parity ${page.url}`);
  }
  const directory=documents.get('tags/index.html'), main=one(select(directory,node=>node.tag==='main'),'tag directory main');
  const directoryTags=pages.filter(page=>page.page===1).sort((a,b)=>a.tag.label.localeCompare(b.tag.label,'en',{sensitivity:'base'}) || a.tag.slug.localeCompare(b.tag.slug,'en'));
  assert.deepEqual(main.children,[element('h1',{},['Tags']),directoryTags.length ? element('ul',{'aria-label':'Tags'},directoryTags.map(page=>element('li',{},[link(page.url,page.tag.label),` — ${countText(page.storyCount)}`]))) : paragraph('No tags are available yet.')],'HTML tag directory labels/order/count/membership parity');
  verifyCards(directory,htmlByRoute.get('tags/index.html'),[],tagsBySlug);
  const home=documents.get('index.html'), homeHTML=htmlByRoute.get('index.html');
  const issueArchive=one(select(home,node=>node.tag==='nav' && node.attrs['aria-label']==='Issue archive'),'issue archive');
  assert.deepEqual(select(issueArchive,node=>node.tag==='a'),issues.map(issue=>link(`/issues/${issue.issueId}/`,issue.issueId)),'HTML archive navigation missing issue');
  if (!issues.length) {
    assert.match(homeHTML,/No issues have been published yet/,'neutral bootstrap landing');
    verifyCards(home,homeHTML,[],tagsBySlug);
  }
  const headers=await readFile(`${dist}/_headers`,'utf8');
  for (const directive of ["default-src 'none'", "style-src 'self'", "frame-ancestors 'none'",'X-Content-Type-Options: nosniff','max-age=0, must-revalidate']) assert.ok(headers.includes(directive),'unsafe header policy');
  console.log(`Verified ${files.length} artifacts, ${issues.length} issue routes, ${pages.length} tag pages, complete JSON/HTML semantics and security policy`);
}
if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) await verifyDist();

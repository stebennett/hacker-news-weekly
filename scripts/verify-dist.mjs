import { readFile } from 'node:fs/promises';
import assert from 'node:assert/strict';
import { pathToFileURL } from 'node:url';
import { expectedData, inventory, makeManifest } from './artifacts.mjs';
import { stableJSON } from './content.mjs';
import { loadStoryArchive, tagArchivePages } from './tags.mjs';
export async function verifyDist(dist='dist') {
  const {data,issues} = await expectedData();
  for (const [name,expected] of Object.entries(data)) assert.equal(await readFile(`${dist}/${name}`,'utf8'),expected,`data parity ${name}`);
  const manifest = JSON.parse(await readFile(`${dist}/build-manifest.json`,'utf8'));
  if (process.env.TWIHN_SOURCE_REVISION) assert.equal(manifest.sourceRevision,process.env.TWIHN_SOURCE_REVISION);
  assert.equal(stableJSON(manifest),stableJSON(await makeManifest(dist,manifest.sourceRevision,issues)),'artifact digest/inventory parity');
  const files = await inventory(dist);
  // Task 11 inter-task hook: permit only the exact validated static tag routes.
  // Public tag JSON/schema exports and semantic artifact verification follow in Task 12.
  const {stories,registry} = await loadStoryArchive();
  const tagRoutes = ['tags/index.html',...tagArchivePages(stories,registry).map(page=>`${page.url.slice(1)}index.html`)];
  const allowed = new Set(['index.html','_headers','build-manifest.json',...Object.keys(data),...issues.map(i=>`issues/${i.issueId}/index.html`),...tagRoutes]);
  for (const file of files) assert.ok(allowed.has(file) || /^_astro\/[A-Za-z0-9_.-]+\.css$/.test(file),`unexpected public artifact ${file}`);
  assert.equal(await readFile(`${dist}/_headers`,'utf8'),await readFile('public/_headers','utf8'),'header policy parity');
  for (const file of files.filter(x=>x.endsWith('.html'))) {
    const html = await readFile(`${dist}/${file}`,'utf8');
    assert.doesNotMatch(html,/<script\b|<style\b/i,'CSP incompatible script/style');
    for (const tag of html.match(/<[^>]+>/g) ?? []) assert.doesNotMatch(tag,/\son\w+\s*=|\sstyle\s*=/i,'CSP incompatible attribute');
    assert.doesNotMatch(html,/(?:src|href)=["'](?:https?:)?\/\/(?!twihn\.com)[^"']*\.(?:js|css|woff2?)/i,'remote asset');
    assert.match(html,/name="viewport"/);
  }
  const escape = text => text.replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  for (const issue of issues) {
    for (const route of [`issues/${issue.issueId}/index.html`, ...(issue === issues[0] ? ['index.html'] : [])]) {
      const html = await readFile(`${dist}/${route}`,'utf8');
      assert.deepEqual([...html.matchAll(/data-hn-id="([1-9][0-9]*)"/g)].map(m=>m[1]),issue.sections.flatMap(s=>s.itemIds),'HTML item order parity');
      for (const item of Object.values(issue.items)) {
        for (const field of ['title','story','discussion']) assert.ok(html.includes(escape(item[field])),`HTML ${field} parity: ${item.id}`);
        assert.doesNotMatch(html,/Sources and availability/,'source disclosure is not rendered in HTML');
      }
      if (issue.correction) assert.doesNotMatch(html,/Correction — revision|Previous published revision/,'correction metadata is not rendered in HTML');
    }
  }
  const home = await readFile(`${dist}/index.html`,'utf8');
  for (const issue of issues) assert.ok(home.includes(`href="/issues/${issue.issueId}/"`),'archive navigation missing issue');
  if (!issues.length) assert.match(home,/No issues have been published yet/,'neutral bootstrap landing');
  const headers = await readFile(`${dist}/_headers`,'utf8');
  for (const directive of ["default-src 'none'", "style-src 'self'", "frame-ancestors 'none'",'X-Content-Type-Options: nosniff','max-age=0, must-revalidate']) assert.ok(headers.includes(directive),'unsafe header policy');
  console.log(`Verified ${files.length} artifacts, ${issues.length} issue routes, JSON/HTML parity and security policy`);
}
if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) await verifyDist();

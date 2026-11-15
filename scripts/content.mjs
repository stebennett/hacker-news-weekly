import { readFile, readdir } from 'node:fs/promises';
import path from 'node:path';
import Ajv from 'ajv';
import addFormats from 'ajv-formats';

export const SECTION_IDS = ['votes', 'comments', 'ask', 'show'];
export const HEADINGS = ['Top 10 stories by votes', 'Top 10 discussed stories', 'Top 10 Ask HN threads by comments', 'All popular Show HN threads (50+ points)'];
export const schema = JSON.parse(await readFile(path.resolve('schema/issue.schema.json'), 'utf8'));
const ajv = new Ajv({ allErrors: true, strict: true });
addFormats(ajv);
const structural = ajv.compile(schema);
const requireThat = (condition, message) => { if (!condition) throw new Error(message); };
export function stableJSON(value) {
  const sort = v => Array.isArray(v) ? v.map(sort) : v && typeof v === 'object' ? Object.fromEntries(Object.keys(v).sort().map(k => [k, sort(v[k])])) : v;
  return JSON.stringify(sort(value), null, 2) + '\n';
}
function safeURL(raw) {
  const url = new URL(raw);
  requireThat(['http:', 'https:'].includes(url.protocol) && url.hostname && !url.username && !url.password && !/[\s\x00-\x1f\\]/.test(raw), 'unsafe URL');
}
export function validateIssue(issue) {
  requireThat(structural(issue), `Invalid issue schema: ${ajv.errorsText(structural.errors)}`);
  const start = Date.parse(issue.window.start), end = Date.parse(issue.window.end);
  requireThat(end - start === 7 * 86400000 && end === Date.parse(issue.scheduledAt), 'window must be frozen seven days ending at scheduledAt');
  requireThat(Date.parse(issue.collectedAt) >= end, 'collection predates window end');
  const local = new Intl.DateTimeFormat('en-CA', {timeZone:'Europe/London',year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hourCycle:'h23',weekday:'short'}).formatToParts(new Date(end));
  const parts = Object.fromEntries(local.map(p => [p.type,p.value]));
  requireThat(`${parts.year}-${parts.month}-${parts.day}` === issue.issueId && parts.weekday === 'Fri' && parts.hour === '08' && parts.minute === '00' && new Date(end).getUTCSeconds() === 0, 'issue identity must be intended Friday 08:00 Europe/London');
  requireThat(issue.sections.every((s,i) => s.id === SECTION_IDS[i]), 'missing or reordered sections');
  const used = new Set();
  for (const [index, section] of issue.sections.entries()) {
    requireThat(section.itemIds.length === (index === 3 ? section.qualifyingCount : Math.min(10, section.qualifyingCount)), 'section length does not match qualifyingCount');
    const metric = index === 0 || index === 3 ? 'score' : 'comments';
    let previous = null;
    for (const id of section.itemIds) {
      const item = issue.items[id];
      requireThat(item && item.id === id, 'section references missing or mismatched ID');
      requireThat(item.kind === (index < 2 ? 'story' : index === 2 ? 'ask' : 'show'), 'wrong section category');
      if (index === 3) requireThat(item.score >= 50, 'Show HN score below 50');
      if (previous) {
        const keys = [metric, 'score', 'comments', 'id'];
        const comparison = keys.map(key => key === 'id' ? previous[key].localeCompare(item[key]) : previous[key] - item[key]).find(v => v !== 0) ?? 0;
        requireThat(comparison >= 0, 'section ranking order invalid');
      }
      previous = item; used.add(id);
    }
  }
  requireThat(issue.sections[0].qualifyingCount === issue.sections[1].qualifyingCount, 'ordinary population counts differ');
  requireThat(used.size === Object.keys(issue.items).length, 'unreferenced public item');
  const ordinaryIds = [...new Set([...issue.sections[0].itemIds, ...issue.sections[1].itemIds])];
  requireThat(ordinaryIds.length <= issue.sections[0].qualifyingCount, 'ordinary selection exceeds population');
  for (const [index,metric] of [[0,'score'],[1,'comments']]) {
    const ranked = [...ordinaryIds].sort((a,b) => issue.items[b][metric]-issue.items[a][metric] || issue.items[b].score-issue.items[a].score || issue.items[b].comments-issue.items[a].comments || b.localeCompare(a)).slice(0,10);
    requireThat(stableJSON(ranked) === stableJSON(issue.sections[index].itemIds), 'independent rankings contradict selected records');
  }
  for (const [id,item] of Object.entries(issue.items)) {
    requireThat(id === item.id, 'item key/id mismatch');
    requireThat(Date.parse(item.submittedAt) >= start && Date.parse(item.submittedAt) < end, 'submission outside window');
    requireThat(item.discussionUrl === `https://news.ycombinator.com/item?id=${id}`, 'unexpected HN discussion URL');
    for (const url of [item.sourceUrl, item.discussionUrl, ...item.sources.map(s => s.url)].filter(Boolean)) safeURL(url);
    requireThat(item.sources.some(s => s.kind === 'discussion' && s.url === item.discussionUrl), 'missing discussion source reference');
    requireThat(!item.sourceUrl || item.sources.some(s => s.kind === 'story' && s.url === item.sourceUrl), 'missing story source reference');
    requireThat(!['text-post','no-comments'].includes(item.availability.discussion.status) || item.availability.discussion.status === 'no-comments' && item.comments === 0, 'invalid discussion availability');
    requireThat(item.availability.story.status !== 'no-comments', 'invalid story availability');
    requireThat(item.availability.story.status !== 'text-post' || !item.sourceUrl, 'text post has external source');
  }
  if (issue.revision > 1) requireThat(issue.correction?.previousRevision === issue.revision - 1 && Date.parse(issue.correction.correctedAt) >= Date.parse(issue.collectedAt), 'revision needs correction metadata');
  else requireThat(!issue.correction, 'initial revision cannot be a correction');
  return issue;
}
function allowKeys(value, keys, label) { requireThat(value && Object.keys(value).every(k => keys.includes(k)), `unknown ${label} field`); }
/** Adapt reviewed legacy selected-json with explicit publication/provenance metadata.
 * metadata: issueId, scheduledAt, collectedAt, revision?, correction?,
 * qualifyingCounts: [ordinary, ordinary, ask, qualifyingShow],
 * itemSources: { [id]: { sources: [{kind,url}], availability: {story:{status,note},discussion:{status,note}} }}.
 * No inference of source access, research, or collection completeness is permitted.
 */
export function adaptLegacyReviewed(legacy, metadata) {
  allowKeys(legacy, ['start','end','sections'], 'legacy root');
  requireThat(metadata.qualifyingCounts?.length === 4, 'explicit qualifyingCounts required');
  requireThat(legacy.sections?.length === 4, 'four legacy sections required');
  const issue = { schemaVersion:1, issueId:metadata.issueId, revision:metadata.revision ?? 1, scheduledAt:metadata.scheduledAt, collectedAt:metadata.collectedAt, displayTimezone:'Europe/London', window:{start:new Date(legacy.start).toISOString().replace('.000Z','Z'), end:new Date(legacy.end).toISOString().replace('.000Z','Z')}, sections:[], items:{} };
  if (metadata.correction) issue.correction = structuredClone(metadata.correction);
  legacy.sections.forEach((group,index) => {
    allowKeys(group,['heading','items'],'legacy section');
    requireThat(group.heading === HEADINGS[index], 'legacy heading mismatch');
    issue.sections.push({id:SECTION_IDS[index],itemIds:[],qualifyingCount:metadata.qualifyingCounts[index]});
    for (const old of group.items) {
      allowKeys(old,['id','title','score','comments','created','url','story','discussion','tags'],'legacy item');
      const id = old.id;
      requireThat(typeof id === 'string' && /^[1-9][0-9]*$/.test(id), 'invalid literal HN ID');
      const provenance = metadata.itemSources?.[id];
      requireThat(provenance, `explicit sources and availability required: ${id}`);
      if (old.tags) {
        const kind = old.tags.includes('show_hn') ? 'show' : old.tags.includes('ask_hn') ? 'ask' : 'story';
        requireThat(kind === (index < 2 ? 'story' : index === 2 ? 'ask' : 'show'), 'legacy tags contradict section');
      }
      const item = {id,title:old.title,kind:index < 2 ? 'story' : index === 2 ? 'ask' : 'show', submittedAt:new Date(old.created).toISOString().replace('.000Z','Z'),score:old.score,comments:old.comments,discussionUrl:`https://news.ycombinator.com/item?id=${id}`,story:old.story,discussion:old.discussion,sources:structuredClone(provenance.sources),availability:structuredClone(provenance.availability)};
      if (old.url) item.sourceUrl = old.url;
      requireThat(!issue.items[id] || stableJSON(issue.items[id]) === stableJSON(item), 'inconsistent duplicate research');
      issue.items[id] = item; issue.sections[index].itemIds.push(id);
    }
  });
  return validateIssue(issue);
}
/** Public issue -> existing render_email.py input. Summaries/order/metrics unchanged. */
export function toLegacyReviewed(issue) {
  validateIssue(issue);
  return {start:issue.window.start,end:issue.window.end,sections:issue.sections.map((s,i) => ({heading:HEADINGS[i],items:s.itemIds.map(id => {const x=issue.items[id]; return {id:x.id,title:x.title,score:x.score,comments:x.comments,created:x.submittedAt,url:x.sourceUrl ?? '',story:x.story,discussion:x.discussion};})}))};
}
export async function loadArchive(dataDir = process.env.TWIHN_DATA_DIR || path.resolve('data')) {
  const entries = await readdir(path.join(dataDir,'issues'), {withFileTypes:true});
  const issues = [];
  for (const entry of entries) {
    if (entry.name === '.gitkeep') continue;
    requireThat(entry.isFile() && /^\d{4}-\d{2}-\d{2}\.json$/.test(entry.name), 'unexpected issue path');
    const issue = validateIssue(JSON.parse(await readFile(path.join(dataDir,'issues',entry.name),'utf8')));
    requireThat(entry.name === `${issue.issueId}.json`, 'filename/issue mismatch'); issues.push(issue);
  }
  issues.sort((a,b) => b.issueId.localeCompare(a.issueId));
  const revisions = [];
  const dirs = await readdir(path.join(dataDir,'revisions'),{withFileTypes:true});
  for (const dir of dirs) {
    if (dir.name === '.gitkeep') continue;
    requireThat(dir.isDirectory() && /^\d{4}-\d{2}-\d{2}$/.test(dir.name), 'unexpected revision directory');
    const current = issues.find(x => x.issueId === dir.name);
    requireThat(current, 'orphan revision directory');
    for (const file of await readdir(path.join(dataDir,'revisions',dir.name),{withFileTypes:true})) {
      requireThat(file.isFile() && /^v[1-9][0-9]*\.json$/.test(file.name), 'unexpected revision path');
      const old = validateIssue(JSON.parse(await readFile(path.join(dataDir,'revisions',dir.name,file.name),'utf8')));
      requireThat(old.issueId === dir.name && file.name === `v${old.revision}.json` && old.revision < current.revision, 'invalid retained revision identity');
      requireThat(stableJSON(old.window) === stableJSON(current.window) && old.scheduledAt === current.scheduledAt, 'correction changed issue window');
      revisions.push(old);
    }
  }
  for (const issue of issues) for (let r=1;r<issue.revision;r++) requireThat(revisions.some(x => x.issueId === issue.issueId && x.revision === r), 'missing retained revision');
  revisions.sort((a,b) => a.issueId.localeCompare(b.issueId) || a.revision-b.revision);
  return {issues,revisions};
}

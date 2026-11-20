import test from 'node:test';
import assert from 'node:assert/strict';
import { adaptLegacyReviewed, toLegacyReviewed, validateIssue, stableJSON } from '../scripts/content.mjs';
import { fixture, legacy, metadata } from './site-fixtures.mjs';

test('legacy adapter rejects overlap between score and comment rankings', () => {
  const counts = metadata(); counts.qualifyingCounts = [2,2,1,0];
  assert.throws(() => adaptLegacyReviewed(legacy(), counts));
});
test('legacy adapter preserves disjoint rankings and exact email summaries', () => {
  const reviewed = legacy();
  reviewed.sections[1].items = [];
  const issue = adaptLegacyReviewed(reviewed, metadata());
  assert.deepEqual(issue.sections[0].itemIds, ['1']);
  assert.deepEqual(issue.sections[1].itemIds, []);
  assert.equal(Object.keys(issue.items).length, 2);
  assert.deepEqual(toLegacyReviewed(issue), reviewed);
  assert.equal(stableJSON(issue), stableJSON(JSON.parse(stableJSON(issue))));
});
test('schema rejects private fields, malformed IDs, unsafe URLs and incomplete summaries', () => {
  for (const mutate of [i => i.recipient = 'private', i => i.items['1'].secret = true,
    i => i.items['1'].sourceUrl = 'javascript:alert(1)', i => i.items['1'].story = '',
    i => i.items['1'].discussionUrl = 'https://example.com/', i => i.issueId = '2026-99-99',
    i => i.sections[0].itemIds.push('1'), i => i.sections[0].qualifyingCount = 11,
    i => i.items['1'].submittedAt = i.window.end]) {
    const issue = fixture(); mutate(issue); assert.throws(() => validateIssue(issue));
  }
});
test('category, rankings, Show threshold and correction rules are enforced', () => {
  const issue = fixture();
  issue.sections[3].itemIds = ['1']; issue.sections[3].qualifyingCount = 1;
  assert.throws(() => validateIssue(issue));
  const correction = fixture(); correction.revision = 2;
  assert.throws(() => validateIssue(correction));
  correction.correction = { correctedAt: '2026-11-14T10:00:00Z', note: 'Corrected a fact.', previousRevision: 1 };
  validateIssue(correction);
});
test('adapter rejects private fields and unproved completeness', () => {
  const unknown = legacy(); unknown.recipient = 'secret';
  assert.throws(() => adaptLegacyReviewed(unknown, metadata()));
  const missing = metadata(); delete missing.qualifyingCounts;
  assert.throws(() => adaptLegacyReviewed(legacy(), missing));
});
test('score ranking takes precedence over comments while Show HN has no cap', () => {
  const issue=fixture();
  const template=structuredClone(issue.items['1']);
  delete issue.items['1'];
  const ordinary=[];
  for(let n=1;n<=11;n++) { const id=String(100+n); ordinary.push(id); const x={...structuredClone(template),id,score:200-n,comments:n,discussionUrl:`https://news.ycombinator.com/item?id=${id}`}; x.sources=[{kind:'discussion',url:x.discussionUrl}]; issue.items[id]=x; }
  issue.sections[0]={id:'votes',itemIds:ordinary.slice(0,10),qualifyingCount:11};
  issue.sections[1]={id:'comments',itemIds:ordinary.slice(10).reverse(),qualifyingCount:11};
  issue.sections[3].itemIds=[];
  for(let n=1;n<=12;n++) { const id=String(200+n); const x={...structuredClone(template),id,kind:'show',score:63-n,discussionUrl:`https://news.ycombinator.com/item?id=${id}`}; x.sources=[{kind:'discussion',url:x.discussionUrl}]; issue.items[id]=x; issue.sections[3].itemIds.push(id); }
  issue.sections[3].qualifyingCount=12;
  validateIssue(issue);
  issue.sections[1].itemIds=[ordinary[0]];
  assert.throws(()=>validateIssue(issue),/ordinary rankings overlap/);
  issue.sections[1].itemIds=ordinary.slice(10).reverse();
  issue.items['212'].score=49;
  assert.throws(()=>validateIssue(issue),/below 50/);
});
test('schedule identity stays UK-local across both DST transitions with seven 24-hour windows', () => {
  for (const scheduledAt of ['2026-04-03T07:00:00Z','2026-10-30T08:00:00Z']) {
    const issue=fixture(), end=Date.parse(scheduledAt);
    issue.scheduledAt=scheduledAt; issue.issueId=scheduledAt.slice(0,10);
    issue.window={start:new Date(end-7*86400000).toISOString(),end:scheduledAt};
    issue.collectedAt=new Date(end+300000).toISOString();
    for(const item of Object.values(issue.items)) item.submittedAt=new Date(end-86400000).toISOString();
    validateIssue(issue);
    issue.scheduledAt=new Date(end+3600000).toISOString();
    issue.window.end=issue.scheduledAt; issue.window.start=new Date(end+3600000-7*86400000).toISOString();
    issue.collectedAt=new Date(end+3900000).toISOString();
    assert.throws(()=>validateIssue(issue),/Friday 08:00/);
  }
});

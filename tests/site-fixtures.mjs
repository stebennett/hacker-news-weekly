import { adaptLegacyReviewed, HEADINGS } from '../scripts/content.mjs';
// Synthetic research exists ONLY in tests, never production data/.
export function legacy() {
  const item = (id) => ({id,title:id === '1' ? '<script>alert("x")</script> & research' : 'Ask HN: test only',score:100,comments:10,created:'2026-11-10T12:00:00Z',url:'',story:'Synthetic Story <img src=x onerror=alert(1)>.',discussion:'Synthetic discussion & themes.'});
  return {start:'2026-11-06T08:00:00Z',end:'2026-11-13T08:00:00Z',sections:HEADINGS.map((heading,i) => ({heading,items:i < 2 ? [item('1')] : i === 2 ? [item('2')] : []}))};
}
export function metadata() {
  const provenance = id => ({sources:[{kind:'discussion',url:`https://news.ycombinator.com/item?id=${id}`}],availability:{story:{status:'text-post',note:'Original text post was reviewed.'},discussion:{status:'unavailable',note:'Discussion could not be retrieved; unverified.'}}});
  return {issueId:'2026-11-13',scheduledAt:'2026-11-13T08:00:00Z',collectedAt:'2026-11-13T08:05:00Z',qualifyingCounts:[1,1,1,0],itemSources:{'1':provenance('1'),'2':provenance('2')}};
}
export const fixture = () => adaptLegacyReviewed(legacy(),metadata());

import { loadStoryArchive } from './tags.mjs';
const {issues,revisions,records,registry} = await loadStoryArchive();
console.log(`Validated ${issues.length} issues, ${revisions.length} retained revisions, ${records.length} stories and ${registry.tags.length} tags`);

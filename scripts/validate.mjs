import { loadArchive } from './content.mjs';
const {issues,revisions} = await loadArchive();
console.log(`Validated ${issues.length} issues and ${revisions.length} retained revisions`);

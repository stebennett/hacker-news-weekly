import { readFile, writeFile } from 'node:fs/promises';
import { adaptLegacyReviewed, stableJSON } from './content.mjs';
// Explicit destination, exclusive create: corrections/retention require deliberate handling.
const [legacyPath, metadataPath, destination] = process.argv.slice(2);
if (!destination) throw new Error('Usage: npm run import:legacy -- reviewed.json metadata.json destination.json');
const issue = adaptLegacyReviewed(JSON.parse(await readFile(legacyPath,'utf8')), JSON.parse(await readFile(metadataPath,'utf8')));
await writeFile(destination, stableJSON(issue), {flag:'wx',mode:0o644});
console.log(`Imported ${issue.issueId} revision ${issue.revision}`);

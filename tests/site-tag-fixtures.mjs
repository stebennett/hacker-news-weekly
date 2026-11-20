import { mkdtemp, mkdir, writeFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { stableJSON } from '../scripts/content.mjs';
import { fixture } from './site-fixtures.mjs';

// Synthetic tags and issues stay inside test temporary directories.
export const tagRegistry = () => ({schemaVersion:1,tags:[{slug:'ai',label:'AI'}]});

export function recordsFor(issues, tagSlugs = ['ai']) {
  const references = new Map();
  for (const issue of issues) for (const id of Object.keys(issue.items)) {
    if (!references.has(id)) references.set(id, new Set());
    references.get(id).add(issue.issueId);
  }
  return [...references].map(([id, issueIds]) => ({
    schemaVersion:1, id, tagSlugs:[...tagSlugs], issueIds:[...issueIds].sort()
  }));
}

export function taggedFixture() {
  const issues = [fixture()];
  return {issues, revisions:[], registry:tagRegistry(), records:recordsFor(issues)};
}

export async function writeTagArchive(t, archive = taggedFixture()) {
  const dataDir = await mkdtemp(path.join(tmpdir(), 'twihn-tags-'));
  t.after(() => rm(dataDir, {recursive:true,force:true}));
  for (const name of ['issues','revisions','stories']) await mkdir(path.join(dataDir, name));
  for (const issue of archive.issues) {
    await writeFile(path.join(dataDir, 'issues', `${issue.issueId}.json`), stableJSON(issue));
  }
  for (const revision of archive.revisions) {
    const dir = path.join(dataDir, 'revisions', revision.issueId);
    await mkdir(dir, {recursive:true});
    await writeFile(path.join(dir, `v${revision.revision}.json`), stableJSON(revision));
  }
  await writeFile(path.join(dataDir, 'tags.json'), stableJSON(archive.registry));
  for (const record of archive.records) {
    await writeFile(path.join(dataDir, 'stories', `${record.id}.json`), stableJSON(record));
  }
  return dataDir;
}

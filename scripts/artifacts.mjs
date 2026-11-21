import { readFile, readdir, mkdir, writeFile } from 'node:fs/promises';
import { createHash } from 'node:crypto';
import path from 'node:path';
import { stableJSON, schema } from './content.mjs';
import { loadStoryArchive, tagArchivePages, tagSchemaSourceBytes } from './tags.mjs';
export const digest = bytes => createHash('sha256').update(bytes).digest('hex');
export async function inventory(root, prefix='') {
  const files = [];
  for (const entry of await readdir(path.join(root,prefix),{withFileTypes:true})) {
    const name = path.posix.join(prefix,entry.name);
    if (entry.isDirectory()) files.push(...await inventory(root,name));
    else if (entry.isFile()) files.push(name);
    else throw new Error('Nonregular artifact');
  }
  return files.sort();
}
export async function expectedData(dataDir) {
  const {sourceBytes,...archive} = await loadStoryArchive(dataDir,{includeSourceBytes:true});
  const {issues,revisions,registry,records,stories} = archive;
  const data = {'data/index.json':stableJSON(issues.map(x => ({issueId:x.issueId,revision:x.revision,scheduledAt:x.scheduledAt,url:`/issues/${x.issueId}/`,dataUrl:`/data/issues/${x.issueId}.json`}))),'data/latest.json':stableJSON(issues[0] ?? null),'schema/issue.schema.json':stableJSON(schema),'data/tags.json':sourceBytes.registry};
  for (const [name,bytes] of Object.entries(tagSchemaSourceBytes())) data[`schema/${name}.schema.json`] = bytes;
  for (const record of records) data[`data/stories/${record.id}.json`] = sourceBytes.records.get(record.id);
  for (const issue of issues) data[`data/issues/${issue.issueId}.json`] = stableJSON(issue);
  for (const issue of revisions) data[`data/revisions/${issue.issueId}/v${issue.revision}.json`] = stableJSON(issue);
  return {...archive,data,pages:tagArchivePages(stories,registry)};
}
export async function exportData(dist, expected) {
  const {data,issues} = expected ?? await expectedData();
  for (const [name,content] of Object.entries(data)) {await mkdir(path.dirname(path.join(dist,name)),{recursive:true}); await writeFile(path.join(dist,name),content);}
  return issues;
}
export async function makeManifest(dist, sourceRevision, issues) {
  if (!/^[A-Za-z0-9._-]{1,128}$/.test(sourceRevision)) throw new Error('Invalid TWIHN_SOURCE_REVISION');
  const artifacts = {};
  for (const file of await inventory(dist)) if (file !== 'build-manifest.json') artifacts[file] = digest(await readFile(path.join(dist,file)));
  return {schemaVersion:1,sourceRevision,issues:issues.map(x => ({issueId:x.issueId,revision:x.revision,sha256:artifacts[`data/issues/${x.issueId}.json`]})),artifacts};
}

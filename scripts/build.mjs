import { spawnSync } from 'node:child_process';
import { writeFile } from 'node:fs/promises';
import { expectedData, exportData, makeManifest } from './artifacts.mjs';
import { stableJSON } from './content.mjs';
import { verifyDist } from './verify-dist.mjs';
// Validate complete story coverage BEFORE Astro can clear or write output.
const expected = await expectedData();
const result = spawnSync(process.execPath,['node_modules/astro/bin/astro.mjs','build'],{stdio:'inherit',env:process.env,timeout:180000});
if (result.error) throw result.error;
if (result.status !== 0) process.exit(result.status ?? 1);
const dist = process.env.TWIHN_OUT_DIR || 'dist';
const issues = await exportData(dist,expected);
// Explicit recorded input, NOT artifact commit HEAD. Local default is deterministic.
await writeFile(`${dist}/build-manifest.json`,stableJSON(await makeManifest(dist,process.env.TWIHN_SOURCE_REVISION || 'unversioned',issues)));
await verifyDist(dist);

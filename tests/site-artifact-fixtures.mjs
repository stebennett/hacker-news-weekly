import { cp, mkdir, symlink, writeFile } from 'node:fs/promises';
import { spawnSync } from 'node:child_process';
import path from 'node:path';

// Both outDir AND Astro's project-local prerender/cache state must be isolated.
export async function isolatedBuildRoot(parent) {
  const root=path.join(parent,'build-root');
  await mkdir(root,{recursive:true});
  for (const name of ['src','scripts','schema','public','package.json','tsconfig.json']) await cp(name,path.join(root,name),{recursive:true});
  await symlink(path.resolve('node_modules'),path.join(root,'node_modules'),'dir');
  await cp('astro.config.mjs',path.join(root,'base-astro.config.mjs'));
  await writeFile(path.join(root,'astro.config.mjs'),`import config from './base-astro.config.mjs';\nexport default {...config,cacheDir:'./.cache/astro',vite:{...config.vite,cacheDir:'.cache/vite'}};\n`);
  return root;
}

export const runBuild = (root,data,dist,revision='tag-route-fixture') => spawnSync(process.execPath,['scripts/build.mjs'],{
  cwd:root,encoding:'utf8',timeout:60000,
  env:{...process.env,TWIHN_DATA_DIR:data,TWIHN_OUT_DIR:dist,TWIHN_SOURCE_REVISION:revision}
});

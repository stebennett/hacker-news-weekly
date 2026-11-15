import { defineConfig } from 'astro/config';
export default defineConfig({ outDir: process.env.TWIHN_OUT_DIR || './dist', site: 'https://twihn.com', output: 'static', build: { inlineStylesheets: 'never' }, vite: { build: { assetsInlineLimit: 0 } } });

import { build } from 'esbuild';
import fs from 'node:fs';
import path from 'node:path';
import zlib from 'node:zlib';

const BUDGET_KB = Number(process.env.BUDGET_KB || 200);
const out = 'dist';
fs.rmSync(out, { recursive: true, force: true });
fs.mkdirSync(path.join(out, 'assets'), { recursive: true });

const js = await build({
  entryPoints: ['src/main.js'], bundle: true, splitting: true, format: 'esm', minify: true, target: ['es2020'],
  outdir: path.join(out, 'assets'), entryNames: '[name]-[hash]', chunkNames: 'chunk-[name]-[hash]',
  metafile: true, legalComments: 'none',
});
const css = await build({ entryPoints: ['src/styles.css'], bundle: true, minify: true, write: false, outdir: out });

const gz = (b) => zlib.gzipSync(b, { level: 9 }).length;
const entryFile = Object.keys(js.metafile.outputs).find((f) => js.metafile.outputs[f].entryPoint === 'src/main.js');
const entryName = path.basename(entryFile);
const cssText = css.outputFiles[0].text;
const lazyChunks = Object.keys(js.metafile.outputs).filter((f) => f !== entryFile && f.endsWith('.js')).map((f) => path.basename(f));
const preloadTags = [
  `<link rel="modulepreload" href="/assets/${entryName}">`,
  ...lazyChunks.map((c) => `<link rel="modulepreload" href="/assets/${c}">`),
].join('\n');

let html = fs.readFileSync('src/index.html', 'utf8');
html = html
  .replace('<!--INLINE_CSS-->', `<style>${cssText}</style>`)
  .replace('<!--PRELOAD-->', preloadTags)
  .replace('<!--SCRIPT-->', `<script type="module" src="/assets/${entryName}"></script>`);
fs.writeFileSync(path.join(out, 'index.html'), html);

const rows = Object.entries(js.metafile.outputs).map(([f]) => {
  const buf = fs.readFileSync(f);
  return { file: path.relative(out, f), raw: buf.length, gzip: gz(buf), initial: path.basename(f) === entryName };
});
const initial = gz(Buffer.from(html)) + rows.filter((r) => r.initial).reduce((s, r) => s + r.gzip, 0);
const total = initial + rows.filter((r) => !r.initial).reduce((s, r) => s + r.gzip, 0);
for (const r of rows) console.log(`${r.initial ? 'initial ' : 'lazy    '} ${r.file.padEnd(36)} ${String(r.raw).padStart(7)} B raw ${String(r.gzip).padStart(6)} B gzip`);
console.log(`initial transfer (html+css+js, gzip): ${(initial / 1024).toFixed(1)} KB | with lazy chunks: ${(total / 1024).toFixed(1)} KB | budget ${BUDGET_KB} KB`);
if (total / 1024 > BUDGET_KB) { console.error('BUNDLE BUDGET EXCEEDED'); process.exit(1); }

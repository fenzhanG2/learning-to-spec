const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const esbuild = require('esbuild');

const root = path.resolve(__dirname, '..');
const web = path.join(root, 'session_spec', 'web');
const digest = filename => crypto.createHash('sha256').update(fs.readFileSync(filename)).digest('hex');

async function main() {
  await esbuild.build({
    absWorkingDir: root,
    entryPoints: ['session_spec/web/render-story.cjs'],
    outfile: 'session_spec/web/render-story.bundle.cjs',
    bundle: true,
    platform: 'node',
    target: 'node18',
    minify: true,
    legalComments: 'linked',
    charset: 'utf8'
  });
  const sources = ['package-lock.json', ...fs.readdirSync(web)
    .filter(name => name.endsWith('.cjs') && name !== 'render-story.bundle.cjs')
    .map(name => 'session_spec/web/' + name)].sort();
  const lock = JSON.parse(fs.readFileSync(path.join(root, 'package-lock.json'), 'utf8'));
  const notices = [];
  for (const [directory, dependency] of Object.entries(lock.packages)) {
    if (!directory.startsWith('node_modules/') || dependency.dev) continue;
    const packageRoot = path.join(root, directory);
    const metadata = JSON.parse(fs.readFileSync(path.join(packageRoot, 'package.json'), 'utf8'));
    const licenses = fs.readdirSync(packageRoot).filter(name => /^licen[cs]e(?:[-.]|$)/i.test(name));
    if (!licenses.length) throw new Error('Missing license for ' + metadata.name);
    for (const name of licenses) {
      notices.push(metadata.name + '@' + metadata.version + ' — ' + name + '\n\n' + fs.readFileSync(path.join(packageRoot, name), 'utf8'));
    }
    const nested = path.join(packageRoot, 'dist', 'dagre.cjs.LEGAL.txt');
    if (fs.existsSync(nested)) notices.push(metadata.name + ' prebundled notices\n\n' + fs.readFileSync(nested, 'utf8'));
  }
  fs.writeFileSync(path.join(root, 'third_party/rendering-dependencies.txt'), notices.join('\n\n---\n\n').trimEnd() + '\n');
  sources.push('third_party/rendering-dependencies.txt');
  const outputs = ['render-story.bundle.cjs', 'render-story.bundle.cjs.LEGAL.txt'];
  const manifest = {
    schema: 'renderer-bundle/v1',
    sources: Object.fromEntries(sources.map(name => [name, digest(path.join(root, name))])),
    outputs: Object.fromEntries(outputs.map(name => [name, digest(path.join(web, name))]))
  };
  fs.writeFileSync(path.join(web, 'renderer-bundle.json'), JSON.stringify(manifest, null, 2) + '\n');
  process.stdout.write('Built the standalone renderer with preserved dependency notices.\n');
}

main().catch(error => { process.stderr.write(String(error) + '\n'); process.exitCode = 1; });

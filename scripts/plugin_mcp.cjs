const { spawn, spawnSync } = require('node:child_process');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const candidates = process.platform === 'win32' ? [['python', []], ['py', ['-3']], ['python3', []]] : [['python3', []], ['python', []]];
let runtime;
for (const [command, args] of candidates) {
  const result = spawnSync(command, [...args, '-X', 'utf8', '-c', 'import sys; print(sys.executable); sys.exit(sys.version_info < (3, 10))'], { timeout: 10000, windowsHide: true, encoding: 'utf8', env: { ...process.env, PYTHONIOENCODING: 'utf-8' }, stdio: ['ignore', 'pipe', 'ignore'] });
  const executable = (result.stdout || '').trim();
  if (result.status === 0 && path.isAbsolute(executable) && fs.existsSync(executable)) {
    runtime = executable;
    break;
  }
}
if (!runtime) {
  process.stderr.write('Learning to Spec needs Python 3.10+ on PATH. No runtime was installed automatically.\n');
  process.exit(1);
}
try {
  for (const name of ['COPILOT_HOME', 'LEARNING_TO_SPEC_HOME']) {
    const value = process.env[name];
    if (value && !value.startsWith('~')) process.env[name] = path.resolve(value);
  }
  const pluginRoot = fs.realpathSync(path.join(__dirname, '..'));
  let neutral;
  for (const locate of [() => os.homedir(), () => os.tmpdir()]) {
    try {
      const candidate = fs.realpathSync(locate());
      const relative = path.relative(pluginRoot, candidate);
      if (relative === '..' || relative.startsWith('..' + path.sep) || path.isAbsolute(relative)) {
        process.chdir(candidate);
        neutral = candidate;
        break;
      }
    } catch {}
  }
  if (!neutral) throw new Error('No external working directory');
} catch {
  process.stderr.write('Learning to Spec needs an accessible working directory outside its plugin installation.\n');
  process.exit(1);
}
const child = spawn(runtime, ['-X', 'utf8', path.join(__dirname, 'plugin_mcp.py')], { stdio: 'inherit', windowsHide: true, env: { ...process.env, PYTHONIOENCODING: 'utf-8' } });
child.on('error', () => { process.stderr.write('Learning to Spec MCP could not start.\n'); process.exitCode = 1; });
child.on('exit', code => { process.exitCode = code ?? 1; });
for (const signal of ['SIGTERM', 'SIGINT']) process.on(signal, () => child.kill(signal));

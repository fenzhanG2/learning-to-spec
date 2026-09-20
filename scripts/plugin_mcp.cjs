const { spawn, spawnSync } = require('node:child_process');
const path = require('node:path');

const candidates = process.platform === 'win32' ? [['python', []], ['py', ['-3']], ['python3', []]] : [['python3', []], ['python', []]];
const runtime = candidates.find(([command, args]) => {
  const result = spawnSync(command, [...args, '-c', 'import sys; sys.exit(sys.version_info < (3, 10))'], { timeout: 10000, windowsHide: true, stdio: 'ignore' });
  return result.status === 0;
});
if (!runtime) {
  process.stderr.write('Learning to Spec needs Python 3.10+ on PATH. No runtime was installed automatically.\n');
  process.exit(1);
}
const child = spawn(runtime[0], [...runtime[1], '-X', 'utf8', path.join(__dirname, 'plugin_mcp.py')], { stdio: 'inherit', windowsHide: true });
child.on('error', () => { process.stderr.write('Learning to Spec MCP could not start.\n'); process.exitCode = 1; });
child.on('exit', code => { process.exitCode = code ?? 1; });
for (const signal of ['SIGTERM', 'SIGINT']) process.on(signal, () => child.kill(signal));

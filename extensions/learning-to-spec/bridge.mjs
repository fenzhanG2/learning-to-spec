import { spawn, spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import os from 'node:os';

export function startBridge(sessionId, options = {}) {
  const root = fileURLToPath(new URL('../../', import.meta.url));
  const candidates = process.platform === 'win32' ? [['python', []], ['py', ['-3']], ['python3', []]] : [['python3', []], ['python', []]];
  let executable;
  for (const [command, args] of candidates) {
    const result = spawnSync(command, [...args, '-X', 'utf8', '-c', 'import sys; print(sys.executable); sys.exit(sys.version_info < (3, 10))'], { timeout: 10000, windowsHide: true, encoding: 'utf8', stdio: ['ignore', 'pipe', 'ignore'] });
    if (result.status === 0 && path.isAbsolute((result.stdout || '').trim())) {
      executable = result.stdout.trim();
      break;
    }
  }
  if (!executable) throw new Error('Learning to Spec requires Python 3.10+. No software was installed automatically.');
  const child = spawn(executable, ['-X', 'utf8', path.join(root, 'scripts/native_bridge.py'), '--session', sessionId], {
    windowsHide: true, cwd: os.homedir(), stdio: ['pipe', 'pipe', 'pipe'], env: { ...process.env, ...options.env, PYTHONIOENCODING: 'utf-8' },
  });
  const pending = new Map();
  let sequence = 0;
  let stopped = false;
  let received = Buffer.alloc(0);
  const fail = () => {
    stopped = true;
    for (const { reject, timer } of pending.values()) {
      clearTimeout(timer);
      reject(new Error('The native export service stopped. Reopen /to-spec; do not repeat an upload.'));
    }
    pending.clear();
  };
  child.stderr.on('data', () => {});
  child.on('error', fail);
  child.on('exit', fail);
  function responseLine(line) {
    let response;
    try { response = JSON.parse(line); } catch { fail(); child.stdin.end(); return; }
    const request = pending.get(response.id);
    if (!request) return;
    pending.delete(response.id);
    clearTimeout(request.timer);
    if (response.error) {
      const error = new Error(response.error);
      if (['operation_failed', 'artifact_name_invalid', 'artifact_exists', 'artifact_auth_required', 'artifact_plan_failed', 'publication_unconfirmed'].includes(response.error_code)) error.code = response.error_code;
      request.reject(error);
    }
    else request.resolve(response.result);
  }
  child.stdout.on('data', chunk => {
    if (stopped) return;
    received = Buffer.concat([received, chunk]);
    if (received.length > 64 * 1024 * 1024) { fail(); child.stdin.end(); return; }
    for (;;) {
      const boundary = received.indexOf(10);
      if (boundary < 0) break;
      const line = received.subarray(0, boundary).toString('utf8');
      received = received.subarray(boundary + 1);
      responseLine(line);
      if (stopped) return;
    }
  });
  return {
    call(operation, data = {}) {
      if (stopped) return Promise.reject(new Error('Native export service is not running.'));
      const id = ++sequence;
      const payload = JSON.stringify({ id, operation, data }) + '\n';
      if (Buffer.byteLength(payload) > 32 * 1024 * 1024 + 65536) return Promise.reject(new Error('Conversation exceeds the export size limit; nothing was truncated.'));
      return new Promise((resolve, reject) => {
        const timer = setTimeout(() => { pending.delete(id); reject(new Error('Native request timed out. Its outcome is unknown; do not repeat it automatically.')); }, 30000);
        pending.set(id, { resolve, reject, timer });
        child.stdin.write(payload, error => { if (error) fail(); });
      });
    },
    close() { child.stdin.end(); },
  };
}

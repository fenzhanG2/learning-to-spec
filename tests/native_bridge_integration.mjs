import assert from 'node:assert/strict';
import { spawn, spawnSync } from 'node:child_process';
import { randomUUID } from 'node:crypto';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { startBridge } from '../extensions/learning-to-spec/bridge.mjs';
import { createWorkflow } from '../extensions/learning-to-spec/workflow.mjs';

const filename = fileURLToPath(import.meta.url);
const systemRoot = process.env.SystemRoot || process.env.SYSTEMROOT || 'C:\\Windows';
const systemEnvironment = process.platform === 'win32'
  ? { SystemRoot: systemRoot, WINDIR: systemRoot, ComSpec: path.join(systemRoot, 'System32', 'cmd.exe'), PATHEXT: '.COM;.EXE;.BAT;.CMD',
    SYSTEMDRIVE: path.parse(systemRoot).root.replace(/[\\/]$/, ''), LOGONSERVER: '\\\\NATIVE-BRIDGE-TEST', USERDOMAIN: 'NATIVE-BRIDGE-TEST', USERNAME: 'fixture' }
  : {};

function pythonExecutable() {
  const candidates = process.platform === 'win32' ? [['python', []], ['py', ['-3']], ['python3', []]] : [['python3', []], ['python', []]];
  for (const [command, prefix] of candidates) {
    const result = spawnSync(command, [...prefix, '-I', '-S', '-c', 'import sys; print(sys.executable); sys.exit(sys.version_info < (3, 10))'], {
      env: { ...systemEnvironment, PATH: process.env.PATH || process.env.Path || '' },
      windowsHide: true, encoding: 'utf8', timeout: 10000, stdio: ['ignore', 'pipe', 'pipe'],
    });
    const executable = (result.stdout || '').trim();
    if (result.status === 0 && path.isAbsolute(executable)) return executable;
  }
  throw new Error('These real bridge tests require Python 3.10+; no software or provider is installed by the test.');
}

async function isolatedEnvironment(root, executable) {
  const directories = ['home', 'copilot', 'temp', 'appdata', 'localappdata', 'xdg-config', 'xdg-cache', 'runtime/profile'];
  for (const directory of directories) await fs.mkdir(path.join(root, directory), { recursive: true });
  const home = path.join(root, 'home');
  return {
    ...systemEnvironment,
    PATH: [path.dirname(executable), path.dirname(process.execPath), ...(process.platform === 'win32'
      ? [path.join(path.dirname(executable), 'Library', 'bin'), path.join(systemRoot, 'System32'), systemRoot] : ['/usr/bin', '/bin'])].join(path.delimiter),
    HOME: home, USERPROFILE: home, ...(process.platform === 'win32'
      ? { HOMEDRIVE: path.parse(home).root.replace(/[\\/]$/, ''), HOMEPATH: home.slice(2) } : {}),
    COPILOT_HOME: path.join(root, 'copilot'), LEARNING_TO_SPEC_HOME: path.join(root, 'runtime', 'profile'),
    TEMP: path.join(root, 'temp'), TMP: path.join(root, 'temp'), TMPDIR: path.join(root, 'temp'),
    APPDATA: path.join(root, 'appdata'), LOCALAPPDATA: path.join(root, 'localappdata'),
    XDG_CONFIG_HOME: path.join(root, 'xdg-config'), XDG_CACHE_HOME: path.join(root, 'xdg-cache'),
    PYTHONNOUSERSITE: '1', PYTHONDONTWRITEBYTECODE: '1', PYTHONIOENCODING: 'utf-8', NO_COLOR: '1',
  };
}

async function worker(scenario, root, sessionId) {
  const allowedEnvironment = new Set(['SystemRoot', 'WINDIR', 'ComSpec', 'PATHEXT', 'SYSTEMDRIVE', 'LOGONSERVER', 'USERDOMAIN', 'USERNAME', 'PATH', 'HOME', 'USERPROFILE',
    'HOMEDRIVE', 'HOMEPATH', 'COPILOT_HOME', 'LEARNING_TO_SPEC_HOME', 'TEMP', 'TMP', 'TMPDIR', 'APPDATA', 'LOCALAPPDATA',
    'XDG_CONFIG_HOME', 'XDG_CACHE_HOME', 'PYTHONNOUSERSITE', 'PYTHONDONTWRITEBYTECODE', 'PYTHONIOENCODING', 'NO_COLOR'].map(name => name.toLowerCase()));
  assert.deepEqual(Object.keys(process.env).filter(name => !allowedEnvironment.has(name.toLowerCase())), []);
  if (process.platform === 'win32') {
    assert.equal(process.env.USERNAME, 'fixture');
    assert.equal(process.env.USERDOMAIN, 'NATIVE-BRIDGE-TEST');
    assert.equal(process.env.LOGONSERVER, '\\\\NATIVE-BRIDGE-TEST');
  }
  assert.equal(path.resolve(os.homedir()), path.join(root, 'home'));
  assert.equal(process.env.COPILOT_HOME, path.join(root, 'copilot'));
  const privacyMode = scenario === 'smart-capture-only' ? 'llm' : 'full';
  const events = [
    { type: 'session.start', data: { sessionId } },
    { type: 'user.message', data: { content: 'Synthetic CI correction for example@example.test; retain the failed check.' } },
  ];
  const calls = [];
  const dialogs = [];
  const logs = [];
  let eventReads = 0;
  let capture;
  let confirmations = 0;
  const realBridge = startBridge(sessionId);
  const bridge = { call: async (operation, data = {}) => {
    assert.ok(['pending_review', 'capture', 'scan'].includes(operation), `Forbidden test operation: ${operation}`);
    if (operation === 'scan') {
      assert.equal(data.privacy_mode, privacyMode);
      assert.equal(data.detection, privacyMode === 'full' ? 'none' : 'copilot');
      assert.equal(data.semantic, privacyMode === 'llm');
      assert.equal(data.pipeline, 'abstract-then-redact/v1');
      calls.push({ operation, forwarded: false });
      throw new Error('TEST_BLOCKED_PROVIDER_DISPATCH');
    }
    calls.push({ operation, forwarded: true });
    const response = await realBridge.call(operation, data);
    if (operation === 'pending_review') assert.deepEqual(response, { review: null });
    if (operation === 'capture') {
      capture = response;
      assert.equal(response.session_id, sessionId);
      assert.equal(response.privacy_mode, privacyMode);
      assert.equal(response.capture_order, 'abstract-then-redact/v1');
      assert.match(response.sha256, /^[a-f0-9]{64}$/);
      if (scenario === 'missing-mode-fault') {
        const faulty = { ...response };
        delete faulty.privacy_mode;
        return faulty;
      }
    }
    return response;
  } };
  const session = {
    sessionId, capabilities: { ui: { elicitation: true, canvases: false } }, openCanvases: [],
    getEvents: async () => { eventReads += 1; return events; },
    log: async message => { logs.push(message); },
    ui: {
      elicitation: async request => {
        dialogs.push(request);
        assert.equal(dialogs.length, 1);
        assert.deepEqual(request.requestedSchema.required, ['readers', 'delivery', 'privacyMode']);
        return { action: 'accept', content: { readers: 'both', delivery: 'local', privacyMode } };
      },
      confirm: async message => {
        confirmations += 1;
        throw new Error('Unexpected final export confirmation before blocked abstraction');
      },
      input: async () => { throw new Error('Unexpected input dialog'); },
      select: async () => { throw new Error('Unexpected select dialog'); },
    },
  };
  const workflow = createWorkflow({ getSession: () => session, getBridge: () => bridge,
    wait: milliseconds => new Promise(resolve => setTimeout(resolve, Math.min(milliseconds, 20))) });
  try {
    if (scenario === 'missing-mode-fault') {
      await assert.rejects(workflow.run({ sessionId }), /did not confirm the requested privacy mode/);
      assert.deepEqual(calls.map(call => call.operation), ['pending_review', 'capture']);
      assert.equal(confirmations, 0);
    } else {
      assert.ok(['smart-capture-only', 'full-capture-only'].includes(scenario));
      await assert.rejects(workflow.run({ sessionId }), /TEST_BLOCKED_PROVIDER_DISPATCH/);
      assert.deepEqual(calls.filter(call => call.forwarded).map(call => call.operation), ['pending_review', 'capture']);
      assert.equal(confirmations, 0);
    }
    assert.equal(eventReads, 1);
    assert.ok(capture);
    assert.equal(JSON.stringify(logs).includes('example@example.test'), false);
    console.log(JSON.stringify({ scenario, sessionId, privacyMode, calls, confirmations,
      captureMode: capture.privacy_mode, captureOrder: capture.capture_order, sourceSha256: capture.sha256,
      environmentKeys: Object.keys(process.env).sort(), models: 0, generationDispatches: 0,
      faultInjection: scenario === 'missing-mode-fault' ? 'Removed mode only after real Python capture response' : null,
      providerBoundary: 'Real capture only; both modes now abstract with a provider, so preparation is blocked by the test before IPC. Not an abstraction or provider-failure test.' }));
  } finally {
    realBridge.close();
  }
}

async function runIsolated(scenario, context) {
  const temporaryParent = await fs.realpath(os.tmpdir());
  const root = await fs.mkdtemp(path.join(temporaryParent, 'lts-native-integration-'));
  const sessionId = randomUUID();
  const environment = await isolatedEnvironment(root, pythonExecutable());
  let finishedNormally = false;
  try {
    const result = await new Promise((resolve, reject) => {
      const child = spawn(process.execPath, [filename, '--bridge-test-worker', scenario, root, sessionId], {
        cwd: root, env: environment, windowsHide: true, detached: process.platform !== 'win32', stdio: ['ignore', 'pipe', 'pipe'],
      });
      let stdout = '', stderr = '', timedOut = false;
      const timer = setTimeout(() => {
        timedOut = true;
        if (process.platform === 'win32') {
          spawnSync(path.join(systemRoot, 'System32', 'taskkill.exe'), ['/PID', String(child.pid), '/T', '/F'], {
            env: environment, windowsHide: true, stdio: 'ignore', timeout: 10000,
          });
        } else {
          try { process.kill(-child.pid, 'SIGKILL'); } catch {}
        }
        reject(new Error(`Isolated bridge test timed out; fixture preserved at ${root}`));
      }, 60000);
      child.stdout.on('data', chunk => { stdout += chunk.toString('utf8'); });
      child.stderr.on('data', chunk => { stderr += chunk.toString('utf8'); });
      child.on('error', error => { clearTimeout(timer); reject(error); });
      child.on('close', (code, signal) => { clearTimeout(timer); resolve({ code, signal, stdout, stderr, timedOut }); });
    });
    finishedNormally = result.code === 0 && !result.timedOut;
    assert.equal(result.code, 0, `${result.stderr || result.stdout}\nFixture: ${root}`);
    const receipt = JSON.parse(result.stdout.trim());
    assert.equal(receipt.sessionId, sessionId);
    context.diagnostic(JSON.stringify(receipt));
  } finally {
    if (finishedNormally) {
      assert.equal(path.dirname(path.resolve(root)), temporaryParent);
      assert.ok(path.basename(root).startsWith('lts-native-integration-'));
      await fs.rm(root, { recursive: true, force: true });
    }
  }
}

if (process.argv[2] === '--bridge-test-worker') {
  await worker(process.argv[3], path.resolve(process.argv[4]), process.argv[5]);
} else {
  const { default: test } = await import('node:test');
  for (const scenario of ['full-capture-only', 'missing-mode-fault', 'smart-capture-only']) {
    test(`real native bridge integration: ${scenario}`, { concurrency: false, timeout: 75000 }, context => runIsolated(scenario, context));
  }
}

const assert = require('node:assert/strict');
const { spawnSync } = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../scripts/plugin_mcp.cjs'), 'utf8');
const simulatedPath = path.win32;
const pluginRoot = 'C:\\fixture\\plugin';
const executable = 'C:\\fixture-tools\\caf\u00e9\\python.exe';

function inspect({ unusableHome = false, insidePlugin = false, firstProbeFails = false, actualProbe = false } = {}) {
  let directory = pluginRoot;
  const outcome = { exit: null, errors: [], chdirs: [], probes: [], spawns: [] };
  const home = insidePlugin ? pluginRoot : 'C:\\fixture-home';
  const temporary = insidePlugin ? simulatedPath.join(pluginRoot, 'temp') : 'C:\\fixture-temp';
  const environment = { ...process.env, COPILOT_HOME: '../fixture-profile', LEARNING_TO_SPEC_HOME: '../runtime', PYTHONIOENCODING: 'cp1252' };
  const modules = {
    'node:path': { ...simulatedPath, resolve: (...parts) => simulatedPath.resolve(directory, ...parts) },
    'node:fs': { existsSync: filename => filename === executable, realpathSync: filename => filename },
    'node:os': { homedir: () => home, tmpdir: () => temporary },
    'node:child_process': {
      spawnSync(command, args, options) {
        outcome.probes.push({ command, args, options });
        if (firstProbeFails && outcome.probes.length === 1) return { status: 1, stdout: '' };
        if (!actualProbe) return { status: 0, stdout: executable + '\r\n' };
        const commandIndex = args.indexOf('-c');
        const script = 'import sys; sys.executable=' + JSON.stringify(executable) + '; ' + args[commandIndex + 1];
        return spawnSync(process.env.TEST_PYTHON_EXECUTABLE, ['-B', '-S', '-X', 'utf8', '-c', script], options);
      },
      spawn(command, args, options) {
        outcome.spawns.push({ command, args, options, cwd: directory });
        return { on() {}, kill() {} };
      }
    }
  };
  const controlledProcess = {
    platform: 'win32', env: environment, on() {},
    stderr: { write: message => outcome.errors.push(message) },
    exit(code) { outcome.exit = code; throw new Error('controlled exit'); },
    chdir(candidate) {
      outcome.chdirs.push(candidate);
      if (unusableHome && candidate === home) throw new Error('EACCES');
      directory = candidate;
    }
  };
  try {
    vm.runInNewContext(source, { require: name => modules[name], process: controlledProcess, __dirname: simulatedPath.join(pluginRoot, 'scripts') }, { timeout: 15000 });
  } catch (error) {
    if (outcome.exit === null) throw error;
  }
  return outcome;
}

test('UTF-8 discovery overrides cp1252 for an actual Python-produced Unicode path', () => {
  const outcome = inspect({ actualProbe: true });
  assert.equal(outcome.spawns.length, 1);
  assert.equal(outcome.spawns[0].command, executable);
  assert.equal(outcome.probes[0].options.env.PYTHONIOENCODING, 'utf-8');
  assert.equal(outcome.spawns[0].options.env.PYTHONIOENCODING, 'utf-8');
});

test('unusable home falls back to external temp without changing relative profiles', () => {
  const outcome = inspect({ unusableHome: true });
  assert.equal(outcome.spawns.length, 1);
  assert.deepEqual(outcome.chdirs, ['C:\\fixture-home', 'C:\\fixture-temp']);
  assert.equal(outcome.spawns[0].options.env.COPILOT_HOME, 'C:\\fixture\\fixture-profile');
  assert.equal(outcome.spawns[0].options.env.LEARNING_TO_SPEC_HOME, 'C:\\fixture\\runtime');
});

test('both neutral candidates within the installation fail closed', () => {
  const outcome = inspect({ insidePlugin: true });
  assert.equal(outcome.spawns.length, 0);
  assert.equal(outcome.exit, 1);
  assert.equal(outcome.chdirs.length, 0);
});

test('candidate fallback keeps the discovered absolute Python instead of re-resolving py', () => {
  const outcome = inspect({ firstProbeFails: true });
  assert.equal(outcome.probes[1].command, 'py');
  assert.equal(outcome.probes[1].args[0], '-3');
  assert.equal(outcome.spawns[0].command, executable);
  assert.equal(outcome.spawns[0].options.stdio, 'inherit');
  assert.equal(outcome.errors.length, 0);
});

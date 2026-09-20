const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');
const script = fs.readFileSync(path.join(__dirname, '../session_spec/web/studio.js'), 'utf8');
const html = fs.readFileSync(path.join(__dirname, '../session_spec/web/studio.html'), 'utf8');

class Element {
  constructor(tag = 'div') {
    this.tag = tag; this.children = []; this.listeners = {}; this.dataset = {};
    this.value = ''; this.hidden = false; this.disabled = false; this.classList = { toggle() {} };
  }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = children; }
  get firstElementChild() { return this.children[0]; }
  setAttribute(name, value) { this[name] = value; }
  removeAttribute(name) { delete this[name]; }
  addEventListener(name, listener) { this.listeners[name] = listener; }
  scrollIntoView() {}
  remove() {}
}

async function fixture() {
  const elements = {};
  for (const match of html.matchAll(/<([a-z]+)[^>]*\bid="([^"]+)"/g)) elements[match[2]] = new Element(match[1]);
  elements.detection.value = 'local'; elements.filter.value = 'all';
  const calls = [];
  const review = { review_id: 'review', audience: 'root', preferences: { readers: 'agent', destination: 'artifactstore' },
    hard_removals: {}, semantic: { status: 'not requested' }, findings: [{ id: 'personal', label: 'Private aside',
      text: 'Synthetic private aside', reason: 'Your choice', occurrences: [{}], necessity: 'uncertain', detectors: ['local'] }] };
  const delivery = { files: ['agent-spec.md', 'evidence.md'], output: 'synthetic-only', preferences: review.preferences };
  const responses = {
    '/api/sessions': { sessions: [{ id: 'selected', title: 'Synthetic', bytes: 100 }], durable_jobs: true }, '/api/jobs': { jobs: [] },
    '/api/scan': { job: 'synthetic' }, '/api/status?job=synthetic': { status: 'done', stage: 'generate', delivery_result: delivery },
    '/api/review?job=synthetic': review, '/api/generate': { job: 'synthetic' },
    '/api/package': { package_id: 'package', files: { 'agent-spec.md': 'hash' }, findings: [{ id: 'residual', category: 'contact', file: 'agent-spec.md', text: 'example.invalid' }] },
    '/api/plan': { plan_id: 'plan', site: 'synthetic', destination: 'test.invalid', audience: 'root', files: ['agent-spec.md'], viewer_policy: {}, note: 'Synthetic policy' },
    '/api/publish': { job: 'synthetic' },
  };
  const walk = element => [element, ...element.children.flatMap(walk)];
  const document = { body: new Element('body'), getElementById: name => elements[name], createElement: tag => new Element(tag),
    querySelectorAll: selector => Object.values(elements).flatMap(walk).filter(element => selector === '[data-finding]' ? element.dataset.finding : selector === '[data-file]' ? element.dataset.file : ['button', 'input', 'select', 'textarea'].includes(element.tag)) };
  const context = vm.createContext({ URLSearchParams, URL, document, location: { hash: '#access=fixture' },
    history: { replaceState() {} }, setTimeout, clearTimeout, fetch: async (route, options) => {
      calls.push({ route, data: options.body ? JSON.parse(options.body) : undefined });
      const value = typeof responses[route] === 'function' ? await responses[route]() : responses[route];
      return { ok: !(value instanceof Error), json: async () => value instanceof Error ? { error: value.message } : value };
    } });
  vm.runInContext(script, context);
  await new Promise(resolve => setImmediate(resolve));
  elements.session.value = 'selected'; elements.readers.value = 'agent'; elements.delivery.value = 'artifactstore'; elements.audience.value = 'root';
  const click = name => elements[name].listeners.click();
  return { context, elements, calls, responses, document, click };
}

test('one generate action approves chosen details; unresolved rewrites block it', async () => {
  const example = await fixture();
  await example.click('scan');
  assert.equal(example.elements.generate.disabled, true);
  const card = example.elements.findings.children[0];
  const choice = card.children.find(child => child.tag === 'select');
  const replacement = card.children.find(child => child.tag === 'textarea');
  choice.value = 'generalize'; choice.listeners.change();
  assert.equal(example.elements.generate.disabled, true);
  replacement.value = 'A general note'; replacement.listeners.input();
  assert.equal(example.elements.generate.disabled, false);
  await example.click('generate');
  assert.equal(example.calls.filter(call => call.route === '/api/generate').length, 1);
  assert.equal(example.calls.find(call => call.route === '/api/generate').data.confirmed, true);
  assert.equal(example.elements['output-panel'].hidden, false);
  assert.equal(example.calls.some(call => ['/api/choices', '/api/approve', '/api/publish'].includes(call.route)), false);
});

test('generation failure restores the privacy screen and supports explicit retry', async () => {
  const example = await fixture();
  await example.click('scan');
  vm.runInContext("state.choices = { personal: { action: 'remove' } }", example.context);
  example.responses['/api/status?job=synthetic'] = { status: 'error', error: 'Synthetic model failure' };
  await example.click('generate');
  assert.equal(example.elements['review-panel'].hidden, false);
  assert.equal(example.elements.generate.disabled, false);
  assert.equal(example.elements.status.textContent, 'Synthetic model failure');
  assert.equal(example.elements.generate.textContent, 'Retry generation →');
  assert.equal(example.elements['error-text'].textContent, 'Synthetic model failure');
  assert.equal(example.elements['error-details'].hidden, false);
});

test('authentication errors are concise without discarding diagnostics or auto-retrying', async () => {
  const example = await fixture();
  const diagnostic = 'Copilot failed: Authentication token found but could not be validated (401): Bad credentials. ' + '<script>not executable</script>'.repeat(12);
  example.responses['/api/status?job=synthetic'] = { stage: 'generate', status: 'error', error: diagnostic,
    approved_choices: { choices: { personal: { action: 'remove' } } } };
  await vm.runInContext("reopenJob('synthetic')", example.context);
  assert.match(example.elements.status.textContent, /configured GitHub host\/account/);
  assert.ok(example.elements.status.textContent.length < 200);
  assert.equal(example.elements['error-text'].textContent, diagnostic);
  assert.equal(example.elements['error-details'].open, false);
  assert.equal(example.elements.generate.textContent, 'Retry generation →');
  assert.equal(example.calls.some(call => call.route === '/api/generate'), false);
  const choice = example.elements.findings.children[0].children.find(child => child.tag === 'select');
  choice.value = 'keep'; choice.listeners.change();
  assert.equal(example.elements.generate.textContent, 'Generate spec →');
  vm.runInContext("status('Ready')", example.context);
  assert.equal(example.elements['error-details'].hidden, true);
  assert.equal(example.elements['error-text'].textContent, '');
});

test('lost generation connection keeps choices, names the saved job and never retries blindly', async () => {
  const example = await fixture();
  await example.click('scan');
  vm.runInContext("state.choices = { personal: { action: 'remove' } }", example.context);
  example.responses['/api/generate'] = () => { throw new TypeError('Failed to fetch'); };
  await example.click('generate');
  assert.match(example.elements.status.textContent, /reopen learning-to-spec job synthetic/);
  assert.match(example.elements.status.textContent, /may already have started/);
  assert.match(example.elements.status.textContent, /Unsubmitted choices/);
  assert.equal(example.elements.remaining.textContent, 'Reopen Studio to continue');
  assert.equal(example.elements.generate.disabled, true);
  assert.equal(example.elements.generate.textContent, 'Generate spec →');
  assert.equal(vm.runInContext('state.choices.personal.action', example.context), 'remove');
  await example.click('generate');
  assert.equal(example.calls.filter(call => call.route === '/api/generate').length, 1);
});

test('lost polling connection is not presented as failed generation', async () => {
  const example = await fixture();
  await example.click('scan');
  vm.runInContext("state.choices = { personal: { action: 'remove' } }", example.context);
  example.responses['/api/status?job=synthetic'] = () => { throw new TypeError('NetworkError'); };
  await example.click('generate');
  assert.equal(example.elements['review-panel'].hidden, false);
  assert.match(example.elements.status.textContent, /Check saved status/);
  assert.equal(example.elements.generate.disabled, true);
  assert.equal(example.elements.generate.textContent, 'Generate spec →');
});

test('lost upload connection invalidates approval and blocks duplicate writes', async () => {
  const example = await fixture();
  vm.runInContext("state.job = 'synthetic'; show('output')", example.context);
  await example.click('prepare-share');
  const choice = example.document.querySelectorAll('[data-finding]')[0];
  choice.value = 'keep'; choice.listeners.change();
  example.responses['/api/publish'] = () => { throw new TypeError('Failed to fetch'); };
  await example.click('publish');
  assert.equal(example.elements.publish.disabled, true);
  assert.equal(vm.runInContext('state.plan', example.context), null);
  assert.match(example.elements.status.textContent, /may already have started/);
  await example.click('publish');
  assert.equal(example.calls.filter(call => call.route === '/api/publish').length, 1);
});

test('download connection failure gives recovery without triggering generation', async () => {
  const example = await fixture();
  vm.runInContext("state.job = 'synthetic'; show('output')", example.context);
  example.responses['/api/file?job=synthetic&name=agent-spec.md'] = () => { throw new TypeError('Failed to fetch'); };
  await vm.runInContext("perform(() => file('agent-spec.md'))", example.context);
  assert.match(example.elements.status.textContent, /Studio connection lost/);
  assert.equal(example.calls.some(call => call.route === '/api/generate'), false);
});

test('a response interrupted after request acceptance also blocks blind retries', async () => {
  const example = await fixture();
  await example.click('scan');
  vm.runInContext("state.choices = { personal: { action: 'remove' } }; fetch = async () => ({ ok: true, json: async () => { throw new TypeError('Connection closed'); } });", example.context);
  await example.click('generate');
  assert.match(example.elements.status.textContent, /response incomplete/);
  assert.equal(example.elements.generate.disabled, true);
  assert.equal(example.elements.generate.textContent, 'Generate spec →');
});

test('recovery guidance never includes a private access capability', async () => {
  const example = await fixture();
  vm.runInContext("parameters.set('job', 'a'.repeat(32)); fetch = async () => { throw new TypeError('Failed to fetch'); };", example.context);
  await vm.runInContext("perform(() => request('/api/jobs'))", example.context);
  assert.match(example.elements.status.textContent, /job a{32}/);
  assert.doesNotMatch(example.elements.status.textContent, /access|fixture|Bearer/);
  assert.doesNotMatch(example.elements['error-text'].textContent, /access|fixture|Bearer/);
});

test('manual Studio recovery never promises native job persistence', async () => {
  const example = await fixture();
  vm.runInContext("state.durableJobs = false; state.job = 'synthetic';", example.context);
  example.responses['/api/jobs'] = () => { throw new TypeError('Failed to fetch'); };
  await vm.runInContext("perform(() => request('/api/jobs'))", example.context);
  assert.match(example.elements.status.textContent, /Restart the manual Studio command/);
  assert.match(example.elements.status.textContent, /Manual job IDs cannot be reopened/);
  assert.doesNotMatch(example.elements.status.textContent, /reopen learning-to-spec job/);
});

test('a new review never claims privacy decisions have already been saved', async () => {
  const example = await fixture();
  example.responses['/api/status?job=synthetic'] = { stage: 'scan', status: 'done' };
  await vm.runInContext("reopenJob('synthetic')", example.context);
  assert.match(example.elements.status.textContent, /Choose how to handle/);
  assert.equal(example.elements.generate.disabled, true);
});

test('upload preparation grants no approval; explicit residual choices and final click are required', async () => {
  const example = await fixture();
  vm.runInContext("state.job = 'synthetic'; show('output')", example.context);
  await example.click('prepare-share');
  assert.equal(example.calls.some(call => ['/api/approve', '/api/publish'].includes(call.route)), false);
  assert.equal(example.elements.publish.disabled, true);
  const choice = example.document.querySelectorAll('[data-finding]')[0];
  choice.value = 'keep'; choice.listeners.change();
  assert.equal(example.elements.publish.disabled, false);
  example.responses['/api/status?job=synthetic'] = { status: 'done', result: { status: 'verified', files: {} } };
  await example.click('publish');
  const submission = example.calls.find(call => call.route === '/api/publish').data;
  assert.equal(submission.publish_intent, true);
  assert.deepEqual(submission.acknowledged, ['residual']);
  assert.equal('reviewed_all_files' in submission, false);
  assert.equal(example.elements.publish.hidden, true);
  assert.equal(example.elements['verify-publish'].hidden, false);
});

test('late destination response cannot re-enable a stale upload', async () => {
  const example = await fixture();
  vm.runInContext("state.job = 'synthetic'; state.manifest = {};", example.context);
  example.elements['site-name'].value = 'before-edit';
  let finish;
  example.responses['/api/plan'] = () => new Promise(resolve => { finish = resolve; });
  const pending = vm.runInContext('refreshPlan()', example.context);
  vm.runInContext('invalidatePlan()', example.context);
  finish({ plan_id: 'stale' });
  await pending;
  assert.equal(example.elements.publish.disabled, true);
  assert.equal(vm.runInContext('state.plan', example.context), null);
});

test('late destination response cannot restore a plan after disconnection', async () => {
  const example = await fixture();
  vm.runInContext("state.job = 'synthetic'; state.manifest = {};", example.context);
  example.elements['site-name'].value = 'synthetic';
  let finish;
  example.responses['/api/plan'] = () => new Promise(resolve => { finish = resolve; });
  const pending = vm.runInContext('refreshPlan()', example.context);
  example.responses['/api/jobs'] = () => { throw new TypeError('Connection lost'); };
  await vm.runInContext("perform(() => request('/api/jobs'))", example.context);
  finish({ plan_id: 'stale' });
  await pending;
  assert.equal(vm.runInContext('state.plan', example.context), null);
  assert.equal(example.elements['destination-card'].hidden, true);
  assert.equal(example.elements.publish.disabled, true);
});

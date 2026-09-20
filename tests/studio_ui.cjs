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
    '/api/sessions': { sessions: [{ id: 'selected', title: 'Synthetic', bytes: 100 }] }, '/api/jobs': { jobs: [] },
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

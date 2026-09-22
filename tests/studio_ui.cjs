const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');
const { createHash, webcrypto } = require('node:crypto');
const contentHash = content => createHash('sha256').update(content).digest('hex');
const snapshotBinding = files => ({ id: 'a'.repeat(64), files: Object.fromEntries([...files, 'deliverables.zip'].map(name => [name, contentHash('Synthetic approved export')])) });
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
  addEventListener(name, listener) {
    const previous = this.listeners[name];
    this.listeners[name] = event => { const result = previous?.(event); return listener(event) ?? result; };
  }
  click() { this.clicked = true; return this.listeners.click?.(); }
  scrollIntoView() {}
  remove() {}
}

async function fixture() {
  const elements = {};
  for (const match of html.matchAll(/<([a-z]+)[^>]*\bid="([^"]+)"/g)) elements[match[2]] = new Element(match[1]);
  for (const match of html.matchAll(/<button\b([^>]*\bdata-file="([^"]+)"[^>]*)>/g)) {
    const identifier = /\bid="([^"]+)"/.exec(match[1])?.[1] || `file-${match[2]}`;
    elements[identifier] ||= new Element('button');
    elements[identifier].dataset.file = match[2];
  }
  elements.detection.value = 'local'; elements.filter.value = 'all';
  const calls = [];
  const urls = new Map();
  let urlSequence = 0;
  const review = { review_id: 'review', audience: 'root', preferences: { readers: 'agent', destination: 'artifactstore' },
    hard_removals: {}, semantic: { status: 'not requested' }, findings: [{ id: 'personal', label: 'Private aside',
      text: 'Synthetic private aside', reason: 'Your choice', occurrences: [{}], necessity: 'uncertain', detectors: ['local'] }] };
  const delivery = { files: ['agent-spec.md', 'evidence.md'], output: 'synthetic-only', preferences: review.preferences };
  delivery.snapshot = snapshotBinding(delivery.files);
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
  const context = vm.createContext({ URLSearchParams, Blob, AbortController, Error, TypeError, crypto: webcrypto, Uint8Array, document, location: { hash: '#access=fixture' },
    URL: { createObjectURL(content) { const url = `blob:synthetic-${++urlSequence}`; urls.set(url, content); return url; }, revokeObjectURL(url) { urls.delete(url); } },
    history: { replaceState() {} }, setTimeout(callback, delay) { const timer = setTimeout(callback, delay); if (delay === 60000) timer.unref(); return timer; }, clearTimeout, fetch: async (route, options) => {
      calls.push({ route, headers: options.headers, data: options.body ? JSON.parse(options.body) : undefined });
      const value = typeof responses[route] === 'function' ? await responses[route](route, options) : responses[route];
      if (value instanceof Response) {
        if (!value.headers.has('X-Delivery-Snapshot')) value.headers.set('X-Delivery-Snapshot', 'a'.repeat(64));
        return value;
      }
      if (route.startsWith('/api/file?') && value === undefined) return new Response('Synthetic approved export', { headers: { 'X-Delivery-Snapshot': 'a'.repeat(64) } });
      return { ok: !(value instanceof Error), json: async () => value instanceof Error ? { error: value.message } : value };
    } });
  vm.runInContext(script, context);
  await new Promise(resolve => setImmediate(resolve));
  elements.session.value = 'selected'; elements.readers.value = 'agent'; elements.delivery.value = 'artifactstore'; elements.audience.value = 'root';
  const click = name => elements[name].listeners.click();
  const fileButton = name => document.querySelectorAll('[data-file]').find(button => button.dataset.file === name);
  return { context, elements, calls, responses, document, click, fileButton, urls, delivery };
}

async function completedOutput(example, delivery = example.delivery) {
  delivery.snapshot ||= snapshotBinding(delivery.files);
  example.context.deliveryFixture = delivery;
  return vm.runInContext("state.job = 'synthetic'; renderOutput(deliveryFixture); show('output'); state.downloadSnapshot.ready", example.context);
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

test('empty local and contextual scans never claim privacy clearance', async () => {
  for (const mode of ['not_run', 'reviewed']) {
    const example = await fixture();
    example.responses['/api/review?job=synthetic'].findings = [];
    example.responses['/api/review?job=synthetic'].semantic = { status: mode };
    await example.click('scan');
    assert.match(example.elements['review-summary'].textContent, mode === 'reviewed' ? /does not establish/ : /Indirect personal disclosures/);
    const empty = example.elements.findings.children[0];
    assert.equal(empty.children[0].textContent, 'No findings is not a privacy clearance');
    assert.match(empty.children[1].textContent, /preserve technical failures/);
    assert.equal(example.calls.some(call => call.route === '/api/generate'), false);
  }
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

test('completed selected exports cache automatically and explicit offline downloads make zero requests', async () => {
  const example = await fixture();
  await example.click('scan');
  vm.runInContext("state.choices = { personal: { action: 'remove' } }", example.context);
  await example.click('generate');
  await vm.runInContext('state.downloadSnapshot.ready', example.context);
  assert.equal(vm.runInContext('state.downloadSnapshot.blobs.size', example.context), 3);
  assert.equal(example.calls.filter(call => call.route.startsWith('/api/file?')).length, 3);
  example.responses['/api/jobs'] = () => { throw new TypeError('Runtime stopped'); };
  await vm.runInContext("perform(() => request('/api/jobs'))", example.context);
  const requests = example.calls.length;
  assert.equal(example.fileButton('agent-spec.md').disabled, false);
  example.fileButton('agent-spec.md').click();
  assert.equal(example.calls.length, requests);
  const link = example.document.body.children.at(-1);
  assert.equal(link.download, 'agent-spec.md');
  assert.equal(await example.urls.get(link.href).text(), 'Synthetic approved export');
  assert.match(example.elements.status.textContent, /snapshot.*No server contact or revalidation/);
  assert.match(example.elements.status.textContent, /Server status is unknown/);
  assert.match(example.elements.status.textContent, /generation, upload and retry remain blocked/);
  assert.match(example.elements.status.textContent, /download requested/);
  assert.doesNotMatch(example.elements.status.textContent, /download completed|connection restored/i);
  assert.equal(example.elements['error-details'].hidden, false);
  assert.match(example.elements['error-text'].textContent, /Studio connection lost/);
  assert.ok(vm.runInContext('state.connectionError', example.context));
  for (const identifier of ['generate', 'publish', 'verify-publish', 'open-folder', 'scan']) {
    assert.equal(example.elements[identifier].disabled, true);
    await example.click(identifier);
  }
  assert.equal(example.calls.length, requests);
  assert.equal(example.elements['output-selection'].textContent, '3 downloads ready in this tab.');
  assert.match(example.elements['snapshot-details'].textContent, /not live or revalidated/);
});

test('readiness stays brief while snapshot limits and caveats live in download help', async () => {
  const example = await fixture();
  let finish;
  example.responses['/api/file?job=synthetic&name=agent-spec.md'] = () => new Promise(resolve => { finish = resolve; });
  const pending = completedOutput(example);
  assert.equal(example.elements['output-selection'].textContent, 'Preparing downloads… 0/3 ready.');
  finish(new Response('Synthetic approved export'));
  await pending;
  assert.equal(example.elements['output-selection'].textContent, '3 downloads ready in this tab.');
  assert.ok(example.elements['output-selection'].textContent.length < 100);
  assert.doesNotMatch(example.elements['output-selection'].textContent, /MiB|seconds|revalidated|reload/);
  assert.match(example.elements['snapshot-details'].textContent, /8 MiB per file, 24 MiB total, 15 seconds/);
  assert.match(example.elements['snapshot-details'].textContent, /not live or revalidated/);
  assert.match(example.elements['snapshot-details'].textContent, /disappear on reload/);
  const help = html.match(/<details\b[^>]*><summary>Local files & download help<\/summary>[\s\S]*?<\/details>/)?.[0];
  assert.ok(help);
  assert.match(help, /id="snapshot-details"/);
  assert.doesNotMatch(help, /^<details\b[^>]*\bopen\b/);
});

test('choice, setup, job and regeneration changes discard cached downloads and object URLs', async () => {
  for (const change of ['choice', 'setup', 'job', 'regenerate']) {
    const example = await fixture();
    await example.click('scan');
    await completedOutput(example);
    example.fileButton('agent-spec.md').click();
    assert.equal(example.urls.size, 1);
    if (change === 'choice') {
      const control = example.elements.findings.children[0].children.find(child => child.tag === 'select');
      control.value = 'keep'; control.listeners.change();
    } else if (change === 'setup') {
      example.elements.readers.value = 'human'; example.elements.readers.listeners.change();
    } else if (change === 'job') {
      vm.runInContext("rememberJob('different')", example.context);
    } else {
      example.responses['/api/generate'] = new Error('Synthetic generation refused');
      await example.click('generate');
    }
    assert.equal(vm.runInContext('state.downloadSnapshot', example.context), null, change);
    assert.equal(example.urls.size, 0, change);
    assert.equal(example.fileButton('agent-spec.md').disabled, true, change);
    const downloads = example.document.body.children.length;
    example.fileButton('agent-spec.md').click();
    assert.equal(example.document.body.children.length, downloads, change);
    assert.equal(example.elements['human-preview'].srcdoc, '', change);
    assert.equal(example.elements['output-selection'].textContent, 'Downloads need preparing.', change);
    assert.equal(example.elements['snapshot-details'].textContent, '', change);
  }
});

test('late old-job export success or failure cannot populate or disconnect a new snapshot', async () => {
  for (const failed of [false, true]) {
    const example = await fixture();
    let finish;
    let reject;
    example.responses['/api/file?job=synthetic&name=agent-spec.md'] = () => new Promise((resolve, fail) => { finish = resolve; reject = fail; });
    const old = completedOutput(example);
    vm.runInContext("rememberJob('new-job')", example.context);
    example.context.nextDelivery = { files: ['human-spec.html'], preferences: { readers: 'human', destination: 'local' }, output: 'new' };
    example.context.nextDelivery.snapshot = snapshotBinding(example.context.nextDelivery.files);
    await vm.runInContext('renderOutput(nextDelivery); state.downloadSnapshot.ready', example.context);
    if (failed) reject(new TypeError('Old runtime request failed'));
    else finish(new Response('OLD EXPORT MUST BE DISCARDED'));
    await old;
    assert.equal(vm.runInContext('state.downloadSnapshot.job', example.context), 'new-job');
    assert.equal(vm.runInContext("state.downloadSnapshot.blobs.has('agent-spec.md')", example.context), false);
    assert.equal(vm.runInContext('state.connectionError', example.context), null);
    assert.equal(example.elements['human-preview'].srcdoc, 'Synthetic approved export');
  }
});

test('same-job replacement generation discards an older in-flight export', async () => {
  const example = await fixture();
  let finish;
  const route = '/api/file?job=synthetic&name=agent-spec.md';
  example.responses[route] = () => new Promise(resolve => { finish = resolve; });
  const old = completedOutput(example);
  vm.runInContext('invalidateDownloads()', example.context);
  example.responses[route] = () => new Response('NEW APPROVED EXPORT');
  example.delivery.snapshot.files['agent-spec.md'] = contentHash('NEW APPROVED EXPORT');
  await completedOutput(example);
  finish(new Response('OLD APPROVED EXPORT'));
  await old;
  assert.equal(await vm.runInContext("state.downloadSnapshot.blobs.get('agent-spec.md').text()", example.context), 'NEW APPROVED EXPORT');
});

test('oversize and incomplete exports remain unavailable while successful partial snapshots survive', async () => {
  const example = await fixture();
  example.responses['/api/file?job=synthetic&name=agent-spec.md'] = () => new Response('x', { headers: { 'content-length': '99999999' } });
  example.responses['/api/file?job=synthetic&name=deliverables.zip'] = () => new Response('x', { headers: { 'content-length': '10' } });
  await completedOutput(example);
  assert.equal(vm.runInContext('state.downloadSnapshot.blobs.size', example.context), 1);
  assert.equal(example.fileButton('agent-spec.md').disabled, true);
  assert.equal(example.fileButton('deliverables.zip').disabled, true);
  assert.equal(example.fileButton('evidence.md').disabled, false);
  assert.equal(example.elements['output-selection'].textContent, '1/3 downloads ready. Others unavailable.');
  assert.match(example.elements['snapshot-details'].textContent, /Incomplete\/unavailable/);
  vm.runInContext('disconnected()', example.context);
  const requests = example.calls.length;
  example.fileButton('evidence.md').click();
  assert.equal(example.calls.length, requests);
  assert.equal(example.document.body.children.at(-1).download, 'evidence.md');
});

test('streamed bytes obey per-file and aggregate limits without trusting content-length', async () => {
  const example = await fixture();
  vm.runInContext('SNAPSHOT_LIMITS.fileBytes = 4; SNAPSHOT_LIMITS.totalBytes = 6', example.context);
  example.responses['/api/file?job=synthetic&name=agent-spec.md'] = () => new Response('12345');
  example.responses['/api/file?job=synthetic&name=evidence.md'] = () => new Response('1234');
  example.delivery.snapshot.files['evidence.md'] = contentHash('1234');
  example.responses['/api/file?job=synthetic&name=deliverables.zip'] = () => new Response('123');
  await completedOutput(example);
  assert.equal(vm.runInContext('state.downloadSnapshot.bytes', example.context), 4);
  assert.equal(vm.runInContext('state.downloadSnapshot.blobs.size', example.context), 1);
  assert.equal(example.fileButton('evidence.md').disabled, false);
  assert.equal(example.fileButton('deliverables.zip').disabled, true);
});

test('snapshot deadline aborts remaining fetches and retains only complete prior files', async () => {
  const example = await fixture();
  vm.runInContext('SNAPSHOT_LIMITS.milliseconds = 10', example.context);
  example.responses['/api/file?job=synthetic&name=evidence.md'] = (route, options) => new Promise((resolve, reject) => {
    options.signal.addEventListener('abort', () => reject(new Error('Synthetic bounded abort')));
  });
  await completedOutput(example);
  assert.equal(vm.runInContext('state.downloadSnapshot.pending', example.context), false);
  assert.equal(vm.runInContext('state.downloadSnapshot.blobs.size', example.context), 1);
  assert.equal(example.calls.filter(call => call.route.includes('deliverables.zip')).length, 0);
  assert.equal(example.elements['output-selection'].textContent, '1/3 downloads ready. Others unavailable.');
  assert.match(example.elements['snapshot-details'].textContent, /Incomplete\/unavailable/);
});

test('reader allowlist never prefetches private files or an unselected reader, including ZIP', async () => {
  for (const readers of ['agent', 'human', 'unknown']) {
    const example = await fixture();
    await completedOutput(example, { files: ['agent-spec.md', 'human-spec.html', 'review.json', 'source.json'],
      preferences: { readers, destination: 'local' }, output: 'synthetic' });
    const files = example.calls.filter(call => call.route.startsWith('/api/file?')).map(call => new URL(call.route, 'http://synthetic.invalid').searchParams.get('name'));
    assert.deepEqual(files, []);
    assert.equal(example.fileButton('deliverables.zip').hidden, true);
    assert.equal(example.elements['output-selection'].textContent, 'Some selected downloads are unavailable. See download help.');
    assert.match(example.elements['snapshot-details'].textContent, /Unrecognized reader\/file selection was not cached/);
  }
});

test('stale saved-job status cannot stage exports after another job was selected', async () => {
  const example = await fixture();
  let finish;
  example.responses['/api/status?job=old-job'] = () => new Promise(resolve => { finish = resolve; });
  const pending = vm.runInContext("reopenJob('old-job')", example.context);
  vm.runInContext("rememberJob('new-job')", example.context);
  finish({ status: 'done', delivery_result: example.delivery });
  await pending;
  assert.equal(example.calls.some(call => call.route.startsWith('/api/file?')), false);
  assert.equal(vm.runInContext('state.downloadSnapshot', example.context), null);
});

test('late old response-body chunks cannot populate a replacement snapshot', async () => {
  const example = await fixture();
  let stream;
  example.responses['/api/file?job=synthetic&name=agent-spec.md'] = () => new Response(new ReadableStream({ start(controller) { stream = controller; } }));
  const old = completedOutput(example);
  await new Promise(resolve => setImmediate(resolve));
  vm.runInContext('invalidateDownloads()', example.context);
  example.responses['/api/file?job=synthetic&name=agent-spec.md'] = () => new Response('NEW BODY');
  example.delivery.snapshot.files['agent-spec.md'] = contentHash('NEW BODY');
  await completedOutput(example);
  stream.enqueue(new TextEncoder().encode('OLD BODY'));
  stream.close();
  await old;
  assert.equal(await vm.runInContext("state.downloadSnapshot.blobs.get('agent-spec.md').text()", example.context), 'NEW BODY');
});

test('runtime loss during automatic staging retains the first export and blocks mutations', async () => {
  const example = await fixture();
  example.responses['/api/file?job=synthetic&name=evidence.md'] = () => { throw new TypeError('Runtime stopped while caching'); };
  await completedOutput(example);
  assert.match(example.elements.status.textContent, /Studio connection lost/);
  assert.equal(vm.runInContext('state.downloadSnapshot.blobs.size', example.context), 1);
  assert.equal(example.fileButton('agent-spec.md').disabled, false);
  assert.equal(example.elements.generate.disabled, true);
  assert.equal(example.elements.publish.disabled, true);
  const requests = example.calls.length;
  example.fileButton('agent-spec.md').click();
  await example.click('generate');
  await example.click('publish');
  assert.equal(example.calls.length, requests);
});

test('late generation acknowledgement cannot fetch a different selected job or stage its output', async () => {
  const example = await fixture();
  await example.click('scan');
  vm.runInContext("state.choices = { personal: { action: 'remove' } }", example.context);
  let finish;
  example.responses['/api/generate'] = () => new Promise(resolve => { finish = resolve; });
  const pending = example.click('generate');
  vm.runInContext("rememberJob('new-job')", example.context);
  finish({ completed: true });
  await pending;
  assert.equal(example.calls.some(call => call.route === '/api/status?job=new-job'), false);
  assert.equal(example.calls.some(call => call.route.startsWith('/api/file?')), false);
  assert.equal(vm.runInContext('state.downloadSnapshot', example.context), null);
  assert.equal(example.calls.filter(call => call.route === '/api/generate').length, 1);
});

test('external same-job switches and wrong sibling hashes never form a mixed cache', async () => {
  for (const changedIdentity of [true, false]) {
    const example = await fixture();
    for (const name of ['evidence.md', 'deliverables.zip']) {
      example.responses[`/api/file?job=synthetic&name=${name}`] = () => new Response(`NEW-GENERATION-${name}`, {
        headers: { 'X-Delivery-Snapshot': (changedIdentity ? 'b' : 'a').repeat(64) },
      });
    }
    await completedOutput(example);
    assert.equal(vm.runInContext('state.downloadSnapshot.blobs.size', example.context), 1);
    assert.equal(await vm.runInContext("state.downloadSnapshot.blobs.get('agent-spec.md').text()", example.context), 'Synthetic approved export');
    assert.equal(example.fileButton('evidence.md').disabled, true);
    assert.equal(example.fileButton('deliverables.zip').disabled, true);
    assert.equal(example.elements['output-selection'].textContent, '1/3 downloads ready. Others unavailable.');
    for (const call of example.calls.filter(call => call.route.startsWith('/api/file?'))) {
      assert.equal(call.headers['X-Delivery-Snapshot'], 'a'.repeat(64));
    }
    vm.runInContext('disconnected()', example.context);
    const requests = example.calls.length;
    example.fileButton('agent-spec.md').click();
    assert.equal(example.calls.length, requests);
  }
});

test('missing manifest binding or browser hashing refuses staging without requests', async () => {
  for (const unavailable of ['binding', 'hashing']) {
    const example = await fixture();
    if (unavailable === 'binding') delete example.delivery.snapshot;
    else example.context.crypto = {};
    example.context.deliveryFixture = example.delivery;
    await vm.runInContext("state.job = 'synthetic'; renderOutput(deliveryFixture); state.downloadSnapshot.ready", example.context);
    assert.equal(example.calls.some(call => call.route.startsWith('/api/file?')), false);
    assert.equal(example.fileButton('agent-spec.md').disabled, true);
    assert.match(example.elements['snapshot-details'].textContent, /Verified generation identity or browser hashing unavailable/);
  }
});

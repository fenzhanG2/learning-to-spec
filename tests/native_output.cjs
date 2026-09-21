const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');
const { createHash, webcrypto } = require('node:crypto');

const script = fs.readFileSync(path.join(__dirname, '../session_spec/web/native-output.js'), 'utf8');
const html = fs.readFileSync(path.join(__dirname, '../session_spec/web/native-output.html'), 'utf8');
const contentHash = content => createHash('sha256').update(content).digest('hex');

class Element {
  constructor(tag, attributes = '') {
    this.tag = tag;
    this.hidden = /\bhidden\b/.test(attributes);
    this.disabled = /\bdisabled\b/.test(attributes);
    this.listeners = {};
    this.children = [];
  }
  append(child) { this.children.push(child); }
  addEventListener(name, listener) { this.listeners[name] = listener; }
  click() { this.clicked = true; return this.listeners.click?.(); }
  remove() { this.removed = true; }
  focus() { this.focused = true; }
}

function fixture(options = {}) {
  const bodies = {
    'human-spec.html': '<!doctype html><html><body>Synthetic story</body></html>',
    'agent-spec.md': '# Synthetic handoff\r\nInspect before editing.\r\n',
    'evidence.md': '# Synthetic evidence\n',
    'deliverables.zip': Buffer.from([0, 80, 75, 255]),
    ...options.bodies,
  };
  const selected = options.selected || ['agent-spec.md', 'evidence.md'];
  const manifest = Object.hasOwn(options, 'manifest') ? options.manifest : {
    snapshot_id: 'a'.repeat(64),
    local: { folder: 'C:\\Synthetic fixture\\deliverables', files: Object.fromEntries([...selected, 'deliverables.zip'].map(name => [name, `C:\\Synthetic fixture\\deliverables\\${name}`])) },
    files: selected.map(name => ({ name, sha256: contentHash(bodies[name]) })),
    bundle: { sha256: contentHash(bodies['deliverables.zip']) },
  };
  const elements = {};
  for (const match of html.matchAll(/<([a-z][a-z0-9]*)\b([^>]*\bid="([^"]+)"[^>]*)>/g)) {
    elements[match[3]] = new Element(match[1], match[2]);
  }
  const document = {
    body: new Element('body'),
    getElementById: name => elements[name],
    createElement: tag => new Element(tag),
    createRange: () => ({ selectNodeContents(element) { element.selected = true; } }),
  };
  const calls = [];
  const timers = new Map();
  const urls = new Map();
  let sequence = 0;
  const fallback = route => {
    if (route === '/api/delivery') return { ok: true, json: async () => manifest };
    if (route === '/api/open') return { ok: true, json: async () => ({ status: 'dispatched' }) };
    const name = new URL(route, 'http://fixture.invalid').searchParams.get('name');
    return new Response(bodies[name], { headers: { 'Content-Type': 'application/octet-stream' } });
  };
  const context = vm.createContext({
    URLSearchParams, Blob, AbortController, Uint8Array,
    crypto: options.crypto || webcrypto,
    location: { hash: options.hash ?? '#access=synthetic-read-only-capability' },
    document,
    navigator: { clipboard: { writeText: options.copy || (async value => { elements['output-path'].copied = value; }) } },
    window: { getSelection: () => ({ removeAllRanges() {}, addRange() {} }) },
    URL: {
      createObjectURL(blob) { const value = `blob:synthetic-${++sequence}`; urls.set(value, blob); return value; },
      revokeObjectURL(value) { urls.delete(value); },
    },
    setTimeout(callback, delay) { const identifier = ++sequence; timers.set(identifier, { callback, delay }); return identifier; },
    clearTimeout(identifier) { timers.delete(identifier); },
    fetch: async (route, request) => {
      calls.push({ route, request });
      return options.fetch ? options.fetch(route, request, fallback) : fallback(route);
    },
  });
  const ready = vm.runInContext(script, context);
  return { ready, elements, calls, timers, urls, document, bodies };
}

test('live progress updates through review and export before enabling verified downloads', async () => {
  const phases = ['draft', 'privacy', 'review', 'export', 'ready'];
  let phaseIndex = 0;
  const example = fixture({ hash: '#access=synthetic-read-only-capability&progress=1',
    fetch: (route, request, fallback) => route === '/api/progress'
      ? { ok: true, json: async () => ({ phase: phases[phaseIndex], elapsed_seconds: 42, limit_seconds: 300 }) }
      : fallback(route),
  });
  for (const label of ['Drafting the story', 'Checking facts and privacy', 'Your choices are ready', 'Exporting your files']) {
    await new Promise(resolve => setImmediate(resolve));
    assert.ok(example.elements['progress-title'].textContent.includes(label));
    assert.match(example.elements['progress-clock'].textContent, /42s.*5-minute limit.*approval time excluded/);
    assert.equal(example.elements.bundle.disabled, true);
    assert.ok(example.calls.every(call => call.route === '/api/progress'));
    const timer = [...example.timers.entries()].find(([, value]) => value.delay === 3000);
    assert.ok(timer);
    example.timers.delete(timer[0]);
    phaseIndex++;
    timer[1].callback();
  }
  await example.ready;
  assert.equal(example.elements.progress.hidden, true);
  assert.equal(example.elements.agent.disabled, false);
  assert.equal(example.calls.filter(call => call.route === '/api/progress').length, 5);
});

test('terminal progress failure stops polling and never exposes private error text or downloads', async () => {
  for (const code of ['timeout', 'cleanup_unconfirmed', 'call_budget', 'draft_references_invalid', 'draft_structure_invalid', 'PRIVATE_FAILURE']) {
    const example = fixture({ hash: '#access=synthetic&progress=1', fetch: async () => ({ ok: true,
      json: async () => ({ phase: 'error', error_code: code, detail: 'PRIVATE_SOURCE' }),
    }) });
    await example.ready;
    assert.equal(example.calls.length, 1);
    assert.equal(example.elements.bundle.disabled, true);
    assert.equal(example.timers.size, 0);
    assert.equal(example.elements['progress-title'].textContent, 'Export stopped');
    assert.doesNotMatch(example.elements.status.textContent, /PRIVATE/);
    if (code.startsWith('draft_')) {
      assert.match(example.elements.status.textContent, /One automatic repair/);
      assert.match(example.elements.status.textContent, /\/to-spec again/);
      assert.match(example.elements.status.textContent, code === 'draft_references_invalid' ? /source citations/ : /invalid draft format/);
    }
  }
  const missing = fixture({ hash: '#progress=1' });
  await missing.ready;
  assert.equal(missing.calls.length, 0);
});

test('failed validation enables only clearly marked local draft files with incomplete privacy warning', async () => {
  const example = fixture({ hash: '#access=synthetic&progress=1', selected: ['human-spec.html', 'agent-spec.md'],
    fetch: async (route, request, fallback) => {
      if (route === '/api/progress') return { ok: true, json: async () => ({ phase: 'draft_available' }) };
      if (route === '/api/delivery') return { ok: true, json: async () => ({ ...await fallback(route).json(), kind: 'unvalidated_draft' }) };
      return fallback(route);
    },
  });
  await example.ready;
  assert.equal(example.elements['draft-warning'].hidden, false);
  assert.match(example.elements.status.textContent, /Unvalidated.*privacy incomplete.*no upload/);
  assert.match(example.elements['export-description'].textContent, /not privacy-reviewed/);
  assert.equal(example.elements.human.disabled, false);
  assert.equal(example.elements.agent.disabled, false);
  assert.equal(example.elements.evidence.hidden, true);
  assert.match(example.elements.bundle.textContent, /private draft/);
  assert.match(example.elements['human-label'].textContent, /Human draft/);
  assert.match(example.elements['evidence-hint'].textContent, /No validated evidence/);
  await example.elements.agent.click();
  assert.equal(example.calls.at(-1).route, '/api/open');
});

test('selected files use authenticated reads then explicit scoped opening; ZIP keeps exact bytes', async () => {
  const example = fixture();
  await example.ready;
  assert.deepEqual(example.calls.map(call => call.route), [
    '/api/delivery', '/api/file?name=agent-spec.md', '/api/file?name=evidence.md', '/api/file?name=deliverables.zip',
  ]);
  for (const { request } of example.calls) {
    assert.equal(request.headers.Authorization, 'Bearer synthetic-read-only-capability');
    assert.equal(request.cache, 'no-store');
    assert.equal(request.redirect, 'error');
    assert.equal(request.method, undefined);
    assert.equal(request.body, undefined);
  }
  assert.equal(example.elements.human.hidden, true);
  assert.equal(example.elements.agent.disabled, false);
  assert.equal(example.elements.evidence.disabled, false);
  assert.equal(example.elements.bundle.disabled, false);
  await example.elements.agent.click();
  const open = example.calls.at(-1);
  assert.equal(open.route, '/api/open');
  assert.equal(open.request.method, 'POST');
  assert.equal(open.request.headers.Authorization, 'Bearer synthetic-read-only-capability');
  assert.deepEqual(JSON.parse(open.request.body), { target: 'agent-spec.md', snapshot_id: 'a'.repeat(64) });
  assert.match(example.elements.status.textContent, /default app.*agent-spec.md/);
  assert.equal(example.document.body.children.length, 0);
  example.elements.bundle.click();
  const anchor = example.document.body.children.at(-1);
  assert.equal(anchor.download, 'deliverables.zip');
  assert.deepEqual(Buffer.from(await example.urls.get(anchor.href).arrayBuffer()), example.bodies['deliverables.zip']);
  assert.equal(example.calls.length, 5);
  assert.match(example.elements.status.textContent, /ZIP copy requested.*Copilot\/browser Downloads/);
  assert.equal(example.timers.size, 1);
});

test('human-only selection opens verified HTML and excludes Agent files and embedded previews', async () => {
  const example = fixture({ selected: ['human-spec.html'] });
  await example.ready;
  assert.equal(example.elements.story, undefined);
  assert.equal(example.elements.preview, undefined);
  assert.equal(example.elements.agent.hidden, true);
  assert.equal(example.elements.evidence.hidden, true);
  assert.doesNotMatch(html, /A look inside|<iframe\b|preview-note/);
  assert.doesNotMatch(script, /srcdoc|innerHTML|previewDocument/);
  assert.match(html, /id="status"[^>]*role="status"[^>]*aria-live="polite"/);
  assert.deepEqual(example.calls.map(call => call.route), [
    '/api/delivery', '/api/file?name=human-spec.html', '/api/file?name=deliverables.zip',
  ]);
  await example.elements.human.click();
  assert.equal(JSON.parse(example.calls.at(-1).request.body).target, 'human-spec.html');
  assert.equal(example.document.body.children.length, 0);
});

test('missing access capability makes no request and enables no downloads', async () => {
  const example = fixture({ hash: '' });
  await example.ready;
  assert.equal(example.calls.length, 0);
  assert.equal(example.elements.bundle.disabled, true);
  assert.equal(example.elements.agent.hidden, true);
  assert.match(example.elements.status.textContent, /Reopen.*\/to-spec/);
});

test('invalid manifests cannot fetch private paths or enable a bundle', async () => {
  const digest = contentHash('synthetic');
  for (const manifest of [
    null,
    { files: null, bundle: { sha256: digest } },
    { files: [{ name: '../review.json', sha256: digest }], bundle: { sha256: digest } },
    { files: [{ name: 'agent-spec.md', sha256: 'invalid' }], bundle: { sha256: digest } },
    { files: Array(2).fill({ name: 'agent-spec.md', sha256: digest }), bundle: { sha256: digest } },
    { files: Array(4).fill({ name: 'evidence.md', sha256: digest }), bundle: { sha256: digest } },
  ]) {
    const example = fixture({ manifest });
    await example.ready;
    assert.equal(example.calls.length, 1);
    assert.equal(example.elements.bundle.disabled, true);
    assert.equal(example.urls.size, 0);
  }
});

test('hash mismatch refuses changed bytes while verified siblings remain downloadable', async () => {
  const example = fixture({ fetch: (route, request, fallback) => route.endsWith('evidence.md')
    ? new Response('tampered synthetic evidence') : fallback(route) });
  await example.ready;
  assert.equal(example.elements.agent.disabled, false);
  assert.equal(example.elements.evidence.disabled, true);
  assert.equal(example.elements.bundle.disabled, true);
  assert.equal(example.calls.length, 3);
  assert.match(example.elements.status.textContent, /saved file changed/);
  await example.elements.agent.click();
  assert.equal(example.calls.length, 4);
  assert.equal(example.urls.size, 0);
});

test('authentication errors and disconnects never retry or dispatch mutations', async () => {
  for (const fetch of [
    async () => new Response('denied', { status: 403 }),
    async () => { throw new TypeError('Synthetic disconnected host'); },
  ]) {
    const example = fixture({ fetch });
    await example.ready;
    assert.equal(example.calls.length, 1);
    assert.equal(example.elements.bundle.disabled, true);
    assert.equal(example.timers.size, 0);
    assert.equal(example.urls.size, 0);
    assert.doesNotMatch(example.elements.status.textContent, /^Ready/);
  }
});

test('broken streams cancel and release the reader without accepting partial bytes', async () => {
  let cancelled = false;
  let released = false;
  const example = fixture({ fetch: (route, request, fallback) => route === '/api/delivery'
    ? fallback(route) : {
      ok: true,
      body: { getReader: () => ({
        async read() { throw new Error('Synthetic incomplete stream'); },
        async cancel() { cancelled = true; },
        releaseLock() { released = true; },
      }) },
    } });
  await example.ready;
  assert.equal(cancelled, true);
  assert.equal(released, true);
  assert.equal(example.elements.agent.disabled, true);
  assert.equal(example.elements.bundle.disabled, true);
  assert.equal(example.calls.length, 2);
});

test('per-file limit refuses oversized streams before enabling a download', async () => {
  const example = fixture({ bodies: { 'agent-spec.md': Buffer.alloc(8 * 1024 * 1024 + 1) } });
  await example.ready;
  assert.equal(example.calls.length, 2);
  assert.equal(example.elements.agent.disabled, true);
  assert.equal(example.elements.bundle.disabled, true);
  assert.match(example.elements.status.textContent, /exceeds the preview limit/);
});

test('aggregate limit bounds all selected file payloads including the ZIP', async () => {
  const payload = Buffer.alloc(6 * 1024 * 1024 + 1);
  const example = fixture({ selected: ['human-spec.html', 'agent-spec.md', 'evidence.md'], bodies: {
    'human-spec.html': payload, 'agent-spec.md': payload, 'evidence.md': payload, 'deliverables.zip': payload,
  } });
  await example.ready;
  assert.equal(example.elements.human.disabled, false);
  assert.equal(example.elements.agent.disabled, false);
  assert.equal(example.elements.evidence.disabled, false);
  assert.equal(example.elements.bundle.disabled, true);
  assert.match(example.elements.status.textContent, /exceeds the preview limit/);
});

test('shared deadline aborts a stalled request and leaves no ready download', async () => {
  const example = fixture({ fetch: (route, request) => new Promise((resolve, reject) => {
    request.signal.addEventListener('abort', () => reject(new Error('Synthetic timeout')));
  }) });
  const deadline = [...example.timers.values()].find(timer => timer.delay === 30000);
  assert.ok(deadline);
  deadline.callback();
  await example.ready;
  assert.equal(example.calls[0].request.signal.aborted, true);
  assert.equal(example.elements.bundle.disabled, true);
  assert.equal(example.timers.size, 0);
  assert.match(example.elements.status.textContent, /Synthetic timeout/);
});

test('missing browser hashing fails closed rather than accepting unchecked files', async () => {
  const example = fixture({ crypto: {} });
  await example.ready;
  assert.equal(example.elements.agent.disabled, true);
  assert.equal(example.elements.bundle.disabled, true);
  assert.equal(example.urls.size, 0);
});

test('cached downloads replace and expire object URLs without further requests', async () => {
  const example = fixture();
  await example.ready;
  example.elements.bundle.click();
  const previous = example.document.body.children.at(-1).href;
  example.elements.bundle.click();
  assert.equal(example.urls.has(previous), false);
  assert.equal(example.urls.size, 1);
  const cleanup = [...example.timers.values()].find(timer => timer.delay === 60000);
  cleanup.callback();
  assert.equal(example.urls.size, 0);
  assert.equal(example.calls.length, 4);
});

test('visible saved location and folder action are separate from browser download destinations', async () => {
  const example = fixture();
  await example.ready;
  assert.equal(example.elements.location.hidden, false);
  assert.equal(example.elements['output-path'].textContent, 'C:\\Synthetic fixture\\deliverables');
  assert.equal(example.elements['agent-path'].textContent, 'agent-spec.md');
  assert.match(example.elements['agent-path'].title, /deliverables\\agent-spec.md$/);
  await example.elements.folder.click();
  assert.deepEqual(JSON.parse(example.calls.at(-1).request.body), { target: 'folder', snapshot_id: 'a'.repeat(64) });
  assert.match(example.elements.status.textContent, /file manager/);
  await example.elements['copy-path'].click();
  assert.equal(example.elements['output-path'].copied, example.elements['output-path'].textContent);
  assert.equal(example.calls.length, 5);
});

test('failed or disconnected opening retains saved path, never retries and enables explicit recovery', async () => {
  for (const fail of [() => new Response('unavailable', { status: 409 }), () => { throw new TypeError('disconnected'); }]) {
    const example = fixture({ fetch: (route, request, fallback) => route === '/api/open' ? fail() : fallback(route) });
    await example.ready;
    await example.elements.agent.click();
    assert.equal(example.calls.length, 5);
    assert.equal(example.elements.agent.disabled, false);
    assert.equal(example.elements.bundle.disabled, false);
    assert.match(example.elements.status.textContent, /Could not confirm.*No automatic retry.*agent-spec.md/);
    assert.equal(example.timers.size, 0);
  }
});

test('opening has a bounded deadline and ignores repeated clicks while waiting', async () => {
  const example = fixture({ fetch: (route, request, fallback) => route !== '/api/open' ? fallback(route) : new Promise((resolve, reject) => {
    request.signal.addEventListener('abort', () => reject(new Error('timeout')));
  }) });
  await example.ready;
  const pending = example.elements.agent.click();
  await example.elements.agent.click();
  await example.elements.folder.click();
  assert.equal(example.calls.length, 5);
  assert.equal(example.elements.agent.disabled, true);
  [...example.timers.values()].find(timer => timer.delay === 10000).callback();
  await pending;
  assert.match(example.elements.status.textContent, /Could not confirm/);
  assert.equal(example.elements.agent.disabled, false);
});

test('clipboard denial selects the visible path with keyboard-copy guidance', async () => {
  const example = fixture({ copy: async () => { throw new Error('denied'); } });
  await example.ready;
  await example.elements['copy-path'].click();
  assert.equal(example.elements['output-path'].selected, true);
  assert.equal(example.elements['output-path'].focused, true);
  assert.match(example.elements.status.textContent, /Ctrl\+C/);
});

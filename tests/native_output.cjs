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
    createElement: tag => tag === 'template' ? {
      innerHTML: '',
      content: { querySelectorAll: selector => options.previewNodes?.[selector] || [] },
    } : new Element(tag),
  };
  const calls = [];
  const timers = new Map();
  const urls = new Map();
  let sequence = 0;
  const fallback = route => {
    if (route === '/api/delivery') return { ok: true, json: async () => manifest };
    const name = new URL(route, 'http://fixture.invalid').searchParams.get('name');
    return new Response(bodies[name], { headers: { 'Content-Type': 'application/octet-stream' } });
  };
  const context = vm.createContext({
    URLSearchParams, Blob, AbortController, Uint8Array,
    crypto: options.crypto || webcrypto,
    location: { hash: options.hash ?? '#access=synthetic-read-only-capability' },
    document,
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
  for (const code of ['timeout', 'cleanup_unconfirmed', 'call_budget', 'PRIVATE_FAILURE']) {
    const example = fixture({ hash: '#access=synthetic&progress=1', fetch: async () => ({ ok: true,
      json: async () => ({ phase: 'error', error_code: code, detail: 'PRIVATE_SOURCE' }),
    }) });
    await example.ready;
    assert.equal(example.calls.length, 1);
    assert.equal(example.elements.bundle.disabled, true);
    assert.equal(example.timers.size, 0);
    assert.equal(example.elements['progress-title'].textContent, 'Export stopped');
    assert.doesNotMatch(example.elements.status.textContent, /PRIVATE/);
  }
  const missing = fixture({ hash: '#progress=1' });
  await missing.ready;
  assert.equal(missing.calls.length, 0);
});

test('selected downloads use authenticated read-only requests and preserve exact bytes', async () => {
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
  example.elements.agent.click();
  const anchor = example.document.body.children.at(-1);
  assert.equal(anchor.download, 'agent-spec.md');
  assert.equal(await example.urls.get(anchor.href).text(), example.bodies['agent-spec.md']);
  assert.equal(example.calls.length, 4);
  assert.match(example.elements.status.textContent, /Download requested/);
  assert.equal(example.timers.size, 1);
});

test('human-only selection previews verified HTML in a sandbox and excludes Agent files', async () => {
  const example = fixture({ selected: ['human-spec.html'] });
  await example.ready;
  assert.ok(example.elements.story.srcdoc.includes(example.bodies['human-spec.html']));
  assert.match(example.elements.story.srcdoc, /default-src 'none'; script-src 'none'; style-src 'unsafe-inline'; img-src data:/);
  assert.match(example.elements.story.srcdoc, /connect-src 'none'; frame-src 'none'/);
  assert.equal(example.elements.preview.hidden, false);
  assert.equal(example.elements.agent.hidden, true);
  assert.equal(example.elements.evidence.hidden, true);
  assert.match(html, /<iframe\b[^>]*\bid="story"[^>]*\bsandbox\s*>/);
  assert.match(html, /id="status"[^>]*role="status"[^>]*aria-live="polite"/);
  assert.deepEqual(example.calls.map(call => call.route), [
    '/api/delivery', '/api/file?name=human-spec.html', '/api/file?name=deliverables.zip',
  ]);
  example.elements.human.click();
  const anchor = example.document.body.children.at(-1);
  assert.equal(await example.urls.get(anchor.href).text(), example.bodies['human-spec.html']);
});

test('preview-only DOM projection disables non-fragment links and active elements', async () => {
  const anchor = href => ({
    values: { href, target: '_top', download: 'companion', ping: '/private' },
    getAttribute(name) { return this.values[name]; },
    setAttribute(name, value) { this.values[name] = value; },
    removeAttribute(name) { delete this.values[name]; },
  });
  const links = ['#target', 'agent-spec.md', 'evidence.md#e000001', 'https://example.invalid/', 'javascript:alert(1)'].map(anchor);
  const active = new Element('script');
  const eventElement = { attributes: [{ name: 'onload' }, { name: 'style' }], removed: [],
    removeAttribute(name) { this.removed.push(name); } };
  const example = fixture({ selected: ['human-spec.html'], previewNodes: {
    'script,base,meta,link,iframe,object,embed': [active], '*': [eventElement], 'a[href],area[href]': links,
  } });
  await example.ready;
  assert.equal(active.removed, true);
  assert.deepEqual(eventElement.removed, ['onload']);
  assert.equal(links[0].values.href, 'about:srcdoc#target');
  for (const link of links) {
    for (const attribute of ['target', 'download', 'ping']) assert.equal(link.values[attribute], undefined);
  }
  for (const link of links.slice(1)) {
    assert.equal(link.values.href, undefined);
    assert.equal(link.values['aria-disabled'], 'true');
    assert.match(link.values.title, /download buttons above/);
  }
  assert.match(html, /Downloaded files keep their original bytes/);
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
  example.elements.agent.click();
  assert.equal(example.calls.length, 3);
  assert.equal(example.urls.size, 1);
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
  example.elements.agent.click();
  const previous = example.document.body.children.at(-1).href;
  example.elements.evidence.click();
  assert.equal(example.urls.has(previous), false);
  assert.equal(example.urls.size, 1);
  const cleanup = [...example.timers.values()].find(timer => timer.delay === 60000);
  cleanup.callback();
  assert.equal(example.urls.size, 0);
  assert.equal(example.calls.length, 4);
});

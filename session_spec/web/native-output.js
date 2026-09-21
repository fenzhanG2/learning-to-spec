const access = new URLSearchParams(location.hash.slice(1)).get('access');
const status = document.getElementById('status');
const controls = { 'human-spec.html': 'human', 'agent-spec.md': 'agent', 'evidence.md': 'evidence', 'deliverables.zip': 'bundle' };
const cache = new Map();
let downloadUrl;
let cleanupTimer;

async function waitForExport() {
  if (!access) throw new Error('Reopen the export with /to-spec in Copilot.');
  if (new URLSearchParams(location.hash.slice(1)).get('progress') !== '1') return;
  const panel = document.getElementById('progress');
  panel.hidden = false;
  const labels = {
    draft: ['1 / 3 · Drafting the story', 'Finding the problem, decisions, turning points and next steps.'],
    checking: ['2 / 3 · Checking source fidelity', 'Checking the draft against the recorded session.'],
    repair: ['1 / 3 · Correcting the draft', 'A validation check found something to repair. The same time and call budget still applies.'],
    privacy: ['2 / 3 · Checking facts and privacy', 'Checking the story and identifying sensitive details for your approval.'],
    review: ['Your choices are ready', 'Return to the conversation to accept the privacy plan and export. Waiting for your answer does not use model calls.'],
    export: ['3 / 3 · Exporting your files', 'Applying your approved choices and checking the HTML, Markdown and ZIP. No more model calls.'],
    working: ['Preparing your spec', 'The job is running. This panel checks its state every three seconds.'],
  };
  for (;;) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 10000);
    let value;
    try {
      const response = await fetch('/api/progress', { headers: { Authorization: `Bearer ${access}` }, cache: 'no-store', redirect: 'error', signal: controller.signal });
      if (!response.ok) throw new Error('Progress connection lost. Keep the conversation open and check its result; do not start a duplicate export.');
      value = await response.json();
    } finally { clearTimeout(timer); }
    if (value.phase === 'error') {
      document.getElementById('progress-title').textContent = 'Export stopped';
      const messages = {
        timeout: 'The configured time limit was reached. Saved progress remains local; no new export or upload was started.',
        cleanup_unconfirmed: 'Worker cleanup is not confirmed. Do not start another export until cleanup is checked.',
        call_budget: 'The three-call model budget was reached. Saved progress remains local; no automatic retry occurred.',
      };
      document.getElementById('progress-detail').textContent = messages[value.error_code] || 'A validation or provider error stopped this export. See the conversation result; saved diagnostics remain local.';
      throw new Error(document.getElementById('progress-detail').textContent);
    }
    if (value.phase === 'ready') { panel.hidden = true; return; }
    const label = Object.hasOwn(labels, value.phase) ? labels[value.phase] : labels.working;
    document.getElementById('progress-title').textContent = label[0];
    document.getElementById('progress-detail').textContent = label[1];
    document.getElementById('progress-clock').textContent = Number.isSafeInteger(value.elapsed_seconds) && value.elapsed_seconds >= 0
      ? `${value.elapsed_seconds}s automatic processing · 5-minute limit · approval time excluded` : '5-minute processing limit · approval time excluded';
    status.textContent = 'Connected · live stage updates · no upload by this panel';
    await new Promise(resolve => setTimeout(resolve, 3000));
  }
}

function previewDocument(content) {
  const template = document.createElement('template');
  template.innerHTML = content;
  for (const element of template.content.querySelectorAll('script,base,meta,link,iframe,object,embed')) element.remove();
  for (const element of template.content.querySelectorAll('*')) {
    for (const attribute of [...element.attributes]) {
      if (attribute.name.toLowerCase().startsWith('on')) element.removeAttribute(attribute.name);
    }
  }
  for (const link of template.content.querySelectorAll('a[href],area[href]')) {
    const href = link.getAttribute('href').trim();
    for (const attribute of ['target', 'download', 'ping']) link.removeAttribute(attribute);
    if (href.startsWith('#')) {
      link.setAttribute('href', `about:srcdoc${href}`);
    } else {
      link.removeAttribute('href');
      link.setAttribute('aria-disabled', 'true');
      link.setAttribute('title', 'Static preview: use the selected download buttons above for companion files.');
    }
  }
  const policy = "default-src 'none'; script-src 'none'; style-src 'unsafe-inline'; img-src data:; connect-src 'none'; frame-src 'none'; object-src 'none'; base-uri 'none'; form-action 'none'";
  return `<!doctype html><html><head><meta http-equiv="Content-Security-Policy" content="${policy}"></head><body>${template.innerHTML}</body></html>`;
}

function download(name) {
  if (!cache.has(name)) return;
  if (downloadUrl) URL.revokeObjectURL(downloadUrl);
  clearTimeout(cleanupTimer);
  downloadUrl = URL.createObjectURL(cache.get(name));
  const link = document.createElement('a');
  link.href = downloadUrl;
  link.download = name;
  document.body.append(link);
  link.click();
  link.remove();
  cleanupTimer = setTimeout(() => URL.revokeObjectURL(downloadUrl), 60000);
  status.textContent = `Download requested: ${name}. Your saved file was not regenerated.`;
}

async function prepare() {
  if (!access) throw new Error('Reopen the finished export with /to-spec in Copilot.');
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 30000);
  const options = { headers: { Authorization: `Bearer ${access}` }, cache: 'no-store', redirect: 'error', signal: controller.signal };
  try {
    const response = await fetch('/api/delivery', options);
    if (!response.ok) throw new Error('This saved export is unavailable. Reopen it with /to-spec.');
    const manifest = await response.json();
    const files = [...manifest.files, { name: 'deliverables.zip', sha256: manifest.bundle.sha256 }];
    if (!files.length || files.length > 4 || new Set(files.map(file => file.name)).size !== files.length) throw new Error('Invalid selected-file manifest.');
    let total = 0;
    for (const file of files) {
      if (!Object.hasOwn(controls, file.name) || !/^[a-f0-9]{64}$/.test(file.sha256)) throw new Error('Unexpected export file.');
      const button = document.getElementById(controls[file.name]);
      button.hidden = false;
      button.disabled = true;
      button.addEventListener('click', () => download(file.name));
      const result = await fetch(`/api/file?name=${encodeURIComponent(file.name)}`, options);
      if (!result.ok || !result.body?.getReader) throw new Error('A selected file is unavailable. No partial file was downloaded.');
      const reader = result.body.getReader();
      const chunks = [];
      let bytes = 0;
      try {
        for (;;) {
          const part = await reader.read();
          if (part.done) break;
          bytes += part.value.byteLength;
          if (bytes > 8 * 1024 * 1024 || total + bytes > 24 * 1024 * 1024) throw new Error('This export exceeds the preview limit. Use the local files linked in Copilot.');
          chunks.push(part.value);
        }
      } finally { await reader.cancel().catch(() => {}); reader.releaseLock(); }
      const blob = new Blob(chunks, { type: result.headers.get('Content-Type') || 'application/octet-stream' });
      const hash = [...new Uint8Array(await crypto.subtle.digest('SHA-256', await blob.arrayBuffer()))].map(value => value.toString(16).padStart(2, '0')).join('');
      if (hash !== file.sha256) throw new Error('The saved file changed. Reopen the export; no unverified download is enabled.');
      total += bytes;
      cache.set(file.name, blob);
      button.disabled = false;
      if (file.name === 'human-spec.html') {
        document.getElementById('story').srcdoc = previewDocument(await blob.text());
        document.getElementById('preview').hidden = false;
      }
    }
    status.textContent = 'Ready · saved locally · no ArtifactStore upload by this panel';
  } finally { clearTimeout(timer); }
}

(new URLSearchParams(location.hash.slice(1)).get('progress') === '1' ? waitForExport().then(prepare) : prepare())
  .catch(error => { status.textContent = `${error.message} Any already verified downloads remain available.`; });

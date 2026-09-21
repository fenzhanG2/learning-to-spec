const access = new URLSearchParams(location.hash.slice(1)).get('access');
const status = document.getElementById('status');
const controls = { 'human-spec.html': 'human', 'agent-spec.md': 'agent', 'evidence.md': 'evidence', 'deliverables.zip': 'bundle' };
const cache = new Map();
let downloadUrl;
let cleanupTimer;
let savedOutput;
let opening = false;

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
        draft_references_invalid: 'The draft has missing or invalid source citations. One automatic repair could not produce a valid draft. No final files were approved or uploaded. Run /to-spec again to try a fresh draft with additional model calls.',
        draft_structure_invalid: 'The model returned an invalid draft format. One automatic repair could not produce a valid draft. No final files were approved or uploaded. Run /to-spec again to try a fresh draft with additional model calls.',
      };
      document.getElementById('progress-detail').textContent = messages[value.error_code] || 'A validation or provider error stopped this export. See the conversation result; saved diagnostics remain local.';
      throw new Error(document.getElementById('progress-detail').textContent);
    }
    if (value.phase === 'ready' || value.phase === 'draft_available') { panel.hidden = true; return; }
    const label = Object.hasOwn(labels, value.phase) ? labels[value.phase] : labels.working;
    document.getElementById('progress-title').textContent = label[0];
    document.getElementById('progress-detail').textContent = label[1];
    document.getElementById('progress-clock').textContent = Number.isSafeInteger(value.elapsed_seconds) && value.elapsed_seconds >= 0
      ? `${value.elapsed_seconds}s automatic processing · 5-minute limit · approval time excluded` : '5-minute processing limit · approval time excluded';
    status.textContent = 'Connected · live stage updates · no upload by this panel';
    await new Promise(resolve => setTimeout(resolve, 3000));
  }
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
  status.textContent = `ZIP copy requested. Check Copilot/browser Downloads for its location. Your original files remain in the folder shown above.`;
}

async function openOutput(name) {
  if (!savedOutput || opening) return;
  opening = true;
  const button = document.getElementById(name === 'folder' ? 'folder' : controls[name]);
  const path = name === 'folder' ? savedOutput.local.folder : savedOutput.local.files[name];
  button.disabled = true;
  status.textContent = `Opening ${name === 'folder' ? 'output folder' : name}…`;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 10000);
  try {
    const response = await fetch('/api/open', {
      method: 'POST', headers: { Authorization: `Bearer ${access}`, 'Content-Type': 'application/json' },
      body: JSON.stringify({ target: name, snapshot_id: savedOutput.snapshot_id }),
      cache: 'no-store', redirect: 'error', signal: controller.signal,
    });
    if (!response.ok || (await response.json()).status !== 'dispatched') throw new Error('Open unavailable');
    status.textContent = `Sent to your ${name === 'folder' ? 'file manager' : 'default app'}: ${path}. If no window appears, use this path or the ZIP copy.`;
  } catch {
    status.textContent = `Could not confirm opening. No automatic retry. Open this saved path manually or download the ZIP copy: ${path}`;
  } finally {
    clearTimeout(timer);
    button.disabled = false;
    opening = false;
  }
}

async function copyFolderPath() {
  try {
    await navigator.clipboard.writeText(savedOutput.local.folder);
    status.textContent = 'Output folder path copied.';
  } catch {
    const path = document.getElementById('output-path');
    const selection = window.getSelection();
    const range = document.createRange();
    range.selectNodeContents(path);
    selection.removeAllRanges();
    selection.addRange(range);
    path.focus();
    status.textContent = 'Copy is unavailable in this host. The folder path is selected; press Ctrl+C (⌘C on Mac).';
  }
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
    if (manifest.kind && manifest.kind !== 'unvalidated_draft') throw new Error('Unknown output quality status.');
    const recovery = manifest.kind === 'unvalidated_draft';
    const files = [...manifest.files, { name: 'deliverables.zip', sha256: manifest.bundle.sha256 }];
    if (!files.length || files.length > 4 || new Set(files.map(file => file.name)).size !== files.length) throw new Error('Invalid selected-file manifest.');
    if (!/^[a-f0-9]{64}$/.test(manifest.snapshot_id) || typeof manifest.local?.folder !== 'string'
        || !manifest.local.folder || files.some(file => typeof manifest.local.files?.[file.name] !== 'string' || !manifest.local.files[file.name])) {
      throw new Error('The saved output location is unavailable. Reopen this export in Copilot.');
    }
    savedOutput = manifest;
    if (recovery) {
      document.getElementById('draft-warning').hidden = false;
      document.getElementById('export-description').textContent = 'These are unvalidated, unredacted private drafts, not privacy-reviewed deliverables. Local hashes detect file changes; they do not establish correctness or safe sharing. This panel cannot upload anything.';
      document.getElementById('bundle').textContent = 'Download private draft ZIP';
      document.getElementById('human-label').textContent = 'Open Human draft ↗';
      document.getElementById('agent-label').textContent = 'Open Agent draft ↗';
      document.getElementById('evidence-hint').textContent = 'No validated evidence companion is included. Check facts and citations against the original session.';
    }
    document.getElementById('output-path').textContent = manifest.local.folder;
    document.getElementById('location').hidden = false;
    document.getElementById('folder').disabled = false;
    document.getElementById('folder').addEventListener('click', () => openOutput('folder'));
    document.getElementById('copy-path').addEventListener('click', copyFolderPath);
    let total = 0;
    for (const file of files) {
      if (!Object.hasOwn(controls, file.name) || !/^[a-f0-9]{64}$/.test(file.sha256)) throw new Error('Unexpected export file.');
      const button = document.getElementById(controls[file.name]);
      button.hidden = false;
      button.disabled = true;
      button.addEventListener('click', () => file.name === 'deliverables.zip' ? download(file.name) : openOutput(file.name));
      const path = document.getElementById(`${controls[file.name]}-path`);
      if (path) { path.textContent = file.name; path.title = manifest.local.files[file.name]; }
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
          if (bytes > 8 * 1024 * 1024 || total + bytes > 24 * 1024 * 1024) throw new Error('This export exceeds the preview limit. Use Open output folder or the saved path shown above.');
          chunks.push(part.value);
        }
      } finally { await reader.cancel().catch(() => {}); reader.releaseLock(); }
      const blob = new Blob(chunks, { type: result.headers.get('Content-Type') || 'application/octet-stream' });
      const hash = [...new Uint8Array(await crypto.subtle.digest('SHA-256', await blob.arrayBuffer()))].map(value => value.toString(16).padStart(2, '0')).join('');
      if (hash !== file.sha256) throw new Error('The saved file changed. Reopen the export; no unverified download is enabled.');
      total += bytes;
      if (file.name === 'deliverables.zip') cache.set(file.name, blob);
      button.disabled = false;
    }
    status.textContent = recovery ? 'Unvalidated draft saved locally · privacy incomplete · no upload'
      : 'Ready · saved locally · no ArtifactStore upload by this panel';
  } finally { clearTimeout(timer); }
}

(new URLSearchParams(location.hash.slice(1)).get('progress') === '1' ? waitForExport().then(prepare) : prepare())
  .catch(error => { status.textContent = `${error.message} Saved paths and any already verified ZIP copy remain available.`; });

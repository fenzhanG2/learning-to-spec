const parameters = new URLSearchParams(location.hash.slice(1));
const access = parameters.get('access');
const elements = name => document.getElementById(name);
const state = { job: null, review: null, choices: {}, manifest: null, plan: null, busy: false, stage: 'source', published: false, generationRetry: false, connectionError: null, durableJobs: null, downloadSnapshot: null };
const SNAPSHOT_LIMITS = { fileBytes: 8 * 1024 * 1024, totalBytes: 24 * 1024 * 1024, milliseconds: 15000 };
const OFFLINE_SNAPSHOT_NOTICE = 'Server status is unknown. Only completed cached exports can be downloaded; generation, upload and retry remain blocked.';
let downloadEpoch = 0;
let downloadUrl = null;
let downloadTimer;
let planTimer;
let planSequence = 0;
function preview(html) {
  elements('human-preview').srcdoc = html;
}
const status = (text, error = false) => {
  const diagnostic = String(text || '');
  const authentication = error && /authentication token|bad credentials|authenticate.*copilot|no gh authentication/i.test(diagnostic);
  elements('status').textContent = error && state.connectionError ? state.connectionError : authentication
    ? 'Copilot could not sign in for generation. Check the configured GitHub host/account, then retry. See technical details below.'
    : error && diagnostic.length > 240 ? 'This step could not finish. See technical details below before retrying.'
      : diagnostic + (state.connectionError ? ` ${OFFLINE_SNAPSHOT_NOTICE}` : '');
  elements('status').classList.toggle('error', error || Boolean(state.connectionError));
  elements('error-details').hidden = !error && !state.connectionError;
  elements('error-details').open = false;
  elements('error-text').textContent = error ? diagnostic : state.connectionError || '';
};
function show(stage) {
  state.stage = stage;
  document.body.dataset.stage = stage;
  for (const name of ['source', 'review', 'output', 'share', 'working']) elements(`${name}-panel`).hidden = name !== stage;
  if (stage !== 'working') {
    for (const name of ['source', 'review', 'output']) {
      if (name === (stage === 'share' ? 'output' : stage)) elements(`step-${name}`).setAttribute('aria-current', 'step');
      else elements(`step-${name}`).removeAttribute('aria-current');
    }
  }
  elements('status').scrollIntoView({ block: 'nearest' });
}
const node = (tag, text, className) => {
  const element = document.createElement(tag);
  if (text !== undefined) element.textContent = text;
  if (className) element.className = className;
  return element;
};
function disconnected() {
  const identifier = state.job || (/^[a-f0-9]{32}$/.test(parameters.get('job') || '') ? parameters.get('job') : null);
  const recovery = state.durableJobs === false
    ? 'Restart the manual Studio command. Manual job IDs cannot be reopened through Copilot; use the saved review/generation paths for recovery.'
    : state.durableJobs === true ? `Ask Copilot to reopen learning-to-spec${identifier ? ` job ${identifier}` : ' Studio'}.`
      : `Reopen Studio from Copilot, or restart its manual command.${identifier ? ` Previous job: ${identifier}.` : ''}`;
  state.connectionError = `Studio connection lost or response incomplete. ${recovery} Check saved status before retrying: an operation may already have started. Unsubmitted choices may need selecting again. ${OFFLINE_SNAPSHOT_NOTICE}`;
  state.generationRetry = false;
  invalidatePlan();
  return new Error(state.connectionError);
}
async function localFetch(path, options) {
  if (state.connectionError) throw new Error(state.connectionError);
  try { return await fetch(path, options); }
  catch { throw disconnected(); }
}
async function responseBody(response, format) {
  try { return await response[format](); }
  catch { throw disconnected(); }
}
async function request(path, data) {
  const response = await localFetch(path, { method: data === undefined ? 'GET' : 'POST', headers: { Authorization: `Bearer ${access}`, 'Content-Type': 'application/json' }, body: data === undefined ? undefined : JSON.stringify(data), cache: 'no-store' });
  const result = await responseBody(response, 'json');
  if (!response.ok) throw new Error(result.error || `Request failed (${response.status})`);
  return result;
}
async function file(name) {
  const response = await localFetch(`/api/file?job=${encodeURIComponent(state.job)}&name=${encodeURIComponent(name)}`, { headers: { Authorization: `Bearer ${access}` }, cache: 'no-store' });
  if (!response.ok) throw new Error((await responseBody(response, 'json')).error || 'File is not available');
  return responseBody(response, 'blob');
}
const outputScope = () => ({ job: state.job, epoch: downloadEpoch });
const currentOutput = scope => scope.job === state.job && scope.epoch === downloadEpoch;
const currentSnapshot = snapshot => Boolean(snapshot && state.downloadSnapshot === snapshot && currentOutput(snapshot));
function releaseDownloadUrl() {
  clearTimeout(downloadTimer);
  if (downloadUrl) URL.revokeObjectURL(downloadUrl);
  downloadUrl = null;
}
function invalidateDownloads() {
  downloadEpoch++;
  state.downloadSnapshot?.controller.abort();
  state.downloadSnapshot?.blobs.clear();
  state.downloadSnapshot = null;
  releaseDownloadUrl();
  preview('');
  elements('output-selection').textContent = 'Downloads need preparing.';
  elements('snapshot-details').textContent = '';
  updateControls();
}
function snapshotStatus(snapshot) {
  if (!currentSnapshot(snapshot)) return;
  const missing = [...snapshot.selected].filter(name => !snapshot.blobs.has(name));
  elements('output-selection').textContent = snapshot.pending
    ? `Preparing downloads… ${snapshot.blobs.size}/${snapshot.selected.size} ready.`
    : snapshot.invalidSelection ? 'Some selected downloads are unavailable. See download help.'
      : missing.length ? `${snapshot.blobs.size}/${snapshot.selected.size} downloads ready. Others unavailable.`
        : `${snapshot.blobs.size} downloads ready in this tab.`;
  elements('snapshot-details').textContent = `${snapshot.fileCount} selected files saved locally. ${snapshot.blobs.size}/${snapshot.selected.size} downloads cached in this tab. `
    + (snapshot.pending ? 'Preparing bounded snapshots… ' : missing.length ? `Incomplete/unavailable: ${missing.join(', ')}. Use saved local files or reopen this job. ` : '')
    + (snapshot.invalidSelection ? 'Unrecognized reader/file selection was not cached. ' : '')
    + (!snapshot.binding ? 'Verified generation identity or browser hashing unavailable; reopen this job. ' : '')
    + 'Cache limits: 8 MiB per file, 24 MiB total, 15 seconds. '
    + 'Cached downloads are exported snapshots, not live or revalidated server state; they disappear on reload or changed setup/choices/job.';
  updateControls();
}
async function snapshotFile(snapshot, name) {
  const response = await fetch(`/api/file?job=${encodeURIComponent(snapshot.job)}&name=${encodeURIComponent(name)}`, {
    headers: { Authorization: `Bearer ${access}`, 'X-Delivery-Snapshot': snapshot.binding.id }, cache: 'no-store', redirect: 'error', signal: snapshot.controller.signal,
  });
  if (!currentSnapshot(snapshot) || snapshot.controller.signal.aborted) {
    response.body?.cancel().catch(() => {});
    throw new Error('Snapshot changed');
  }
  const limit = Math.min(SNAPSHOT_LIMITS.fileBytes, SNAPSHOT_LIMITS.totalBytes - snapshot.bytes);
  const declared = response.headers?.get('content-length');
  if (!response.ok || response.headers.get('X-Delivery-Snapshot') !== snapshot.binding.id || !response.body?.getReader || limit <= 0 || declared && (!/^\d+$/.test(declared) || Number(declared) > limit)) {
    response.body?.cancel().catch(() => {});
    throw new Error('Snapshot unavailable or over its size limit');
  }
  const reader = response.body.getReader();
  const chunks = [];
  let bytes = 0;
  let finished = false;
  try {
    for (;;) {
      const part = await reader.read();
      if (!currentSnapshot(snapshot) || snapshot.controller.signal.aborted) throw new Error('Snapshot changed or timed out');
      if (part.done) break;
      bytes += part.value.byteLength;
      if (bytes > limit) throw new Error('Snapshot exceeds its size limit');
      chunks.push(part.value);
    }
    if (declared !== null && declared !== undefined && Number(declared) !== bytes) throw new Error('Snapshot response incomplete');
    const content = new Blob(chunks, { type: response.headers?.get('content-type') || 'application/octet-stream' });
    const hash = [...new Uint8Array(await crypto.subtle.digest('SHA-256', await content.arrayBuffer()))].map(value => value.toString(16).padStart(2, '0')).join('');
    if (hash !== snapshot.binding.files[name]) throw new Error('Snapshot file hash does not match the selected generation');
    finished = true;
    return content;
  } finally {
    if (!finished) reader.cancel().catch(() => {});
    reader.releaseLock();
  }
}
async function stageSnapshots(snapshot) {
  const timeout = setTimeout(() => snapshot.controller.abort(), SNAPSHOT_LIMITS.milliseconds);
  try {
    for (const name of snapshot.selected) {
      if (!snapshot.binding || !currentSnapshot(snapshot) || snapshot.controller.signal.aborted || state.connectionError) break;
      try {
        const content = await snapshotFile(snapshot, name);
        if (!currentSnapshot(snapshot) || snapshot.controller.signal.aborted) return;
        snapshot.blobs.set(name, content);
        snapshot.bytes += content.size;
        if (name === 'human-spec.html') {
          const html = await content.text();
          if (currentSnapshot(snapshot)) preview(html);
        }
      } catch (error) {
        if (!currentSnapshot(snapshot)) return;
        snapshot.errors.set(name, error.message);
        if (error instanceof TypeError && !snapshot.controller.signal.aborted) status(disconnected().message, true);
      }
      snapshotStatus(snapshot);
    }
  } finally {
    clearTimeout(timeout);
    snapshot.pending = false;
    snapshotStatus(snapshot);
  }
}
function downloadSnapshot(name) {
  const snapshot = state.downloadSnapshot;
  if (state.busy || !currentSnapshot(snapshot) || !snapshot.selected.has(name) || !snapshot.blobs.has(name)) return;
  releaseDownloadUrl();
  downloadUrl = URL.createObjectURL(snapshot.blobs.get(name));
  const link = node('a'); link.href = downloadUrl; link.download = name; document.body.append(link); link.click(); link.remove();
  downloadTimer = setTimeout(releaseDownloadUrl, 60000);
  status(`Cached export snapshot download requested: ${name}. No server contact or revalidation. Check your browser downloads.`);
}
function updateControls() {
  const findings = state.review?.findings || [];
  const remaining = findings.filter(finding => {
    const choice = state.choices[finding.id];
    return !choice?.action || (choice.action === 'generalize' && !choice.replacement?.trim());
  }).length;
  elements('remaining').textContent = state.connectionError ? 'Reopen Studio to continue' : remaining ? `${remaining} still need your choice` : 'Ready to generate';
  const unavailable = state.busy || Boolean(state.connectionError);
  for (const control of document.querySelectorAll('button, input, select, textarea')) control.disabled = unavailable;
  elements('generate').disabled = !state.review || Boolean(remaining) || unavailable;
  elements('generate').textContent = state.generationRetry ? 'Retry generation →' : 'Generate spec →';
  const unresolved = [...document.querySelectorAll('[data-finding]')].some(control => control.value !== 'keep');
  elements('publish').disabled = !state.plan || unavailable || unresolved || state.published;
  for (const button of document.querySelectorAll('[data-file]')) {
    button.disabled = state.busy || !currentSnapshot(state.downloadSnapshot) || !state.downloadSnapshot.blobs.has(button.dataset.file);
    button.title = button.disabled ? 'No complete bounded snapshot is available for this file.' : 'Download cached export snapshot; no live server revalidation.';
  }
}
async function perform(operation) {
  if (state.busy || state.connectionError) return;
  const previous = state.stage;
  state.busy = true;
  updateControls();
  try { await operation(); }
  catch (error) {
    if (state.stage === 'working') show(previous);
    status(error.message, true);
  } finally {
    state.busy = false;
    updateControls();
  }
}
function action(identifier, operation) {
  elements(identifier).addEventListener('click', () => perform(operation));
}
function rememberJob(identifier) {
  invalidateDownloads();
  state.job = identifier;
  parameters.set('job', identifier);
  history.replaceState(null, '', `#${parameters}`);
}
async function waitJob(label) {
  elements('working-title').textContent = label;
  show('working');
  for (;;) {
    const result = await request(`/api/status?job=${state.job}`);
    if (result.status === 'error') throw new Error(result.error);
    if (result.status === 'done') return result;
    status(label + '…');
    await new Promise(resolve => setTimeout(resolve, 1800));
  }
}
function invalidatePlan() {
  planSequence++;
  clearTimeout(planTimer);
  state.plan = null;
  elements('destination-card').hidden = true;
  updateControls();
}
function renderFindings() {
  const review = state.review;
  elements('findings').replaceChildren();
  const contextual = review.semantic?.status === 'reviewed';
  elements('review-summary').textContent = review.findings.length
    ? `${review.findings.length} details to consider. Choose what belongs in your spec.`
    : contextual
      ? 'Contextual review flagged no additional details. This does not establish that sharing is safe; indirect disclosures can still be missed.'
      : 'Local checks flagged no additional details. Indirect personal disclosures may still be present. For deeper review, change Privacy scan in setup.';
  elements('review-toolbar').hidden = review.findings.length < 2;
  elements('scan-details').textContent = `${Object.values(review.hard_removals || {}).reduce((sum, value) => sum + value, 0)} automatic removal matches/fields (overlaps possible). Contextual review: ${review.semantic?.status || 'not requested'}. ${review.semantic?.coverage || ''}`;
  const visible = review.findings.filter(finding => elements('filter').value !== 'unresolved' || !state.choices[finding.id]?.action);
  if (!visible.length) {
    const empty = node('div', undefined, 'empty');
    empty.append(node('strong', review.findings.length ? 'All details have a choice' : 'No findings is not a privacy clearance'), node('p', 'Check that your choices preserve technical failures, corrections and constraints. Detection is not an anonymity guarantee.', 'hint'));
    elements('findings').append(empty);
  }
  for (const finding of visible) {
    const card = node('article', undefined, 'finding');
    card.classList.toggle('decided', Boolean(state.choices[finding.id]?.action));
    const heading = node('div', undefined, 'finding-top');
    heading.append(node('h3', finding.label), node('span', `${finding.occurrences.length} ${finding.occurrences.length === 1 ? 'place' : 'places'}`, 'badge'));
    card.append(heading, node('pre', finding.text), node('p', finding.reason, 'explanation'));
    const choice = node('select');
    choice.setAttribute('aria-label', `Decision for ${finding.id}`);
    for (const [value, label] of [['', 'Choose what to do…'], ['remove', 'Remove'], ['pseudonymize', 'Replace with a pseudonym'], ['generalize', 'Rewrite less specifically'], ['keep', 'Keep as is']]) {
      const option = node('option', label + (finding.recommended === value ? ' · suggested' : ''));
      option.value = value; choice.append(option);
    }
    choice.value = state.choices[finding.id]?.action || '';
    const replacement = node('textarea');
    replacement.setAttribute('aria-label', `Generalization for ${finding.id}`);
    replacement.placeholder = 'A less revealing equivalent that keeps the technical meaning';
    replacement.value = state.choices[finding.id]?.replacement || finding.alternative || '';
    replacement.hidden = choice.value !== 'generalize';
    const changed = () => {
      invalidateDownloads();
      state.generationRetry = false;
      state.choices[finding.id] = { action: choice.value, replacement: replacement.value };
      replacement.hidden = choice.value !== 'generalize';
      card.classList.toggle('decided', Boolean(choice.value));
      invalidatePlan();
    };
    choice.addEventListener('change', changed);
    replacement.addEventListener('input', changed);
    const details = node('details');
    details.append(node('summary', 'Context & detection details'), node('p', `Assessed task relevance: ${finding.necessity} · ${finding.detectors.join(' + ')} detection`));
    if (finding.assessment_summary) details.append(node('p', finding.assessment_summary.notice));
    for (const context of finding.contexts || []) details.append(node('pre', context));
    for (const context of finding.related_contexts || []) details.append(node('p', `Related clue (context only) · Event ${context.event} · ${JSON.stringify(context.field)}`), node('pre', context.text));
    card.append(choice, replacement, details);
    elements('findings').append(card);
  }
  updateControls();
}
function updateAudience() {
  const sharing = elements('delivery').value === 'artifactstore';
  elements('audience-row').hidden = !sharing;
  elements('team-row').hidden = !sharing || elements('audience').value !== 'team';
}
elements('delivery').addEventListener('change', updateAudience);
elements('audience').addEventListener('change', async () => {
  updateAudience();
  if (elements('team-row').hidden) return;
  elements('team-status').textContent = 'Loading your teams…';
  try {
    const result = await request('/api/teams', {});
    elements('team-options').replaceChildren();
    for (const team of result.teams || []) {
      const option = node('option'); option.value = team.slug; option.label = team.name || team.slug;
      elements('team-options').append(option);
    }
    elements('team-status').textContent = 'Choose a team or enter its slug. Access is checked before upload.';
  } catch (error) { elements('team-status').textContent = error.message + ' You can still enter a known team slug.'; }
});
elements('detection').addEventListener('change', () => { elements('semantic-notice').hidden = elements('detection').value !== 'copilot'; });
elements('filter').addEventListener('change', renderFindings);
for (const identifier of ['session', 'readers', 'delivery', 'audience', 'team', 'purpose', 'detection', 'custom']) {
  for (const event of ['input', 'change']) elements(identifier).addEventListener(event, invalidateDownloads);
}
action('scan', async () => {
  invalidateDownloads();
  for (const identifier of ['session', 'readers', 'delivery']) if (!elements(identifier).value) throw new Error('Please choose ' + ({ session: 'a session', readers: 'your readers', delivery: 'a destination' })[identifier] + '.');
  const delivery = elements('delivery').value;
  const audience = delivery === 'local' ? 'local' : elements('audience').value === 'team' ? `team:${elements('team').value.trim()}` : elements('audience').value;
  const result = await request('/api/scan', { session: elements('session').value, readers: elements('readers').value, delivery, detection: elements('detection').value, audience, purpose: elements('purpose').value.trim() || 'Technical story and actionable Agent handoff', semantic: elements('detection').value === 'copilot', custom: elements('custom').value.split('\n').map(value => value.trim()).filter(Boolean) });
  rememberJob(result.job);
  state.review = null; state.choices = {}; state.manifest = null; state.published = false; state.generationRetry = false;
  invalidatePlan();
  await waitJob('Checking privacy');
  state.review = await request(`/api/review?job=${state.job}`);
  elements('filter').value = 'all';
  renderFindings(); show('review'); status(elements('detection').value === 'copilot' ? 'Privacy check finished, including the Copilot review you selected.' : 'Privacy checked locally. Nothing sent to a model.');
});
action('change-source', async () => {
  invalidateDownloads();
  show('source');
  status('Edit the setup, then check privacy again. Your previous job is saved.');
});
action('generate', async () => {
  invalidateDownloads();
  const scope = outputScope();
  try {
    const started = await request('/api/generate', { job: state.job, review_id: state.review.review_id, choices: state.choices, confirmed: true });
    if (!currentOutput(scope)) return;
    invalidatePlan(); state.manifest = null; state.published = false;
    const result = started.completed ? await request(`/api/status?job=${state.job}`) : await waitJob('Writing and checking your spec');
    if (!currentOutput(scope)) return;
    renderOutput(result.delivery_result || result.result, scope);
    state.generationRetry = false;
    show('output'); status('Saved on your computer. Nothing uploaded.');
  } catch (error) {
    state.generationRetry = !state.connectionError;
    throw error;
  }
});
function renderOutput(result, scope = outputScope()) {
  if (!currentOutput(scope)) return;
  state.downloadSnapshot?.controller.abort();
  state.downloadSnapshot?.blobs.clear();
  releaseDownloadUrl();
  preview('');
  const readers = result.preferences?.readers;
  const allowed = ['human', 'agent', 'both'].includes(readers) ? ['evidence.md',
    ...(['human', 'both'].includes(readers) ? ['human-spec.html'] : []),
    ...(['agent', 'both'].includes(readers) ? ['agent-spec.md'] : [])] : [];
  const listed = Array.isArray(result.files) ? result.files : [];
  const selected = [...new Set(listed.filter(name => allowed.includes(name)))];
  const validSelection = selected.length > 0 && listed.length === selected.length;
  const binding = result.snapshot;
  const boundNames = validSelection ? [...selected, 'deliverables.zip'] : selected;
  const validBinding = binding && /^[a-f0-9]{64}$/.test(binding.id) && binding.files && typeof globalThis.crypto?.subtle?.digest === 'function'
    && Object.keys(binding.files).length === boundNames.length && boundNames.every(name => /^[a-f0-9]{64}$/.test(binding.files[name]));
  const snapshot = { ...scope, selected: new Set(validSelection ? [...selected, 'deliverables.zip'] : selected),
    binding: validBinding ? { id: binding.id, files: { ...binding.files } } : null,
    blobs: new Map(), errors: new Map(), bytes: 0, fileCount: selected.length, invalidSelection: !validSelection,
    pending: true, controller: new AbortController() };
  state.downloadSnapshot = snapshot;
  for (const button of document.querySelectorAll('[data-file]')) button.hidden = !snapshot.selected.has(button.dataset.file);
  elements('open-agent').hidden = !selected.includes('agent-spec.md');
  elements('human-preview-panel').hidden = !selected.includes('human-spec.html');
  elements('output-path').textContent = result.output;
  elements('prepare-share').hidden = result.preferences?.destination !== 'artifactstore';
  elements('prepare-share').textContent = state.published ? 'View upload' : 'Upload to ArtifactStore →';
  snapshotStatus(snapshot);
  snapshot.ready = stageSnapshots(snapshot);
}
for (const button of document.querySelectorAll('[data-file]')) button.addEventListener('click', () => downloadSnapshot(button.dataset.file));
action('revise', async () => { invalidateDownloads(); invalidatePlan(); renderFindings(); show('review'); status('Update the details you want to change, then generate again.'); });
action('back-output', async () => { invalidatePlan(); show('output'); });
for (const [identifier, name] of [['open-agent', 'agent-spec.md'], ['open-folder', 'folder']]) action(identifier, async () => {
  await request('/api/open', { job: state.job, name });
  status('Requested your default file app. Files stay on this computer.');
});
async function refreshPlan() {
  const site = elements('site-name').value.trim();
  const sequence = ++planSequence;
  state.plan = null;
  elements('destination-card').hidden = true;
  updateControls();
  if (!site || !state.manifest || state.published) {
    elements('destination-status').textContent = 'Give this artifact a name.';
    return;
  }
  elements('destination-status').textContent = 'Checking name and access…';
  try {
    const plan = await request('/api/plan', { job: state.job, site });
    if (sequence !== planSequence) return;
    state.plan = plan;
    elements('destination-title').textContent = plan.site + ' · ArtifactStore';
    elements('destination-audience').textContent = plan.team ? `Team: ${plan.team}. Inherits the team access shown below.` : 'Owner-only, plus service administrators.';
    elements('plan-details').textContent = `Destination: ${plan.destination}\nFiles: ${plan.files.join(', ')}\nViewer policy:\n${JSON.stringify(plan.viewer_policy, null, 2)}\n\n${plan.note}`;
    elements('destination-card').hidden = false;
    elements('destination-status').textContent = 'Ready. Nothing has been uploaded.';
    elements('publish').textContent = plan.team ? 'Upload to team' : 'Upload · owner-only';
  } catch (error) {
    if (sequence === planSequence) elements('destination-status').textContent = error.message;
  } finally { updateControls(); }
}
function renderShare(manifest) {
  state.manifest = manifest; state.published = false;
  elements('share-form').hidden = false;
  elements('publish').hidden = false;
  elements('verify-publish').hidden = true;
  elements('published').replaceChildren();
  elements('final-summary').textContent = `Files: ${Object.keys(manifest.files).join(', ')}. ${manifest.findings.length ? manifest.findings.length + ' remaining disclosures need your choice below.' : 'No additional disclosures flagged.'}`;
  elements('final-findings').replaceChildren();
  for (const finding of manifest.findings) {
    const row = node('div', undefined, 'final-finding');
    const choice = node('select');
    choice.dataset.finding = finding.id;
    choice.setAttribute('aria-label', `Sharing decision for ${finding.id}`);
    for (const [value, label] of [['', 'Choose what to do…'], ['keep', 'Include in this upload'], ['revise', 'Change my privacy choices first']]) {
      const option = node('option', label); option.value = value; choice.append(option);
    }
    choice.addEventListener('change', () => {
      if (choice.value === 'revise') { invalidateDownloads(); invalidatePlan(); renderFindings(); show('review'); }
      updateControls();
    });
    row.append(node('strong', finding.category), node('p', `${finding.file}: ${finding.text}`), choice);
    elements('final-findings').append(row);
  }
}
action('prepare-share', async () => {
  const job = await request(`/api/status?job=${state.job}`);
  if (job.publication_attempted) { renderPublication(job.result, job.error); show('share'); return; }
  renderShare(await request('/api/package', { job: state.job, evidence: true }));
  if (!elements('site-name').value) elements('site-name').value = 'session-story-' + state.job.slice(0, 10);
  show('share'); status('Choose the exact upload. Your original session is never included.');
  await refreshPlan();
});
elements('site-name').addEventListener('input', () => {
  invalidatePlan();
  elements('destination-status').textContent = 'Checking name and access…';
  planTimer = setTimeout(refreshPlan, 500);
});
function renderPublication(result, error) {
  state.published = true;
  invalidatePlan();
  elements('share-form').hidden = true;
  elements('publish').hidden = true;
  elements('verify-publish').hidden = false;
  elements('prepare-share').textContent = 'View upload';
  elements('published').replaceChildren(node('p', error || (result?.status === 'verified' ? 'Uploaded and verified against the approved content.' : 'An upload was attempted. Verify its state before any further action.')));
  if (result?.status === 'verified') {
    for (const [name, url] of Object.entries(result.files || {})) {
      const link = node('a', name); link.href = url; link.rel = 'noreferrer noopener'; link.target = '_blank';
      elements('published').append(link);
    }
  }
}
action('publish', async () => {
  try {
    await request('/api/publish', { job: state.job, confirm: state.plan.plan_id, package_id: state.manifest.package_id, publish_intent: true, acknowledged: [...document.querySelectorAll('[data-finding]')].filter(control => control.value === 'keep').map(control => control.dataset.finding) });
    const job = await waitJob('Uploading and verifying');
    renderPublication(job.result); show('share'); status('Publication verified.');
  } catch (error) {
    const job = await request(`/api/status?job=${state.job}`);
    if (job.publication_attempted) renderPublication(job.result, error.message);
    else await refreshPlan();
    show('share');
    throw error;
  }
});
action('verify-publish', async () => {
  await request('/api/verify', { job: state.job });
  const job = await waitJob('Verifying the existing upload');
  renderPublication(job.result); show('share'); status('Readback verified. No new upload.');
});
async function refreshJobs() {
  const result = await request('/api/jobs');
  elements('saved-jobs').replaceChildren(node('option', 'Choose a job…'));
  elements('saved-jobs').firstElementChild.value = '';
  elements('history').hidden = !result.jobs.length;
  for (const job of result.jobs) {
    const date = job.updated_at ? new Date(job.updated_at).toLocaleString() : '';
    const option = node('option', `${date} · ${job.stage} / ${job.status} · ${job.id.slice(0, 8)}`);
    option.value = job.id; elements('saved-jobs').append(option);
  }
}
async function reopenJob(identifier) {
  if (!identifier) return;
  rememberJob(identifier); invalidatePlan(); state.manifest = null; state.review = null; state.choices = {}; state.published = false; state.generationRetry = false;
  const scope = outputScope();
  elements('site-name').value = '';
  let job = await request(`/api/status?job=${identifier}`);
  if (!currentOutput(scope)) return;
  if (job.status === 'running') {
    show(job.stage === 'scan' ? 'source' : 'review');
    try { job = await waitJob('Continuing your saved work'); }
    catch { job = await request(`/api/status?job=${identifier}`); }
  }
  if (!currentOutput(scope)) return;
  if (job.stage === 'scan' && job.status === 'error') {
    show('source'); status(job.error, true); return;
  }
  const review = await request(`/api/review?job=${identifier}`);
  if (!currentOutput(scope)) return;
  state.review = review;
  state.choices = job.approved_choices?.choices || {};
  state.generationRetry = job.stage === 'generate' && job.status === 'error';
  elements('filter').value = 'all';
  renderFindings();
  const preferences = state.review.preferences || {};
  elements('readers').value = preferences.readers || '';
  elements('delivery').value = preferences.destination || '';
  elements('audience').value = state.review.audience?.startsWith('team:') ? 'team' : state.review.audience || '';
  elements('team').value = state.review.audience?.startsWith('team:') ? state.review.audience.slice(5) : '';
  updateAudience();
  if (job.publication_attempted) {
    if (job.delivery_result) renderOutput(job.delivery_result, scope);
    renderPublication(job.result, job.error); show('share'); status('Restored upload history. No automatic retry.'); return;
  }
  if (job.status === 'done' && job.delivery_result) {
    renderOutput(job.delivery_result, scope); show('output'); status('Reopened saved files. No model call or upload.');
  } else {
    const restored = Object.keys(state.choices).length ? 'Your saved choices are ready to continue.'
      : state.review.findings.length ? 'Choose how to handle each flagged detail before generating.' : 'Privacy check is ready. No additional details need a choice.';
    show('review'); status(job.error || restored, Boolean(job.error));
  }
}
elements('saved-jobs').addEventListener('change', () => perform(async () => { await reopenJob(elements('saved-jobs').value); elements('history').open = false; }));
elements('history').addEventListener('toggle', () => { if (elements('history').open && !state.busy && !state.connectionError) refreshJobs().catch(error => status(error.message, true)); });
perform(async () => {
  if (!access) throw new Error('Open Studio from Copilot, or use the complete private URL printed by the studio command.');
  const result = await request('/api/sessions');
  state.durableJobs = typeof result.durable_jobs === 'boolean' ? result.durable_jobs : null;
  if (state.durableJobs === false) elements('working-description').textContent = 'Keep the manual Studio command running while we work. Closing that process disconnects this page.';
  for (const session of result.sessions) {
    const option = node('option', `${session.title} · ${Math.round(session.bytes / 1024)} KB`);
    option.value = session.id; option.selected = session.id === 'selected' || session.id === parameters.get('session');
    elements('session').append(option);
  }
  await refreshJobs();
  if (parameters.get('job')) { await reopenJob(parameters.get('job')); return; }
  if (result.resume_job) { await reopenJob(result.resume_job); return; }
  if (!result.sessions.length) throw new Error('No sessions found. Open Studio with an exported Copilot session path.');
  status('Choose your output. Privacy is checked before generation.');
});

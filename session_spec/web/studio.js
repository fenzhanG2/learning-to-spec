const access = new URLSearchParams(location.hash.slice(1)).get('access');
const elements = name => document.getElementById(name);
const state = { job: null, review: null, choices: {}, manifest: null, plan: null, busy: false };
function preview(html) {
  elements('human-preview').srcdoc = html;
}
const status = (text, error = false) => { elements('status').textContent = text; elements('status').classList.toggle('error', error); };
const show = stage => {
  for (const name of ['source', 'review', 'output', 'share']) {
    elements(`${name}-panel`).hidden = name !== stage;
    if (name === stage) elements(`step-${name}`).setAttribute('aria-current', 'step');
    else elements(`step-${name}`).removeAttribute('aria-current');
  }
  elements('status').scrollIntoView({ block: 'start' });
};
const node = (tag, text, className) => {
  const element = document.createElement(tag);
  if (text !== undefined) element.textContent = text;
  if (className) element.className = className;
  return element;
};
async function request(path, data) {
  const response = await fetch(path, { method: data === undefined ? 'GET' : 'POST', headers: { Authorization: `Bearer ${access}`, 'Content-Type': 'application/json' }, body: data === undefined ? undefined : JSON.stringify(data), cache: 'no-store' });
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || `Request failed (${response.status})`);
  return result;
}
async function file(name) {
  const response = await fetch(`/api/file?job=${encodeURIComponent(state.job)}&name=${encodeURIComponent(name)}`, { headers: { Authorization: `Bearer ${access}` }, cache: 'no-store' });
  if (!response.ok) throw new Error((await response.json()).error || 'File is not available');
  return response.blob();
}
function action(identifier, operation) {
  elements(identifier).addEventListener('click', async () => {
    if (state.busy) return;
    state.busy = true;
    elements(identifier).disabled = true;
    try { await operation(); } catch (error) { status(error.message, true); }
    finally { state.busy = false; elements(identifier).disabled = false; updateRemaining(); }
  });
}
async function waitJob(stage) {
  for (;;) {
    const result = await request(`/api/status?job=${state.job}`);
    if (result.status === 'error') throw new Error(result.error);
    if (result.status === 'done') return result.result;
    status(`${stage} · Working… Keep this tab and the studio process open. Approved Copilot stages call Copilot; no historical commands are executed.`);
    await new Promise(resolve => setTimeout(resolve, 1800));
  }
}
function updateRemaining() {
  if (!state.review) return;
  const remaining = state.review.findings.filter(finding => !state.choices[finding.id]?.action).length;
  elements('remaining').textContent = `${remaining} decisions remaining`;
  elements('generate').disabled = Boolean(remaining || state.busy || !elements('choices-confirmed').checked);
  elements('publish').disabled = !state.plan || state.busy;
  elements('plan').disabled = !state.manifest || !elements('reviewed-all').checked || Boolean(document.querySelector('[data-finding]:not(:checked)')) || state.busy;
}
function renderFindings() {
  elements('findings').replaceChildren();
  for (const finding of state.review.findings) {
    if (elements('filter').value === 'unresolved' && state.choices[finding.id]?.action) continue;
    const card = node('article', undefined, 'finding');
    card.classList.toggle('decided', Boolean(state.choices[finding.id]?.action));
    const heading = node('div', undefined, 'finding-top');
    heading.append(node('h3', finding.label), node('span', `${finding.occurrences.length} occurrence${finding.occurrences.length === 1 ? '' : 's'}`, 'badge'));
    card.append(heading, node('pre', finding.text), node('p', finding.reason, 'explanation'), node('p', `Task necessity: ${finding.necessity} · Suggested: ${finding.recommended || 'individual choice required'} · ${finding.detectors.join(' + ')} detection`, 'hint'));
    const choice = node('select');
    choice.setAttribute('aria-label', `Decision for ${finding.id}`);
    for (const [value, label] of [['', 'Choose an action…'], ['remove', 'Remove this detail'], ['pseudonymize', 'Use a consistent pseudonym'], ['generalize', 'Generalize in my own words'], ['keep', 'Keep — useful and appropriate']]) {
      const option = node('option', label); option.value = value; choice.append(option);
    }
    choice.value = state.choices[finding.id]?.action || '';
    const replacement = node('textarea');
    replacement.setAttribute('aria-label', `Generalization for ${finding.id}`);
    replacement.placeholder = 'A less revealing equivalent that preserves technical meaning';
    replacement.value = state.choices[finding.id]?.replacement || finding.alternative || '';
    replacement.hidden = choice.value !== 'generalize';
    choice.addEventListener('change', () => {
      elements('choices-confirmed').checked = false;
      state.choices[finding.id] = { action: choice.value, replacement: replacement.value };
      replacement.hidden = choice.value !== 'generalize';
      card.classList.toggle('decided', Boolean(choice.value));
      updateRemaining();
    });
    replacement.addEventListener('input', () => { elements('choices-confirmed').checked = false; state.choices[finding.id] = { action: choice.value, replacement: replacement.value }; updateRemaining(); });
    const details = node('details');
    details.append(node('summary', 'Where this appears'));
    for (const context of finding.contexts || []) details.append(node('pre', context));
    for (const context of finding.related_contexts || []) {
      details.append(node('p', `Related clue · Event ${context.event} · ${context.field.join(' / ')}`), node('pre', context.text));
    }
    for (const occurrence of finding.occurrences) details.append(node('div', `Event ${Number(occurrence.path[0]) + 1} · ${occurrence.path.slice(1).join(' / ')}`));
    card.append(choice, replacement, details);
    elements('findings').append(card);
  }
  updateRemaining();
}
elements('audience').addEventListener('change', () => { elements('team-row').hidden = elements('audience').value !== 'team'; });
elements('delivery').addEventListener('change', () => {
  elements('audience-row').hidden = elements('delivery').value !== 'artifactstore';
  elements('team-row').hidden = elements('delivery').value !== 'artifactstore' || elements('audience').value !== 'team';
});
elements('detection').addEventListener('change', () => { elements('semantic').checked = false; elements('semantic-consent').hidden = elements('detection').value !== 'copilot'; });
elements('choices-confirmed').addEventListener('change', updateRemaining);
elements('filter').addEventListener('change', renderFindings);
action('load-teams', async () => {
  const result = await request('/api/teams', {});
  elements('team-options').replaceChildren();
  for (const team of result.teams || []) { const option = node('option'); option.value = team.slug; option.label = team.name || team.slug; elements('team-options').append(option); }
  status(`Loaded ${(result.teams || []).length} destinations. Access will be checked again before upload.`);
});
action('scan', async () => {
  const delivery = elements('delivery').value;
  const audience = delivery === 'local' ? 'local' : elements('audience').value === 'team' ? `team:${elements('team').value.trim()}` : elements('audience').value;
  const result = await request('/api/scan', { session: elements('session').value, readers: elements('readers').value, delivery, detection: elements('detection').value, audience, purpose: elements('purpose').value, semantic: elements('semantic').checked, custom: elements('custom').value.split('\n').map(value => value.trim()).filter(Boolean) });
  state.job = result.job;
  state.review = null; state.choices = {}; state.plan = null; elements('choices-confirmed').checked = false;
  await waitJob('Scanning disclosures');
  state.review = await request(`/api/review?job=${state.job}`);
  elements('review-summary').textContent = `${state.review.findings.length} grouped findings · ${Object.values(state.review.hard_removals).reduce((sum, value) => sum + value, 0)} hard-removal matches/fields (overlaps possible). Semantic review: ${state.review.semantic.status}. ${state.review.semantic.coverage}`;
  renderFindings(); show('review'); status('Nothing has been uploaded. Choose every finding, then inspect the generated documents.');
});
action('suggestions', async () => {
  elements('choices-confirmed').checked = false;
  let applied = 0;
  for (const finding of state.review.findings) {
    if (finding.recommended && !state.choices[finding.id]?.action) { state.choices[finding.id] = { action: finding.recommended }; applied++; }
  }
  renderFindings(); status(`Applied ${applied} available suggestions without changing your decisions. Uncertain findings still need your individual choice.`);
});
action('change-source', async () => show('source'));
action('generate', async () => {
  await request('/api/generate', { job: state.job, review_id: state.review.review_id, choices: state.choices, confirmed: elements('choices-confirmed').checked });
  state.plan = null; state.manifest = null;
  const result = await waitJob('Generating reduced-source documents');
  await renderOutput(result);
  show('output'); status('Generated from the reduced session. Markdown is a file—not an HTML handoff page.');
});
async function renderOutput(result) {
  const selected = result.files || [];
  for (const button of document.querySelectorAll('[data-file]')) button.hidden = button.dataset.file !== 'deliverables.zip' && !selected.includes(button.dataset.file);
  elements('open-agent').hidden = !selected.includes('agent-spec.md');
  elements('human-preview-panel').hidden = !selected.includes('human-spec.html');
  elements('output-path').textContent = `Selected local files: ${result.output}`;
  elements('output-selection').textContent = `Your selection: ${result.preferences.readers} · ${result.preferences.destination}. Delivered: ${selected.join(', ')}. No upload has happened.`;
  if (selected.includes('human-spec.html')) preview(await (await file('human-spec.html')).text());
  elements('prepare-share').hidden = result.preferences.destination !== 'artifactstore';
}
for (const button of document.querySelectorAll('[data-file]')) button.addEventListener('click', async () => {
  button.disabled = true;
  try {
    const name = button.dataset.file;
    const content = await file(name);
    const url = URL.createObjectURL(content);
    const link = node('a'); link.href = url; link.download = name; document.body.append(link); link.click(); link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 60000);
    status(`Requested ${name}. Check your browser's downloads; the local file path is also shown above.`);
  } catch (error) { status(error.message, true); } finally { button.disabled = false; }
});
action('revise', async () => { state.plan = null; show('review'); });
action('back-output', async () => show('output'));
for (const [identifier, name] of [['open-agent', 'agent-spec.md'], ['open-folder', 'folder']]) action(identifier, async () => {
  const result = await request('/api/open', { job: state.job, name });
  status(`${result.note} File: ${result.requested}`);
});
action('prepare-share', async () => {
  state.manifest = await request('/api/package', { job: state.job, evidence: true });
  state.plan = null; elements('reviewed-all').checked = false; elements('plan-details').hidden = true;
  elements('final-summary').textContent = `Package contains only ${Object.keys(state.manifest.files).join(', ')}. ${state.manifest.findings.length} residual findings need review. Package ${state.manifest.package_id.slice(0,16)}…`;
  elements('final-findings').replaceChildren();
  for (const finding of state.manifest.findings) {
    const row = node('div', undefined, 'final-finding'); const checkbox = node('input'); checkbox.type = 'checkbox'; checkbox.id = finding.id; checkbox.dataset.finding = finding.id;
    const label = node('label', `${finding.file} · ${finding.category}: ${finding.text}`); label.htmlFor = finding.id;
    row.append(checkbox, label); elements('final-findings').append(row);
  }
  show('share'); status('Final byte-level review. A local package is not permission to publish.');
});
action('plan', async () => {
  const acknowledged = [...document.querySelectorAll('[data-finding]:checked')].map(element => element.dataset.finding);
  await request('/api/approve', { job: state.job, package_id: state.manifest.package_id, acknowledged, reviewed_all_files: elements('reviewed-all').checked });
  state.plan = await request('/api/plan', { job: state.job, site: elements('site-name').value.trim() });
  elements('plan-details').textContent = `Destination: ${state.plan.destination}\nNew artifact: ${state.plan.site}\nAudience: ${state.plan.audience}\nFiles: ${state.plan.files.join(', ')}\nViewer policy:\n${JSON.stringify(state.plan.viewer_policy,null,2)}\n\n${state.plan.note}`;
  elements('plan-details').hidden = false; status('Review the exact destination and viewer policy. Upload still requires your confirmation.');
});
for (const identifier of ['site-name', 'reviewed-all']) elements(identifier).addEventListener('input', () => { state.plan = null; updateRemaining(); });
elements('final-findings').addEventListener('change', () => { state.plan = null; updateRemaining(); });
action('publish', async () => {
  await request('/api/publish', { job: state.job, confirm: state.plan.plan_id });
  const result = await waitJob('Uploading approved files');
  elements('published').replaceChildren(node('p', 'Uploaded and read back: the approved authored content is verified. ArtifactStore may add its own HTML security, favicon and editor markup.'));
  for (const [name, url] of Object.entries(result.files || {})) { const link = node('a', name); link.href = url; link.rel = 'noreferrer noopener'; link.target = '_blank'; elements('published').append(link); }
  state.plan = null; status('Publication verified. Keep the private source and review files on your computer.');
});
(async () => {
  try {
    if (!access) throw new Error('Open the complete private URL printed by the studio command. The access fragment is required.');
    const result = await request('/api/sessions');
    for (const session of result.sessions) { const option = node('option', `${session.title} · ${Math.round(session.bytes/1024)} KB`); option.value = session.id; option.selected = session.id === 'selected'; elements('session').append(option); }
    if (result.resume_job) {
      state.job = result.resume_job;
      state.review = await request(`/api/review?job=${state.job}`);
      state.choices = result.resume_choices || {};
      const job = await request(`/api/status?job=${state.job}`);
      await renderOutput(job.result);
      renderFindings(); show('output'); status('Reopened verified local files. No new model call or upload.');
      return;
    }
    if (!result.sessions.length) throw new Error('No local Copilot sessions found. Start the studio with --session PATH for an exported Copilot session.');
    status('Ready. Local scan first; cloud review and uploading are always opt-in.');
  } catch (error) { status(error.message, true); elements('scan').disabled = true; }
})();

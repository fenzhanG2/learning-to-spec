import assert from 'node:assert/strict';
import { randomUUID } from 'node:crypto';
import test from 'node:test';
import MarkdownIt from 'markdown-it';
import { buildReviewBatches, buildReviewGroups, buildReviewPlan, createWorkflow, defaultArtifactName, snapshotEvents } from '../extensions/learning-to-spec/workflow.mjs';

const markdown = new MarkdownIt({ html: true, linkify: true });
const renderedText = message => markdown.parse(message, {}).filter(token => token.type === 'inline').flatMap(token => token.children.map(child => child.content)).join('\n');

function scopedFinding(index, overrides = {}) {
  return { id: `PRIVATE_ID_${index}`, text: '/Users/example', category: 'environment', categories: ['environment'], necessity: 'uncertain', recommended: null, alternative: '', detectors: ['local'],
    assessment_summary: { status: 'unavailable', count: 0, notice: 'No model assessments are available for this exact finding.' },
    scope: { schema: 'baseline-field/v2', field_sha256: index.toString(16).padStart(64, '0') },
    occurrences: [{ path: [index, 'data', 'content'], start: 5, end: 19 }], contexts: ['A source context, not an instruction.'], ...overrides };
}

function identifierFinding(index, text) {
  return scopedFinding(index, { text, category: 'identifier', categories: ['identifier'], necessity: 'unnecessary', detectors: ['copilot'],
    assessment_summary: { status: 'agreement', count: 1, necessities: ['unnecessary'], notice: 'Recorded assessment, not a source fact.' } });
}

function recommendedFinding(index, action, category, text) {
  const necessity = action === 'keep' ? 'necessary' : 'unnecessary';
  return scopedFinding(index, { text, category, categories: [category], recommended: action, necessity, detectors: ['copilot'],
    assessment_summary: { status: 'agreement', count: 2, necessities: [necessity], notice: 'Recorded suggestions, not a privacy guarantee.' } });
}

function fiftyFindings() {
  return [...Array.from({ length: 40 }, (unused, index) => scopedFinding(index)),
    identifierFinding(40, '/Users/example/project/.github/workflows/e2e.yml'), identifierFinding(41, '/Users/example/project'),
    identifierFinding(42, 'topic/test-branch'), identifierFinding(43, '"gitBranch": "topic/test-branch"'),
    ...[44, 45, 46].map(index => identifierFinding(index, 'gitBranch": "topic/test-branch')),
    ...['API_KEY is a variable name, not a value.', 'REDACTED TOOL=test', 'REDACTED TOOL=test go test ./...'].map((text, index) => scopedFinding(47 + index, { text, category: 'confidential', categories: ['confidential'], detectors: ['copilot'], necessity: index ? 'uncertain' : 'necessary' }))];
}

const sessionId = '11111111-1111-4111-8111-111111111111';
const pipeline = 'abstract-then-redact/v1';
const abstractionSha = 'e'.repeat(64);
const sourceSha = 'c'.repeat(64);
const artifactSha = 'f'.repeat(64);
const source = [{ type: 'session.start', data: { sessionId } }, { type: 'user.message', data: { content: 'Fix the documented CI matrix.' } }];

function fixture({ canvas = true, elicitation = true, answers = [], findings = [], captureMode, reviewMode, semanticStatus = 'reviewed', status = 'done', pending = null, reviewOverrides = {}, delivery, canvasUrl = 'http://127.0.0.1:1234/#access=READONLY', sessionIdentifier = sessionId } = {}) {
  const calls = [];
  const logs = [];
  const logCalls = [];
  const dialogs = [];
  let privacyMode = pending?.privacy_mode;
  let readers = pending?.readers;
  let destination = pending?.delivery;
  let audience = pending?.audience;
  const session = { sessionId: sessionIdentifier, capabilities: { ui: { canvases: canvas, elicitation } }, openCanvases: [],
    getEvents: async () => { calls.push(['getEvents']); return [{ ...source[0], data: { sessionId: sessionIdentifier } }, source[1]]; },
    log: async (value, options) => { logs.push(value); logCalls.push({ value, options }); },
    rpc: { canvas: { open: async value => calls.push(['canvas', value]) } },
    ui: { elicitation: async value => { dialogs.push(value); return answers.shift(); }, confirm: async value => { dialogs.push(value); return answers.shift(); }, input: async value => { dialogs.push(value); return answers.shift(); } },
  };
  const bridge = { call: async (operation, data) => {
    calls.push([operation, data]);
    if (operation === 'pending_review') return { review: pending };
    if (operation === 'capture') {
      privacyMode = data.privacy_mode;
      return { session_id: sessionIdentifier, source: 'current', privacy_mode: captureMode === undefined ? privacyMode : captureMode, capture_order: pipeline, sha256: sourceSha };
    }
    if (operation === 'output_canvas' || operation === 'progress_canvas') return { url: canvasUrl };
    if (operation === 'scan') { readers = data.readers; destination = data.delivery; audience = data.audience; return { job: 'a'.repeat(32) }; }
    if (operation === 'generate') return { job: 'a'.repeat(32) };
    if (operation === 'status') return { status };
    if (operation === 'review') return {
      review_id: pending?.review_id || 'review', privacy_mode: reviewMode === undefined ? privacyMode : reviewMode,
      pipeline, abstraction_sha256: abstractionSha, original_source_sha256: sourceSha,
      source_sha256: artifactSha, preferences: { readers, destination }, audience,
      semantic: { status: privacyMode === 'llm' ? semanticStatus : 'not_requested' }, findings, ...reviewOverrides,
    };
    if (operation === 'deliverables') return delivery ?? { files: ({ human: ['human-spec.html'], agent: ['agent-spec.md', 'evidence.md'], both: ['human-spec.html', 'agent-spec.md', 'evidence.md'] }[readers]).map(name => ({ name, path: '/private/' + name })), bundle: { path: '/private/deliverables.zip' } };
    throw new Error('Unexpected operation');
  } };
  return { calls, logs, logCalls, dialogs, session, bridge, workflow: createWorkflow({ getSession: () => session, getBridge: () => bridge }) };
}

function savedReview(privacyMode = 'llm', findings = 1) {
  return {
    job: 'a'.repeat(32), review_id: 'b'.repeat(64), snapshot_sha256: sourceSha,
    pipeline, abstraction_sha256: abstractionSha, review_source_sha256: artifactSha,
    privacy_mode: privacyMode, readers: 'both', delivery: 'local', audience: 'local', findings,
  };
}

for (const privacyMode of ['full', 'llm']) {
  test(`${privacyMode} saved review resumes only explicitly with no capture, rescan or inherited approvals`, async () => {
    const smart = privacyMode === 'llm';
    const pending = savedReview(privacyMode, smart ? 1 : 0);
    const { workflow, calls, dialogs, logs } = fixture({ canvas: false, pending,
      findings: smart ? [{ id: 'F1', text: 'PRIVATE_RESUME_QUOTE' }] : [],
      answers: [{ action: 'accept', content: { continuation: 'resume' } },
        ...(smart ? [{ action: 'accept', content: { finding_1: 'remove' } }] : []), true],
    });
    const result = await workflow.tool.handler({}, { sessionId });
    assert.deepEqual(calls.map(call => call[0]), ['pending_review', 'review', 'generate', 'status', 'deliverables']);
    assert.deepEqual(calls.find(call => call[0] === 'review')[1], { job: pending.job });
    assert.deepEqual(calls.find(call => call[0] === 'generate')[1], {
      job: pending.job, review_id: pending.review_id, choices: smart ? { F1: { action: 'remove' } } : {}, confirmed: true, pipeline, abstraction_sha256: abstractionSha,
    });
    assert.match(dialogs[0].message, /not messages added later/);
    assert.match(dialogs[0].message, /Incomplete prior choices are not assumed/);
    assert.equal(dialogs[0].requestedSchema.properties.continuation.default, undefined);
    assert.equal(dialogs.length, smart ? 3 : 2);
    assert.match(logs.join('\n'), /Stage 1 of 3: reusing the bound saved preparation/);
    assert.equal(JSON.stringify(result).includes('PRIVATE_RESUME_QUOTE'), false);
    assert.equal(JSON.stringify(logs).includes('PRIVATE_RESUME_QUOTE'), false);
    if (!smart) assert.match(dialogs[0].message, /personal details, internal information or secrets/);
  });
}

test('cancelled or invalid continuation never reads the saved findings or rescans', async () => {
  for (const answer of [
    { action: 'cancel' },
    { action: 'accept', content: { continuation: 'keep-all' } },
    { action: 'accept', content: { continuation: 'resume', privacyMode: 'full' } },
  ]) {
    const pending = savedReview();
    const { workflow, calls } = fixture({ pending, answers: [answer] });
    if (answer.action === 'cancel') assert.deepEqual(await workflow.run({ sessionId }), { cancelled: true, job: pending.job });
    else await assert.rejects(workflow.run({ sessionId }), /Invalid saved-review choice/);
    assert.deepEqual(calls, [['pending_review', {}]]);
  }
});

test('explicit new snapshot choice reopens all settings without reusing or deleting a saved job', async () => {
  const { workflow, calls, dialogs } = fixture({ pending: savedReview(), answers: [
    { action: 'accept', content: { continuation: 'new' } }, { action: 'cancel' },
  ] });
  assert.deepEqual(await workflow.run({ sessionId }), { cancelled: true });
  assert.deepEqual(dialogs[1].requestedSchema.required, ['readers', 'delivery', 'privacyMode']);
  assert.deepEqual(calls, [['pending_review', {}]]);
});

test('resume binds review identity, snapshot, mode, settings, audience and finding count', async () => {
  for (const reviewOverrides of [
    { review_id: 'changed' }, { source_sha256: 'd'.repeat(64) }, { privacy_mode: 'full' }, { privacy_mode: undefined },
    { original_source_sha256: 'd'.repeat(64) }, { pipeline: undefined }, { abstraction_sha256: 'd'.repeat(64) },
    { preferences: { readers: 'human', destination: 'local' } },
    { preferences: { readers: 'both', destination: 'artifactstore' } },
    { audience: 'root' }, { findings: [] },
  ]) {
    const { workflow, calls, dialogs } = fixture({ pending: savedReview(), reviewOverrides,
      findings: [{ id: 'F1', text: 'private' }], answers: [{ action: 'accept', content: { continuation: 'resume' } }],
    });
    await assert.rejects(workflow.run({ sessionId }), /changed after selection/);
    assert.deepEqual(calls.map(call => call[0]), ['pending_review', 'review']);
    assert.equal(dialogs.length, 1);
  }
});

test('invalid pending metadata or unavailable discovery fails visibly without starting a new scan', async () => {
  for (const changes of [
    { privacy_mode: undefined }, { privacy_mode: 'local' }, { job: '../other' }, { snapshot_sha256: 'invalid' },
    { pipeline: undefined }, { abstraction_sha256: 'invalid' }, { review_source_sha256: 'invalid' },
    { findings: -1 }, { findings: 1.5 }, { privacy_mode: 'full', findings: 1 },
    { audience: 'root' }, { readers: 'all' }, { delivery: 'other' },
  ]) {
    const { workflow, calls, dialogs } = fixture({ pending: { ...savedReview(), ...changes } });
    await assert.rejects(workflow.run({ sessionId }), /identity or settings/);
    assert.deepEqual(calls, [['pending_review', {}]]);
    assert.deepEqual(dialogs, []);
  }
  const { workflow, bridge, dialogs } = fixture();
  bridge.call = async operation => {
    assert.equal(operation, 'pending_review');
    throw new Error('Saved-review service unavailable');
  };
  await assert.rejects(workflow.run({ sessionId }), /service unavailable/);
  assert.deepEqual(dialogs, []);
});

test('legacy scan-first metadata only offers a new explicitly consented snapshot', async () => {
  const { workflow, bridge, calls, dialogs, logs } = fixture({ answers: [{ action: 'cancel' }] });
  const original = bridge.call;
  bridge.call = async (operation, data) => operation === 'pending_review'
    ? { review: null, legacy_reviews_unavailable: 2 } : original(operation, data);
  assert.deepEqual(await workflow.run({ sessionId }), { cancelled: true });
  assert.deepEqual(calls, []);
  assert.equal(dialogs.length, 1);
  assert.match(logs.join('\n'), /Older scan-first reviews are preserved but cannot resume/);
  assert.match(dialogs[0].message, /BEFORE redaction/);
});

test('fresh preparation requires matching original source, artifact and abstract-first provenance', async () => {
  for (const reviewOverrides of [{ original_source_sha256: 'd'.repeat(64) }, { source_sha256: undefined },
    { pipeline: undefined }, { pipeline: 'scan-first' }, { abstraction_sha256: undefined },
    { preferences: { readers: 'human', destination: 'local' } }, { audience: 'root' }]) {
    const { workflow, calls } = fixture({ reviewOverrides, answers: [
      { action: 'accept', content: { readers: 'both', delivery: 'local', privacyMode: 'full' } },
    ] });
    await assert.rejects(workflow.run({ sessionId }), /captured source or export settings|bound abstract-first draft/);
    assert.equal(calls.some(call => call[0] === 'generate'), false);
  }
});

test('old capture ordering or missing byte hash fails before provider preparation', async () => {
  for (const changes of [{ capture_order: undefined }, { capture_order: 'scan-first' }, { sha256: undefined }]) {
    const { workflow, bridge, calls } = fixture({ answers: [
      { action: 'accept', content: { readers: 'both', delivery: 'local', privacyMode: 'llm' } },
    ] });
    const original = bridge.call;
    bridge.call = async (operation, data) => {
      const result = await original(operation, data);
      return operation === 'capture' ? { ...result, ...changes } : result;
    };
    await assert.rejects(workflow.run({ sessionId }), /abstract-first capture/);
    assert.equal(calls.some(call => call[0] === 'scan'), false);
  }
});

test('session changes during a resume choice cannot read or act on the saved job', async () => {
  const { workflow, calls, session } = fixture({ pending: savedReview() });
  session.ui.elicitation = async () => {
    session.sessionId = 'another-session';
    return { action: 'accept', content: { continuation: 'resume' } };
  };
  await assert.rejects(workflow.run({ sessionId }), /identity/);
  assert.deepEqual(calls, [['pending_review', {}]]);
});

test('resumed review still needs successful Smart review and a new final generation confirmation', async () => {
  const failed = fixture({ pending: savedReview(), semanticStatus: 'failed', findings: [{ id: 'F1', text: 'private' }],
    answers: [{ action: 'accept', content: { continuation: 'resume' } }],
  });
  await assert.rejects(failed.workflow.run({ sessionId }), /rules-only fallback/);
  assert.deepEqual(failed.calls.map(call => call[0]), ['pending_review', 'review']);
  const declined = fixture({ pending: savedReview('full', 0), answers: [{ action: 'accept', content: { continuation: 'resume' } }, false] });
  assert.equal((await declined.workflow.run({ sessionId })).cancelled, true);
  assert.deepEqual(declined.calls.map(call => call[0]), ['pending_review', 'review']);
});

test('/to-spec is registered as code, not a prompt', () => {
  const { workflow } = fixture();
  assert.equal(workflow.command.name, 'to-spec');
  assert.deepEqual(workflow.tool.parameters.properties, {});
  assert.equal(workflow.tool.parameters.additionalProperties, false);
});

test('fifty compatible scoped findings form one explicit group retaining all source members', () => {
  const findings = Array.from({ length: 50 }, (unused, index) => ({
    id: `F${index + 1}`, text: 'same exact phrase', label: 'Check possible disclosure',
    scope: { schema: 'baseline-field/v2', field_sha256: String(index).padStart(64, '0') },
    occurrences: [{ path: [index, 'data', 'content'], start: 5, end: 22 }],
    contexts: [`context for field ${index}`], recommended: 'remove',
  }));
  const before = JSON.stringify(findings);
  const batches = buildReviewBatches(findings);
  assert.equal(batches.length, 1);
  assert.equal(JSON.stringify(findings), before);
  assert.deepEqual(batches.flatMap(batch => batch.entries.flatMap(entry => entry.members.map(member => member.finding.id))), findings.map(finding => finding.id));
  for (const [index, batch] of batches.entries()) {
    assert.equal(Object.keys(batch.properties).length, 1);
    assert.match(batch.message, new RegExp(`Batch ${index + 1} of 1`));
    assert.match(batch.message, /Stage 2 of 3/);
    assert.match(batch.message, /technical failures and corrections/);
    for (const entry of batch.entries) {
      const field = batch.properties[entry.key];
      assert.deepEqual(field.enum, ['keep', 'remove', 'pseudonymize', 'generalize', 'individually']);
      assert.equal(field.default, 'keep');
      assert.ok(renderedText(batch.message).includes(entry.finding.text));
      assert.match(batch.message, /50 finding\(s\) · 50 listed occurrence/);
      assert.doesNotMatch(batch.message, /field_sha256|ID:|\{"|[a-f0-9]{64}/);
      assert.match(batch.message, /Source items \(50\): 1–50/);
      assert.doesNotMatch(batch.message, /offsets|Finding \d+:/);
    }
  }
});

test('batch payload bounds shorten only context, never quotes or scoped identities', () => {
  const findings = Array.from({ length: 7 }, (unused, index) => ({
    id: `F${index}`, text: 'exact quote 中🙂 '.repeat(240), label: 'Disclosure',
    contexts: ['context '.repeat(500), 'second', 'third'],
    related_contexts: [{ event: 2, field: ['data', 'content'], text: 'related '.repeat(500) }],
    assessment_summary: { notice: 'Model assessments disagree; decide from the source.' },
  }));
  const batches = buildReviewBatches(findings);
  assert.ok(batches.length > 1);
  assert.equal(batches.flatMap(batch => batch.entries).length, 7);
  for (const batch of batches) {
    const payload = { message: batch.message, requestedSchema: { type: 'object', properties: batch.properties, required: Object.keys(batch.properties) } };
    assert.ok(Buffer.byteLength(JSON.stringify(payload), 'utf8') <= 24000);
    assert.ok(batch.entries.length <= 5);
    for (const entry of batch.entries) assert.ok(batch.message.includes(entry.finding.text));
    assert.match(batch.message, /Context shortened/);
    assert.match(batch.message, /Related context \(not a removal target\)/);
    assert.match(batch.message, /Model assessments disagree/);
  }
});

test('oversized quotes and duplicate identities fail closed instead of skipping findings', () => {
  assert.deepEqual(buildReviewBatches([]), []);
  assert.throws(() => buildReviewBatches([{ id: 'F1', text: 'x'.repeat(25000) }]), /size limit/);
  assert.throws(() => buildReviewBatches([{ id: 'F1', text: 'first' }, { id: 'F1', text: 'second' }]), /duplicate finding identity/);
  assert.throws(() => buildReviewBatches([{ id: 'F1' }]), /Invalid/);
  assert.throws(() => buildReviewBatches(null), /Invalid saved findings/);
});

test('supported clear recommendations default to keep technical context, remove asides and pseudonymize identifiers', () => {
  const findings = [recommendedFinding(0, 'keep', 'confidential', 'The test failed; the correction still needs verification.'),
    recommendedFinding(1, 'keep', 'confidential', 'Security warning: API_KEY is a variable name, not a value.'),
    recommendedFinding(2, 'remove', 'personal_life', 'An unrelated private aside.'),
    recommendedFinding(3, 'pseudonymize', 'identifier', 'person@example.test')];
  const before = structuredClone(findings);
  const properties = buildReviewBatches(findings)[0].properties;
  assert.deepEqual(Object.values(properties).map(property => property.default), ['keep', 'keep', 'remove', 'pseudonymize']);
  for (const property of Object.values(properties)) assert.ok(property.enum.includes(property.default));
  assert.deepEqual(findings, before);
});

test('policy defaults remain valid without treating ambiguous recommendations as facts or inventing wording', () => {
  const base = recommendedFinding(0, 'remove', 'reputation', 'An optional private aside.');
  for (const changes of [
    { recommended: null }, { recommended: 'approve' }, { recommended: {} }, { recommended: 'generalize', alternative: '' },
    { recommended: 'generalize', alternative: 'Model-proposed wording is not user input.' },
    { allowed_actions: ['keep', 'pseudonymize'] }, { necessity: 'necessary' }, { necessity: 'uncertain' },
    { recommended: 'keep' }, { assessment_summary: undefined },
    ...['disagreement', 'uncertain', 'unavailable', 'invalid'].map(status => ({ assessment_summary: { ...base.assessment_summary, status } })),
    { assessment_summary: { ...base.assessment_summary, count: 0 } },
    { assessment_summary: { ...base.assessment_summary, count: true } },
    { assessment_summary: { ...base.assessment_summary, necessities: ['necessary', 'unnecessary'] } },
  ]) {
    const property = Object.values(buildReviewBatches([{ ...base, ...changes }])[0].properties)[0];
    assert.ok(property.enum.includes(property.default), JSON.stringify(changes));
    assert.notEqual(property.default, 'generalize');
    assert.doesNotMatch(JSON.stringify(property), /Model-proposed wording/);
    if (changes.necessity === 'necessary' || changes.assessment_summary?.status === 'disagreement'
      || changes.assessment_summary?.necessities?.includes('necessary')) assert.equal(property.default, 'keep');
  }
});

test('accepting displayed defaults preserves every scoped ID and still requires final confirmation', async () => {
  const findings = [recommendedFinding(0, 'keep', 'confidential', 'Retain the failed test and correction.'),
    recommendedFinding(1, 'keep', 'confidential', 'Retain the failed test and correction.'),
    recommendedFinding(2, 'remove', 'personal_life', 'An unrelated private aside.'),
    recommendedFinding(3, 'pseudonymize', 'identifier', 'person@example.test')];
  for (const confirmed of [false, true]) {
    const { workflow, calls, session } = fixture({ canvas: false, findings });
    let forms = 0;
    session.ui.elicitation = async request => {
      forms += 1;
      const properties = request.requestedSchema.properties;
      for (const property of Object.values(properties)) assert.ok(property.enum.includes(property.default));
      if (forms === 2) assert.deepEqual(Object.keys(properties), ['plan']);
      return { action: 'accept', content: Object.fromEntries(Object.entries(properties).map(([key, property]) => [key, property.default])) };
    };
    session.ui.confirm = async () => confirmed;
    await workflow.run({ sessionId });
    assert.equal(forms, 2);
    const dispatched = calls.filter(call => call[0] === 'generate');
    assert.equal(dispatched.length, confirmed ? 1 : 0);
    if (confirmed) assert.deepEqual(dispatched[0][1], { job: 'a'.repeat(32), review_id: 'review', confirmed: true,
      pipeline, abstraction_sha256: abstractionSha,
      choices: Object.fromEntries(findings.map(finding => [finding.id, { action: finding.recommended }])) });
    assert.equal(calls.some(call => call[0] === 'publish'), false);
  }
});

test('schema defaults are not filled into missing or cancelled human responses', async () => {
  for (const response of [{ action: 'accept', content: {} }, { action: 'cancel' }]) {
    const finding = recommendedFinding(0, 'keep', 'confidential', 'Retain the failed check.');
    const { workflow, calls } = fixture({ findings: [finding], answers: [
      { action: 'accept', content: { readers: 'both', delivery: 'local', privacyMode: 'llm' } }, response,
    ] });
    if (response.action === 'accept') await assert.rejects(workflow.run({ sessionId }), /required choice/);
    else assert.equal((await workflow.run({ sessionId })).cancelled, true);
    assert.equal(calls.some(call => call[0] === 'generate'), false);
  }
});

test('fresh and saved real stage transitions request persistent public logs', async () => {
  for (const resume of [false, true]) {
    const { workflow, logCalls } = fixture({ canvas: false, pending: resume ? savedReview('full', 0) : null, answers: [
      { action: 'accept', content: resume ? { continuation: 'resume' } : { readers: 'both', delivery: 'local', privacyMode: 'full' } }, true,
    ] });
    await workflow.run({ sessionId });
    for (const stage of [1, 2, 3]) {
      const messages = logCalls.filter(call => call.value.includes(`Stage ${stage} of 3`));
      assert.equal(messages.length, 1);
      assert.equal(messages[0].options.ephemeral, false);
    }
  }
});

test('unchanged polling stages produce no heartbeat logs or backend payload disclosure', async () => {
  const context = fixture({ canvas: false, answers: [
    { action: 'accept', content: { readers: 'both', delivery: 'local', privacyMode: 'full' } }, false,
  ] });
  const original = context.bridge.call;
  let polls = 0;
  context.bridge.call = async (operation, data) => {
    if (operation === 'status' && polls++ < 3) return { status: 'running', stage: 'PRIVATE_BACKEND_STAGE', detail: 'PRIVATE_PAYLOAD' };
    return original(operation, data);
  };
  const workflow = createWorkflow({ getSession: () => context.session, getBridge: () => context.bridge, wait: async () => {} });
  await workflow.run({ sessionId });
  const running = context.logCalls.filter(call => call.value.endsWith('is running.'));
  assert.equal(running.length, 1);
  assert.equal(running[0].options.ephemeral, false);
  assert.match(running[0].value, /private Copilot draft/);
  assert.doesNotMatch(JSON.stringify(context.logs), /PRIVATE_BACKEND_STAGE|PRIVATE_PAYLOAD|\d+%/);
});

test('preparation phase codes map to fixed visible labels only when reported and deduplicate polls', async () => {
  const context = fixture({ canvas: false, answers: [
    { action: 'accept', content: { readers: 'both', delivery: 'local', privacyMode: 'full' } }, false,
  ] });
  const phases = ['draft', 'draft', 'checking', 'repair', 'repair', 'checking', 'privacy', 'privacy'];
  const original = context.bridge.call;
  context.bridge.call = async (operation, data) => {
    if (operation === 'status' && phases.length) return { status: 'running', stage: 'scan', phase: phases.shift(), detail: 'PRIVATE_CHECKPOINT_PATH', note: 'PRIVATE_MODEL_TEXT' };
    return original(operation, data);
  };
  const workflow = createWorkflow({ getSession: () => context.session, getBridge: () => context.bridge, wait: async () => {} });
  await workflow.run({ sessionId });
  const progress = context.logCalls.filter(call => call.value.endsWith('is running.'));
  assert.deepEqual(progress.map(call => call.value), ['Drafting the private spec', 'Checking the private draft',
    'Repairing the private draft', 'Checking the private draft', 'Reviewing draft privacy']
    .map(label => `Learning to Spec · Stage 1 of 3: ${label} is running.`));
  assert.equal(progress.every(call => call.options.ephemeral === false), true);
  assert.doesNotMatch(JSON.stringify(context.logs), /PRIVATE_CHECKPOINT_PATH|PRIVATE_MODEL_TEXT|\d+%/);
});

test('missing unknown malformed prototype or wrong-stage phase values never enter visible logs', async () => {
  const context = fixture({ canvas: false, answers: [
    { action: 'accept', content: { readers: 'both', delivery: 'local', privacyMode: 'full' } }, false,
  ] });
  const snapshots = [undefined, null, {}, ['draft'], '__proto__', 'constructor', 'PRIVATE_STAGE_NAME'].map(phase => ({ stage: 'scan', phase }));
  snapshots.push({ stage: 'generate', phase: 'privacy' });
  const original = context.bridge.call;
  context.bridge.call = async (operation, data) => {
    if (operation === 'status' && snapshots.length) return { status: 'running', ...snapshots.shift() };
    return original(operation, data);
  };
  const workflow = createWorkflow({ getSession: () => context.session, getBridge: () => context.bridge, wait: async () => {} });
  await workflow.run({ sessionId });
  const progress = context.logs.filter(message => message.endsWith('is running.'));
  assert.equal(progress.length, 1);
  assert.doesNotMatch(progress[0], /PRIVATE_STAGE_NAME|constructor|__proto__|Reviewing draft privacy|\[object/);
});

test('terminal preparation timeout has fixed friendly wording and never restarts or echoes diagnostics', async () => {
  const context = fixture({ answers: [
    { action: 'accept', content: { readers: 'both', delivery: 'local', privacyMode: 'llm' } },
  ] });
  const original = context.bridge.call;
  context.bridge.call = async (operation, data) => operation === 'status'
    ? { status: 'error', stage: 'scan', error_code: 'timeout', error: 'PRIVATE_ERROR_PATH', phase: 'PRIVATE_PHASE' }
    : original(operation, data);
  await context.workflow.command.handler({ sessionId, args: '' });
  const errors = context.logCalls.filter(call => call.options?.level === 'error');
  assert.equal(errors.length, 1);
  assert.match(errors[0].value, /Preparation timed out/);
  assert.match(errors[0].value, /configured time limit/);
  assert.doesNotMatch(errors[0].value, /600|300|10 minutes|5 minutes|stopped|terminated/);
  assert.match(errors[0].value, /Any saved work is preserved/);
  assert.match(errors[0].value, /Nothing restarted automatically and no upload was started/);
  assert.match(errors[0].value, /privacy forms does not count/);
  assert.doesNotMatch(JSON.stringify(context.logs), /PRIVATE_ERROR_PATH|PRIVATE_PHASE/);
  assert.equal(context.calls.filter(call => call[0] === 'scan').length, 1);
  assert.equal(context.calls.some(call => ['review', 'generate', 'publish'].includes(call[0])), false);
});

test('unknown or nonterminal timeout markers never claim a diagnosed preparation timeout', async () => {
  const snapshots = [
    ...[undefined, null, {}, ['timeout'], 'TIMEOUT', 'PRIVATE_TIMEOUT'].map(error_code => ({ status: 'error', stage: 'scan', error_code })),
    { status: 'error', stage: 'generate', error_code: 'timeout' },
    { status: 'error', error_code: 'timeout' },
    { status: 'running', stage: 'scan', error_code: 'timeout' },
  ];
  for (const snapshot of snapshots) {
    const context = fixture({ answers: [
      { action: 'accept', content: { readers: 'both', delivery: 'local', privacyMode: 'full' } }, false,
    ] });
    const original = context.bridge.call;
    let first = true;
    context.bridge.call = async (operation, data) => {
      if (operation === 'status' && first) { first = false; return { ...snapshot, error: 'PRIVATE_DIAGNOSTIC' }; }
      return original(operation, data);
    };
    const workflow = createWorkflow({ getSession: () => context.session, getBridge: () => context.bridge, wait: async () => {} });
    await workflow.command.handler({ sessionId, args: '' });
    assert.doesNotMatch(JSON.stringify(context.logs), /Preparation timed out|PRIVATE_DIAGNOSTIC|PRIVATE_TIMEOUT/);
    assert.equal(context.calls.some(call => ['generate', 'publish'].includes(call[0])), false);
  }
});

test('tool invocation returns safe bounded failure details instead of an opaque host exception', async () => {
  for (const code of ['timeout', 'call_budget', 'cleanup_unconfirmed', 'export_failed', 'draft_references_invalid', 'draft_structure_invalid']) {
    const context = fixture({ answers: [{ action: 'accept', content: { readers: 'both', delivery: 'local', privacyMode: 'llm' } }] });
    const original = context.bridge.call;
    context.bridge.call = async (operation, data) => operation === 'status'
      ? { status: 'error', stage: 'scan', error_code: code, error: 'PRIVATE_DIAGNOSTIC_CANARY' }
      : original(operation, data);
    const response = await context.workflow.tool.handler({}, { sessionId });
    const result = JSON.parse(response.textResultForLlm);
    assert.equal(response.resultType, 'success');
    assert.equal(result.status, 'not_exported');
    assert.equal(result.error_code, code);
    if (code.startsWith('draft_')) {
      assert.match(result.message, /One automatic repair was attempted/);
      assert.match(result.message, /\/to-spec again.*additional model calls/);
      assert.match(result.message, code === 'draft_references_invalid' ? /source citations/ : /invalid draft format/);
    }
    assert.match(result.next_action, /Do not retry/);
    assert.doesNotMatch(response.textResultForLlm, /PRIVATE_DIAGNOSTIC_CANARY/);
    assert.equal(context.calls.filter(call => call[0] === 'scan').length, 1);
    assert.equal(context.calls.some(call => ['review', 'generate', 'publish'].includes(call[0])), false);
  }
});

test('unvalidated draft is returned locally without review approval, regeneration or upload, including CLI hosts', async () => {
  for (const canvas of [true, false]) {
    const context = fixture({ canvas, answers: [{ action: 'accept', content: { readers: 'both', delivery: 'artifactstore', privacyMode: 'llm' } }, 'Just me · root'] });
    context.session.ui.select = async () => 'Just me · root';
    const original = context.bridge.call;
    context.bridge.call = async (operation, data) => {
      if (operation === 'status') return { status: 'error', stage: 'scan', error_code: 'draft_quality_invalid', draft_available: true, error: 'PRIVATE_SOURCE' };
      if (operation === 'deliverables') return { kind: 'unvalidated_draft', folder: 'C:/Synthetic/draft', files: [] };
      return original(operation, data);
    };
    const response = await context.workflow.tool.handler({}, { sessionId });
    const result = JSON.parse(response.textResultForLlm);
    assert.equal(result.status, 'draft_available');
    assert.equal(result.privacy, 'incomplete');
    assert.equal(result.upload_allowed, false);
    assert.doesNotMatch(response.textResultForLlm, /PRIVATE_SOURCE|C:\/Synthetic/);
    assert.match(renderedText(context.logs.join('\n')), /C:\/Synthetic\/draft/);
    assert.match(context.logs.join('\n'), /not as a verified spec/);
    assert.equal(context.calls.some(call => ['review', 'generate', 'package', 'plan', 'publish'].includes(call[0])), false);
    const output = await context.workflow.canvas.open({ sessionId });
    assert.match(output.status, /no upload authority/);
    let options;
    context.session.ui.select = async (title, choices) => { options = choices; return 'Open saved files'; };
    await context.workflow.run({ sessionId });
    assert.deepEqual(options, ['Open saved files', 'Create a new snapshot']);
  }
});

test('publication failure preserves local files and reports an unknown remote outcome without leaking diagnostics or retrying', async () => {
  for (const remoteState of [{ stage: 'publish', error_code: 'PRIVATE_PROVIDER_CODE' }, { stage: 'scan', error_code: 'timeout' }]) {
    const pending = { ...savedReview('full', 0), delivery: 'artifactstore', audience: 'root' };
    const context = fixture({ canvas: false, pending, answers: [
      { action: 'accept', content: { continuation: 'resume' } }, true,
      { action: 'accept', content: { site: 'synthetic-site' } }, { action: 'accept', content: { upload_action: 'upload' } },
    ] });
    const original = context.bridge.call;
    let published = false;
    context.bridge.call = async (operation, data) => {
      if (['package', 'plan', 'publish'].includes(operation) || (operation === 'status' && published)) {
        context.calls.push([operation, data]);
        if (operation === 'package') return { package_id: 'PRIVATE_PACKAGE_ID', audience: 'root', findings: [], files: { 'agent-spec.md': {} } };
        if (operation === 'plan') return { plan_id: 'PRIVATE_PLAN_ID', package_id: 'PRIVATE_PACKAGE_ID', audience: 'root', site: data.site, team: null };
        if (operation === 'publish') { published = true; return { job: pending.job }; }
        return { status: 'error', ...remoteState, error: 'PRIVATE_DIAGNOSTIC_CANARY', job: 'PRIVATE_JOB_CANARY', path: '/PRIVATE_PATH' };
      }
      return original(operation, data);
    };
    const response = await context.workflow.tool.handler({}, { sessionId });
    const result = JSON.parse(response.textResultForLlm);
    assert.equal(response.resultType, 'success');
    assert.deepEqual(Object.keys(result).sort(), ['error_code', 'message', 'next_action', 'status']);
    assert.equal(result.status, 'not_published');
    assert.equal(result.error_code, 'publication_unconfirmed');
    assert.match(result.message, /Local files remain available/);
    assert.match(result.message, /remote publication outcome is unknown/);
    assert.match(result.message, /partially or fully completed/);
    assert.match(result.next_action, /Do not retry blindly/);
    assert.match(result.next_action, /Check the remote outcome first/);
    assert.doesNotMatch(response.textResultForLlm, /PRIVATE_|a{32}|not_exported|No completed output|no .*upload was performed/);
    assert.deepEqual(context.calls.map(call => call[0]), ['pending_review', 'review', 'generate', 'status', 'deliverables', 'package', 'plan', 'publish', 'status']);
    assert.equal(context.logs.some(value => value.startsWith('Your spec is ready.')), true);
    const preview = await context.workflow.canvas.open({ sessionId });
    assert.equal(preview.status, 'Read-only selected files · no upload authority');
    assert.deepEqual(context.calls.at(-1), ['output_canvas', { job: pending.job }]);
  }
});

test('unconfirmed worker cleanup warns against retry without claiming the process stopped', async () => {
  const context = fixture({ answers: [
    { action: 'accept', content: { readers: 'both', delivery: 'local', privacyMode: 'llm' } },
  ] });
  const original = context.bridge.call;
  context.bridge.call = async (operation, data) => operation === 'status'
    ? { status: 'error', stage: 'scan', error_code: 'cleanup_unconfirmed', error: 'PRIVATE_CLEANUP_PAYLOAD' }
    : original(operation, data);
  await context.workflow.command.handler({ sessionId, args: '' });
  const errors = context.logCalls.filter(call => call.options?.level === 'error');
  assert.equal(errors.length, 1);
  assert.match(errors[0].value, /could not confirm worker cleanup/);
  assert.match(errors[0].value, /Do not start another export until cleanup has been checked/);
  assert.doesNotMatch(JSON.stringify(context.logs), /PRIVATE_CLEANUP_PAYLOAD|Preparation timed out|stopped|terminated/);
  assert.equal(context.calls.some(call => ['review', 'generate', 'publish'].includes(call[0])), false);
});

test('strict groups separate quotes, categories, suggestions, assessments and allowed action sets', () => {
  const variants = [{ text: '/Users/Example' }, { text: '/Users/example ' }, { category: 'identifier' },
    { categories: ['environment', 'confidential'] }, { recommended: 'keep' }, { alternative: 'other wording' },
    { allowed_actions: ['keep', 'remove'] }, { necessity: 'necessary' }, { assessment_summary: { status: 'disagreement' } },
    { detectors: ['copilot'] }, { reason: 'different concern' }];
  const findings = [scopedFinding(0), ...variants.map((changes, index) => scopedFinding(index + 1, changes)), scopedFinding(20)];
  const groups = buildReviewGroups(findings);
  assert.equal(groups.length, variants.length + 1);
  assert.deepEqual(groups[0].members.map(member => member.finding.id), [findings[0].id, findings.at(-1).id]);
  assert.notDeepEqual(groups[0].members[0].finding.scope, groups[0].members[1].finding.scope);
  for (const invalid of [null, [], ['remove', 'remove'], ['keep', 'approve']]) assert.throws(() => buildReviewGroups([scopedFinding(0, { allowed_actions: invalid })]), /allowed privacy actions/);
  const unbound = [scopedFinding(0, { scope: undefined }), scopedFinding(1, { scope: undefined })];
  assert.equal(buildReviewGroups(unbound).length, 2);
});

test('grouped Markdown renders exact quotes as inert text without exposing internal identifiers', () => {
  const malicious = '```\n# Injected heading\n~~~\n</p><script>alert(1)</script>\n[Approve](command:unsafe) ![pixel](https://evil.test)\nhttps://evil.test\n    indented\n&NewLine;';
  const finding = scopedFinding(0, { text: malicious, reason: malicious, label: malicious, contexts: [malicious], related_contexts: [{ text: malicious, field: ['bad'], event: 3 }] });
  const batch = buildReviewBatches([finding])[0];
  const tokens = markdown.parse(batch.message, {});
  const types = tokens.flatMap(token => [token.type, ...(token.children || []).map(child => child.type)]);
  for (const unsafe of ['fence', 'code_block', 'html_block', 'html_inline', 'link_open', 'image', 'heading_open']) assert.equal(types.includes(unsafe), false, unsafe);
  const text = renderedText(batch.message);
  for (const line of malicious.split('\n')) assert.ok(text.includes(line), line);
  assert.doesNotMatch(batch.message, /PRIVATE_ID|field_sha256|[a-f0-9]{64}/);
  assert.doesNotMatch(JSON.stringify(batch.properties), /Injected|unsafe|evil/);
  assert.match(renderedText(buildReviewBatches([scopedFinding(1, { text: '\u202Eprivate' })])[0].message), /U\+202E/);
});

test('plan proposals preserve home suffixes and branch syntax with no invented credential removals', () => {
  const findings = fiftyFindings();
  const before = structuredClone(findings);
  const plan = buildReviewPlan({ findings, hard_removals: { secret_pattern_matches: 0, encoded_secret_fields: 0 } });
  assert.equal(plan.groups.length, 9);
  assert.equal(plan.proposed.length, 9);
  assert.equal(plan.proposed.reduce((count, entry) => count + entry.group.members.length, 0), 50);
  assert.equal(plan.manual.length, 0);
  assert.equal(plan.available, true);
  assert.deepEqual(findings, before);
  assert.deepEqual(plan.properties.plan.enum, ['apply', 'customize']);
  assert.deepEqual(plan.properties.plan.enumNames, ['Apply this plan', 'Inspect details / customize']);
  assert.equal(plan.properties.plan.default, 'apply');
  assert.ok(plan.proposed.some(entry => entry.choice.replacement === '[USER_HOME]/project/.github/workflows/e2e.yml'));
  assert.ok(plan.proposed.some(entry => entry.choice.replacement === '"gitBranch": "[BRANCH_1]"'));
  assert.ok(plan.proposed.some(entry => entry.choice.replacement === 'gitBranch": "[BRANCH_1]'));
  assert.doesNotMatch(plan.message, /masked already|PRIVATE_ID|field_sha256|[a-f0-9]{64}/);
  assert.doesNotMatch(plan.message, /offsets|Finding \d+:|Source items \(/);
  assert.ok(renderedText(plan.message).trim().split(/\s+/).length <= 350);
  assert.match(renderedText(plan.message), /Home paths: replace the user-home prefix/);
  assert.match(renderedText(plan.message), /Example: \/Users\/example\/project\/\.github\/workflows\/e2e.yml → \[USER_HOME\]\/project\/\.github\/workflows\/e2e.yml/);
  assert.match(plan.message, /50 findings: 50 matched occurrences across 50 source items, in this saved draft only/);
  assert.match(plan.message, /Keep necessary or uncertain technical context \(not certified safe\): 3 findings/);
  const positive = buildReviewPlan({ findings, hard_removals: { secret_pattern_matches: 2, encoded_secret_fields: 1 } });
  assert.match(positive.message, /2 secret-pattern match\(es\) and 1 encoded-secret field\(s\) masked already/);
  assert.match(positive.message, /No guarantee all secrets were found/);
  const invalid = buildReviewPlan({ findings, hard_removals: { secret_pattern_matches: true, encoded_secret_fields: -1 } });
  assert.doesNotMatch(invalid.message, /masked already/);
  const captured = buildReviewPlan({ findings, capture_rule_matches: { secret_fields: 1, secret_pattern_matches: 3 }, hard_removals: { secret_pattern_matches: 0, encoded_secret_fields: 0 } });
  assert.match(captured.message, /Recognized secret patterns were masked before review/);
  assert.match(captured.message, /1 secret field\(s\) and 3 pattern match\(es\)/);
  assert.match(captured.message, /Counts may overlap and are not unique credentials/);
  for (const capture_rule_matches of [undefined, { secret_fields: 0, secret_pattern_matches: 0 }, { secret_fields: true, secret_pattern_matches: 3 }, { secret_fields: -1, secret_pattern_matches: 3 }]) {
    assert.doesNotMatch(buildReviewPlan({ findings, capture_rule_matches }).message, /masked before review/);
  }
});

test('bulk defaults preserve uncertain technical context and exclude unbound findings', () => {
  const variants = [
    { category: 'confidential', categories: ['confidential'], recommended: 'keep', necessity: 'necessary' },
    { category: 'reputation', categories: ['reputation'], recommended: 'remove' },
    { recommended: 'remove' }, { recommended: 'keep' }, { allowed_actions: ['keep', 'remove'] },
    { assessment_summary: { status: 'disagreement', necessities: ['necessary', 'unnecessary'] } },
    { detectors: ['copilot'], necessity: 'uncertain' }, { text: 'go test ./...' },
    { text: 'export KEY=/Users/example; run command' }, { scope: undefined },
  ];
  const findings = variants.map((changes, index) => scopedFinding(index, changes));
  findings.push(identifierFinding(30, 'a plausible person name'), identifierFinding(31, 'unknown/branch'));
  const plan = buildReviewPlan({ findings });
  assert.equal(plan.proposed.length, findings.length - 1);
  assert.equal(plan.manual.reduce((count, group) => count + group.members.length, 0), 1);
  for (const entry of plan.proposed) assert.equal(entry.choice.action,
    entry.group.finding.category === 'reputation' ? 'remove' : entry.group.finding.category === 'identifier' ? 'pseudonymize' : 'keep');
});

test('a necessary sentence containing a home identifier proposes one compatible scoped rewrite', () => {
  for (const prefix of ['Reported workspace: ', '🔧 Reported workspace: ']) {
    const text = prefix + '/Users/example/project; PATH remains unverified.';
    const path = [0, 'data', 'agent'];
    const scope = { schema: 'baseline-field/v2', field_sha256: 'a'.repeat(64) };
    const parent = scopedFinding(0, { text, scope, necessity: 'necessary', detectors: ['copilot'],
      occurrences: [{ path, start: 10, end: 10 + Array.from(text).length }] });
    const start = 10 + Array.from(prefix).length;
    const child = scopedFinding(1, { scope, occurrences: [{ path, start, end: start + 14 }] });
    const findings = [parent, child];
    const before = structuredClone(findings);
    const plan = buildReviewPlan({ findings });
    const proposal = plan.proposed.find(entry => entry.group.finding.id === parent.id);
    assert.deepEqual(proposal.choice, { action: 'generalize', replacement: prefix + '[USER_HOME]/project; PATH remains unverified.' });
    assert.match(plan.message, /Overlapping context: preserve the surrounding technical text/);
    assert.equal(plan.available, true);
    assert.deepEqual(findings, before);
  }
});

test('context rewrite never expands to another field or invents a resolution for partial spans', () => {
  const text = 'Workspace: /Users/example/project';
  const scope = { schema: 'baseline-field/v2', field_sha256: 'a'.repeat(64) };
  const parent = scopedFinding(0, { text, scope, necessity: 'necessary', detectors: ['copilot'],
    occurrences: [{ path: [0, 'data', 'agent'], start: 0, end: text.length }] });
  for (const occurrence of [
    { path: [0, 'data', 'evidence'], start: 11, end: 25 },
    { path: [0, 'data', 'agent'], start: 30, end: 44 },
  ]) {
    const child = scopedFinding(1, { scope, occurrences: [occurrence] });
    const plan = buildReviewPlan({ findings: [parent, child] });
    assert.equal(plan.proposed.find(entry => entry.group.finding.id === parent.id).choice.action, 'keep');
  }
});

test('one accepted plan produces exactly fifty scoped decisions with technical context retained', async () => {
  const findings = fiftyFindings();
  const frozen = structuredClone(findings);
  const plan = buildReviewPlan({ findings });
  const { workflow, calls, dialogs, logs } = fixture({ canvas: false, findings, answers: [
    { action: 'accept', content: { readers: 'both', delivery: 'local', privacyMode: 'llm' } },
    { action: 'accept', content: { plan: 'apply' } },
    true,
  ] });
  const result = await workflow.tool.handler({}, { sessionId });
  const generate = calls.find(call => call[0] === 'generate')[1];
  assert.deepEqual(Object.keys(generate.choices).sort(), findings.map(finding => finding.id).sort());
  for (const entry of plan.proposed) for (const member of entry.group.members) assert.deepEqual(generate.choices[member.finding.id], entry.choice);
  assert.deepEqual(generate.choices[findings[47].id], { action: 'keep' });
  assert.deepEqual(generate.choices[findings[48].id], { action: 'keep' });
  assert.deepEqual(generate.choices[findings[49].id], { action: 'keep' });
  assert.equal(generate.review_id, 'review');
  assert.equal(generate.confirmed, true);
  const forms = dialogs.filter(dialog => typeof dialog === 'object');
  assert.deepEqual(forms.map(dialog => dialog.requestedSchema.required.length), [3, 1]);
  assert.equal(forms[1].requestedSchema.properties.plan.default, 'apply');
  assert.deepEqual(findings, frozen);
  assert.doesNotMatch(JSON.stringify(result) + JSON.stringify(logs), /PRIVATE_ID|API_KEY|test-branch|Users\/example/);
});

test('plan cancel, invalid selector, abort and declined generation cannot imply consent', async () => {
  for (const answer of [{ action: 'cancel' }, { action: 'accept', content: { plan: 'keep-all' } }, { action: 'accept', content: { plan: 'apply', extra: 'keep' } }]) {
    const context = fixture({ findings: [scopedFinding(0)], answers: [
      { action: 'accept', content: { readers: 'both', delivery: 'local', privacyMode: 'llm' } }, answer,
    ] });
    if (answer.action === 'cancel') assert.equal((await context.workflow.run({ sessionId })).cancelled, true);
    else await assert.rejects(context.workflow.run({ sessionId }), /Invalid privacy plan/);
    assert.equal(context.calls.some(call => call[0] === 'generate'), false);
  }
  const declined = fixture({ findings: [scopedFinding(0)], answers: [
    { action: 'accept', content: { readers: 'both', delivery: 'local', privacyMode: 'llm' } }, { action: 'accept', content: { plan: 'apply' } }, false,
  ] });
  assert.equal((await declined.workflow.run({ sessionId })).cancelled, true);
  assert.equal(declined.calls.some(call => call[0] === 'generate'), false);
  const aborted = fixture({ findings: [scopedFinding(0)] });
  const controller = new AbortController();
  aborted.session.ui.elicitation = async request => {
    if (!request.requestedSchema.properties.plan) return { action: 'accept', content: { readers: 'both', delivery: 'local', privacyMode: 'llm' } };
    controller.abort();
    return { action: 'accept', content: { plan: 'apply' } };
  };
  await assert.rejects(aborted.workflow.run({ sessionId, signal: controller.signal }), /cancelled/);
  assert.equal(aborted.calls.some(call => call[0] === 'generate'), false);
});

test('customize by group then review individually permits distinct actions without plan approvals', async () => {
  const findings = Array.from({ length: 6 }, (unused, index) => scopedFinding(index));
  const { workflow, calls, dialogs } = fixture({ findings, canvas: false, answers: [
    { action: 'accept', content: { readers: 'both', delivery: 'local', privacyMode: 'llm' } },
    { action: 'accept', content: { plan: 'customize' } }, { action: 'accept', content: { finding_1: 'individually' } },
    { action: 'accept', content: { finding_1: 'keep', finding_2: 'remove', finding_3: 'pseudonymize', finding_4: 'generalize', finding_5: 'keep' } }, 'a workspace',
    { action: 'accept', content: { finding_6: 'remove' } }, true,
  ] });
  await workflow.run({ sessionId });
  const choices = calls.find(call => call[0] === 'generate')[1].choices;
  assert.deepEqual(choices, Object.fromEntries(findings.map((finding, index) => [finding.id, index === 3 ? { action: 'generalize', replacement: 'a workspace' } : { action: ['keep', 'remove', 'pseudonymize', '', 'keep', 'remove'][index] }])));
  const individualForms = dialogs.filter(dialog => typeof dialog === 'object').slice(3);
  assert.deepEqual(individualForms.map(dialog => dialog.requestedSchema.required.length), [5, 1]);
  assert.equal(individualForms.every(dialog => Object.values(dialog.requestedSchema.properties).every(property => !property.enum.includes('individually'))), true);
  assert.equal(individualForms.every(dialog => /Source offsets are zero-based, end-exclusive/.test(dialog.message)), true);
  assert.doesNotMatch(dialogs[1].message, /offsets|Finding \d+:/);
  assert.doesNotMatch(dialogs[2].message, /offsets|Finding \d+:/);
});

test('group generalization uses one explicit replacement for exact members and cancellation never dispatches', async () => {
  for (const cancelled of [false, true]) {
    const findings = [scopedFinding(0), scopedFinding(1)];
    const context = fixture({ findings, canvas: false, answers: [
      { action: 'accept', content: { readers: 'both', delivery: 'local', privacyMode: 'llm' } },
      { action: 'accept', content: { plan: 'customize' } }, { action: 'accept', content: { finding_1: 'generalize' } },
      cancelled ? undefined : 'chosen home', true,
    ] });
    const result = await context.workflow.run({ sessionId });
    if (cancelled) {
      assert.equal(result.cancelled, true);
      assert.equal(context.calls.some(call => call[0] === 'generate'), false);
    } else {
      assert.deepEqual(context.calls.find(call => call[0] === 'generate')[1].choices, Object.fromEntries(findings.map(finding => [finding.id, { action: 'generalize', replacement: 'chosen home' }])));
    }
  }
});

test('cancellation in individual fallback and unknown group actions preserve the saved review', async () => {
  for (const choice of ['individually', 'approve-all']) {
    const context = fixture({ findings: [scopedFinding(0), scopedFinding(1)], answers: [
      { action: 'accept', content: { readers: 'both', delivery: 'local', privacyMode: 'llm' } }, { action: 'accept', content: { plan: 'customize' } },
      { action: 'accept', content: { finding_1: choice } }, { action: 'cancel' },
    ] });
    if (choice === 'individually') assert.equal((await context.workflow.run({ sessionId })).cancelled, true);
    else await assert.rejects(context.workflow.run({ sessionId }), /Invalid or incomplete/);
    assert.equal(context.calls.filter(call => call[0] === 'scan').length, 1);
    assert.equal(context.calls.some(call => call[0] === 'generate'), false);
  }
});

test('frozen group membership resists external mutation while the plan dialog is open', async () => {
  const findings = [scopedFinding(0)];
  const context = fixture({ findings, canvas: false, answers: [true] });
  context.session.ui.elicitation = async request => {
    context.dialogs.push(request);
    if (!request.requestedSchema.properties.plan) return { action: 'accept', content: { readers: 'both', delivery: 'local', privacyMode: 'llm' } };
    findings[0].id = 'OTHER';
    findings[0].text = '/Users/another';
    findings.push(scopedFinding(1));
    return { action: 'accept', content: { plan: 'apply' } };
  };
  await context.workflow.run({ sessionId });
  assert.deepEqual(context.calls.find(call => call[0] === 'generate')[1].choices, { PRIVATE_ID_0: { action: 'generalize', replacement: '[USER_HOME]' } });
});

test('overlap conflict rejection from the backend is preserved without retry or weakening choices', async () => {
  const context = fixture({ findings: [scopedFinding(0), identifierFinding(1, '/Users/example/project')], answers: [
    { action: 'accept', content: { readers: 'both', delivery: 'local', privacyMode: 'llm' } }, { action: 'accept', content: { plan: 'apply' } }, true,
  ] });
  const original = context.bridge.call;
  context.bridge.call = async (operation, data) => {
    if (operation === 'generate') {
      context.calls.push([operation, data]);
      throw new Error('Overlapping approved scopes conflict');
    }
    return original(operation, data);
  };
  await assert.rejects(context.workflow.run({ sessionId }), /scopes conflict/);
  assert.equal(context.calls.filter(call => call[0] === 'generate').length, 1);
  assert.equal(context.calls.some(call => ['deliverables', 'publish'].includes(call[0])), false);
});

test('large membership stays compact without dropping bound proposals or customization', () => {
  const findings = Array.from({ length: 120 }, (unused, index) => identifierFinding(index, `/Users/example/project-${index}/file.txt`));
  const plan = buildReviewPlan({ findings });
  assert.equal(plan.available, true);
  assert.equal(plan.proposed.length, findings.length);
  assert.ok(renderedText(plan.message).trim().split(/\s+/).length <= 350);
  assert.doesNotMatch(plan.message, /offsets|Finding \d+:/);
  assert.equal((plan.message.match(/Example:/g) || []).length, 1);
  const batches = buildReviewBatches(findings);
  assert.equal(batches.flatMap(batch => batch.entries).length, findings.length);
  assert.equal(batches.every(batch => batch.entries.length <= 5), true);
  for (const batch of batches) assert.ok(Buffer.byteLength(JSON.stringify({ message: batch.message, requestedSchema: { type: 'object', properties: batch.properties, required: Object.keys(batch.properties) } }), 'utf8') <= 24000);
});

test('plan word bound includes both masking notices and does not truncate long exact examples', () => {
  const findings = fiftyFindings();
  const plan = buildReviewPlan({ findings, capture_rule_matches: { secret_fields: 12, secret_pattern_matches: 50 }, hard_removals: { secret_pattern_matches: 2, encoded_secret_fields: 3 } });
  assert.equal(plan.available, true);
  assert.ok(renderedText(plan.message).trim().split(/\s+/).length <= 350);
  const longFinding = identifierFinding(0, '/Users/example/' + 'folder/'.repeat(40) + 'file.txt');
  const longPlan = buildReviewPlan({ findings: [longFinding] });
  assert.equal(longPlan.available, false);
  const batch = buildReviewBatches([longFinding])[0];
  assert.ok(renderedText(batch.message).includes(longFinding.text));
});

test('customization uses compact source item ranges and no more than two context excerpts', () => {
  const findings = [0, 1, 2, 6, 7, 20].map(index => scopedFinding(index, { contexts: [`source ${index}`], related_contexts: [{ text: 'extra related text' }] }));
  const batch = buildReviewBatches(findings)[0];
  assert.match(batch.message, /Source items \(6\): 1–3, 7–8, 21/);
  assert.equal((batch.message.match(/context excerpt:/g) || []).length, 2);
  assert.doesNotMatch(batch.message, /offsets|Finding \d+:|extra related text/);
  assert.equal(batch.entries[0].members.length, findings.length);
});

test('App opens read-only progress after capture and uses one panel through completion', async () => {
  const { workflow, calls, session } = fixture({ answers: [{ action: 'accept', content: { readers: 'both', delivery: 'local', privacyMode: 'llm' } }, true] });
  session.rpc.canvas.open = async value => {
    calls.push(['canvas', value]);
    const progress = await workflow.canvas.open({ sessionId });
    assert.match(progress.status, /Live progress.*read-only/);
    assert.equal(progress.url.includes('READONLY'), true);
  };
  await assert.rejects(workflow.canvas.open({ sessionId }), /No completed export/);
  const result = await workflow.tool.handler({}, { sessionId });
  assert.equal(JSON.parse(result.textResultForLlm).job, 'a'.repeat(32));
  assert.equal(result.textResultForLlm.includes('access='), false);
  assert.deepEqual(calls.map(call => call[0]), ['pending_review', 'getEvents', 'capture', 'scan', 'canvas', 'progress_canvas', 'status', 'review', 'generate', 'status', 'deliverables']);
  assert.equal((await workflow.canvas.open({ sessionId })).url.includes('READONLY'), true);
});

test('failed progress-panel opening keeps the same job and retries only its completed view', async () => {
  const context = fixture({ answers: [
    { action: 'accept', content: { readers: 'both', delivery: 'local', privacyMode: 'llm' } }, true,
  ] });
  let opens = 0;
  context.session.rpc.canvas.open = async value => {
    context.calls.push(['canvas', value]);
    if (++opens === 1) throw new Error('PRIVATE_HOST_FAILURE');
  };
  const result = await context.workflow.run({ sessionId });
  assert.equal(result.status, 'done');
  for (const operation of ['capture', 'scan', 'generate']) {
    assert.equal(context.calls.filter(call => call[0] === operation).length, 1);
  }
  assert.equal(opens, 2);
  assert.match(context.logs.join('\n'), /still running.*rather than starting another/);
  assert.doesNotMatch(context.logs.join('\n'), /PRIVATE_HOST_FAILURE/);
});

for (const privacyMode of ['full', 'llm']) {
  for (const readers of ['human', 'agent', 'both']) {
    for (const resume of [false, true]) {
      test(`${resume ? 'saved' : 'fresh'} ${privacyMode}/${readers} completion exposes only selected file metadata and preserves trusted canvas access`, async () => {
        const sessionIdentifier = randomUUID();
        const names = { human: ['human-spec.html'], agent: ['agent-spec.md', 'evidence.md'], both: ['human-spec.html', 'agent-spec.md', 'evidence.md'] }[readers];
        const windows = 'C:\\Users\\WINDOWS_EXPORT_PRIVATE\\internal-work\\';
        const posix = '/home/POSIX_EXPORT_PRIVATE/internal-work/';
        const canvasUrl = 'http://127.0.0.1:1234/#access=READONLY_CAPABILITY_PRIVATE';
        const files = names.map((name, index) => ({ name, path: (index % 2 ? posix : windows) + name, bytes: index, sha256: String(index + 1).repeat(64),
          source_path: posix + 'source.json', access: 'APPROVAL_TOKEN_PRIVATE', detail: { raw: 'RAW_REVIEW_PRIVATE' } }));
        const delivery = { files, bundle: { name: windows + 'OVERRIDE_PRIVATE.zip', path: posix + 'deliverables.zip', sha256: 'f'.repeat(64), url: canvasUrl },
          source: windows + 'source.json', review_id: 'RAW_REVIEW_PRIVATE', preferences: { unrelated: windows } };
        const smart = privacyMode === 'llm';
        const pending = resume ? { ...savedReview(privacyMode, smart ? 1 : 0), readers } : null;
        const context = fixture({ sessionIdentifier, pending, delivery, canvasUrl,
          findings: smart ? [{ id: 'SCOPED_ID_PRIVATE', text: 'SOURCE_QUOTE_PRIVATE', category: 'reputation', contexts: ['SOURCE_CONTEXT_PRIVATE'] }] : [],
          answers: [resume ? { action: 'accept', content: { continuation: 'resume' } } : { action: 'accept', content: { readers, delivery: 'local', privacyMode } },
            ...(smart ? [{ action: 'accept', content: { finding_1: 'remove' } }] : []), true],
        });
        const original = structuredClone(delivery);
        const result = await context.workflow.tool.handler({}, { sessionId: sessionIdentifier });
        const metadata = JSON.parse(result.textResultForLlm);
        assert.deepEqual(metadata, { status: 'done', job: 'a'.repeat(32), files: files.map(({ name, bytes, sha256 }) => ({ name, bytes, sha256 })), bundle: { name: 'deliverables.zip', sha256: 'f'.repeat(64) } });
        assert.equal(result.resultType, 'success');
        assert.deepEqual(delivery, original);
        const exposed = result.textResultForLlm + JSON.stringify(context.logs);
        for (const canary of ['WINDOWS_EXPORT_PRIVATE', 'POSIX_EXPORT_PRIVATE', 'READONLY_CAPABILITY_PRIVATE', 'APPROVAL_TOKEN_PRIVATE', 'RAW_REVIEW_PRIVATE', 'OVERRIDE_PRIVATE', 'SCOPED_ID_PRIVATE', 'SOURCE_QUOTE_PRIVATE', 'SOURCE_CONTEXT_PRIVATE']) assert.equal(exposed.includes(canary), false, canary);
        assert.doesNotMatch(result.textResultForLlm, /source_path|review_id|preferences|"path"|"url"|"access"/);
        for (const name of names) assert.ok(context.logs.some(log => log.includes(name)));
        if (readers === 'human') assert.equal(exposed.includes('agent-spec.md'), false);
        if (readers === 'agent') assert.equal(exposed.includes('human-spec.html'), false);
        assert.equal(context.calls.some(call => ['capture', 'scan'].includes(call[0])), !resume);
        assert.equal((await context.workflow.canvas.open({ sessionId: sessionIdentifier })).url, canvasUrl);
        assert.deepEqual(context.calls.at(-1), ['output_canvas', { job: 'a'.repeat(32) }]);
        context.session.openCanvases.push(context.calls.find(call => call[0] === 'canvas')[1]);
        context.session.ui.select = async () => 'Open saved files';
        const savedResult = await context.workflow.tool.handler({}, { sessionId: sessionIdentifier });
        assert.deepEqual(JSON.parse(savedResult.textResultForLlm), { status: 'opened_saved_files', job: 'a'.repeat(32) });
        assert.equal(context.calls.filter(call => call[0] === 'generate').length, 1);
        assert.doesNotMatch(savedResult.textResultForLlm + JSON.stringify(context.logs), /_PRIVATE|C:\\\\Users|\/home\//);
      });
    }
  }
}

test('optional file sizes and hashes are typed metadata, never arbitrary strings or fabricated zeroes', async () => {
  for (const invalid of [undefined, null, true, -1, 1.5, Number.MAX_SAFE_INTEGER + 1, 'C:\\Users\\META_PRIVATE\\file', { path: '/home/META_PRIVATE/file' }]) {
    const sessionIdentifier = randomUUID();
    const context = fixture({ sessionIdentifier, canvas: false,
      delivery: { files: [{ name: 'human-spec.html', bytes: invalid, sha256: invalid }], bundle: { bytes: invalid, sha256: invalid, path: '/home/META_PRIVATE/bundle' } },
      answers: [{ action: 'accept', content: { readers: 'human', delivery: 'local', privacyMode: 'full' } }, true],
    });
    const result = await context.workflow.tool.handler({}, { sessionId: sessionIdentifier });
    assert.deepEqual(JSON.parse(result.textResultForLlm), { status: 'done', job: 'a'.repeat(32), files: [{ name: 'human-spec.html' }], bundle: { name: 'deliverables.zip' } });
    assert.doesNotMatch(result.textResultForLlm + JSON.stringify(context.logs), /META_PRIVATE/);
  }
});

test('unknown, duplicate, missing and unselected deliverable names fail without reflecting metadata', async () => {
  for (const files of [[], [{ name: 'C:\\Users\\FILENAME_PRIVATE\\human-spec.html' }], [{ name: '/home/FILENAME_PRIVATE/human-spec.html' }],
    [{ name: 'agent-spec.md' }], [{ name: 'human-spec.html' }, { name: 'human-spec.html' }],
    [{ name: 'human-spec.html' }, { name: 'review.json' }]]) {
    const sessionIdentifier = randomUUID();
    const context = fixture({ sessionIdentifier, delivery: { files, bundle: { path: '/home/FILENAME_PRIVATE/bundle' } },
      answers: [{ action: 'accept', content: { readers: 'human', delivery: 'local', privacyMode: 'full' } }, true],
    });
    await assert.rejects(context.workflow.tool.handler({}, { sessionId: sessionIdentifier }), error => error.message === 'Invalid selected deliverable metadata. No local paths were exposed.');
    assert.doesNotMatch(JSON.stringify(context.logs), /FILENAME_PRIVATE|review\.json/);
    assert.equal(context.calls.filter(call => call[0] === 'canvas').length, 1);
    assert.equal(context.calls.some(call => call[0] === 'output_canvas'), false);
  }
});

test('native command completion logs names, not Windows or POSIX output locations', async () => {
  const sessionIdentifier = randomUUID();
  const context = fixture({ sessionIdentifier, canvas: false,
    delivery: { files: [{ name: 'human-spec.html', path: 'C:\\Users\\COMMAND_PRIVATE\\human-spec.html' }], bundle: { path: '/home/COMMAND_PRIVATE/deliverables.zip' } },
    answers: [{ action: 'accept', content: { readers: 'human', delivery: 'local', privacyMode: 'full' } }, true],
  });
  await context.workflow.command.handler({ sessionId: sessionIdentifier });
  assert.ok(context.logs.includes('Your spec is ready.\nhuman-spec.html\nZIP: deliverables.zip'));
  assert.doesNotMatch(JSON.stringify(context.logs), /COMMAND_PRIVATE|C:\\\\Users|\/home\//);
});

test('wrong session and explicit source arguments fail before reads', async () => {
  const { workflow, calls } = fixture();
  await assert.rejects(workflow.run({ sessionId: 'other' }), /identity/);
  await assert.rejects(workflow.tool.handler({ path: 'private' }, { sessionId }), /no arguments/);
  assert.deepEqual(calls, []);
});

test('unsupported native UI does not silently open a browser', async () => {
  const { workflow, calls } = fixture({ canvas: false, elicitation: false });
  await assert.rejects(workflow.run({ sessionId }), /does not support native/);
  assert.deepEqual(calls, []);
});

test('canvas without native forms cannot approve any operation', async () => {
  const { workflow, calls } = fixture({ canvas: true, elicitation: false });
  await assert.rejects(workflow.run({ sessionId }), /native privacy forms/);
  assert.deepEqual(calls, []);
});

test('cancelled setup reads no transcript and starts no model', async () => {
  const { workflow, calls, session } = fixture({ canvas: false, answers: [{ action: 'cancel' }] });
  let modelReads = 0;
  session.rpc.model = { getCurrent: async () => { modelReads += 1; return { modelId: 'synthetic-model' }; } };
  assert.deepEqual(await workflow.run({ sessionId }), { cancelled: true });
  assert.equal(modelReads, 0);
  assert.deepEqual(calls, [['pending_review', {}]]);
});

test('new exports read the active session model once after consent without changing it', async () => {
  const models = ['synthetic-selected-model', 'auto'];
  const context = fixture({ canvas: false, answers: models.flatMap(() => [
    { action: 'accept', content: { readers: 'both', delivery: 'local', privacyMode: 'llm' } }, false,
  ]) });
  context.session.rpc.model = {
    getCurrent: async () => {
      context.calls.push(['getCurrentModel']);
      assert.ok(context.dialogs.length > 0);
      return { modelId: models[context.calls.filter(call => call[0] === 'getCurrentModel').length - 1], reasoningEffort: 'high' };
    },
    switchTo: async () => assert.fail('The plugin must not change the host model'),
  };
  for (const model of models) {
    assert.equal((await context.workflow.run({ sessionId })).cancelled, true);
    assert.equal(context.calls.filter(call => call[0] === 'scan').at(-1)[1].host_model, model);
    assert.equal(Object.hasOwn(context.calls.filter(call => call[0] === 'scan').at(-1)[1], 'reasoning_effort'), false);
  }
  assert.equal(context.calls.filter(call => call[0] === 'getCurrentModel').length, 2);
  assert.ok(context.calls.findIndex(call => call[0] === 'getCurrentModel') < context.calls.findIndex(call => call[0] === 'getEvents'));
});

test('unavailable or invalid host model lookup leaves CLI selection unset without leaking SDK errors', async () => {
  for (const response of [null, {}, { modelId: '--untrusted argument' }, new Error('PRIVATE_HOST_ERROR')]) {
    const context = fixture({ canvas: false, answers: [
      { action: 'accept', content: { readers: 'both', delivery: 'local', privacyMode: 'llm' } }, false,
    ] });
    context.session.rpc.model = { getCurrent: async () => { if (response instanceof Error) throw response; return response; } };
    assert.equal((await context.workflow.run({ sessionId })).cancelled, true);
    assert.equal(Object.hasOwn(context.calls.find(call => call[0] === 'scan')[1], 'host_model'), false);
    assert.doesNotMatch(JSON.stringify([context.calls, context.logs, context.dialogs]), /PRIVATE_HOST_ERROR|untrusted argument/);
  }
});

test('cancellation during model lookup blocks capture and scan', async () => {
  const context = fixture({ canvas: false, answers: [
    { action: 'accept', content: { readers: 'both', delivery: 'local', privacyMode: 'llm' } },
  ] });
  const controller = new AbortController();
  context.session.rpc.model = { getCurrent: async () => { controller.abort(); return { modelId: 'synthetic-model' }; } };
  await assert.rejects(context.workflow.run({ sessionId, signal: controller.signal }), /Export cancelled/);
  assert.equal(context.calls.some(call => ['getEvents', 'capture', 'scan'].includes(call[0])), false);
});

test('native setup defaults to both local Smart but still requires explicit acceptance', async () => {
  const { workflow, calls, dialogs } = fixture({ answers: [{ action: 'cancel' }] });
  await workflow.run({ sessionId });
  const setup = dialogs[0];
  assert.deepEqual(setup.requestedSchema.required, ['readers', 'delivery', 'privacyMode']);
  assert.deepEqual(Object.keys(setup.requestedSchema.properties), ['readers', 'delivery', 'privacyMode']);
  assert.deepEqual(setup.requestedSchema.properties.readers.enum, ['both', 'human', 'agent']);
  assert.deepEqual(setup.requestedSchema.properties.delivery.enum, ['local', 'artifactstore']);
  assert.deepEqual(setup.requestedSchema.properties.privacyMode.enum, ['full', 'llm']);
  assert.deepEqual(setup.requestedSchema.properties.privacyMode.enumNames, ['No redaction', 'Smart redaction (rules + Copilot)']);
  assert.equal(Object.hasOwn(setup.requestedSchema.properties, 'detection'), false);
  assert.deepEqual(Object.fromEntries(Object.entries(setup.requestedSchema.properties).map(([key, property]) => [key, property.default])), { readers: 'both', delivery: 'local', privacyMode: 'llm' });
  for (const property of Object.values(setup.requestedSchema.properties)) assert.ok(property.enum.includes(property.default));
  assert.match(setup.message, /personal details, internal information or secrets/);
  assert.match(setup.message, /Export this conversation as a spec/);
  assert.match(setup.message, /rules and Copilot on the draft, masking recognized secrets/);
  assert.match(setup.message, /uncertain private details or asides/);
  assert.match(setup.message, /can miss sensitive content/);
  assert.match(setup.message, /Both modes use your quota/);
  assert.match(setup.message, /Accept sends the original observable conversation to Copilot to draft privately BEFORE redaction/);
  assert.match(setup.message, /separate confirmation/);
  assert.match(setup.message, /Uploading requires final approval/);
  assert.ok(setup.message.trim().split(/\s+/).length <= 100);
  assert.doesNotMatch(setup.message, /pre.masked|on.device|local rules only/i);
  assert.deepEqual(calls, [['pending_review', {}]]);
});

for (const privacyMode of ['full', 'llm']) {
  test(`${privacyMode} preparation binds accepted drafting consent but cannot imply final export consent`, async () => {
    const { workflow, calls, dialogs } = fixture({ canvas: false,
      answers: [{ action: 'accept', content: { readers: 'both', delivery: 'local', privacyMode } }, false],
    });
    assert.equal((await workflow.run({ sessionId })).cancelled, true);
    assert.deepEqual(calls.find(call => call[0] === 'capture')[1], { events: source, privacy_mode: privacyMode, capture_order: pipeline });
    assert.deepEqual(calls.find(call => call[0] === 'scan')[1], {
      session: 'current', readers: 'both', delivery: 'local', privacy_mode: privacyMode,
      detection: privacyMode === 'llm' ? 'copilot' : 'none', audience: 'local', semantic: privacyMode === 'llm', pipeline,
    });
    assert.deepEqual(calls.map(call => call[0]), ['pending_review', 'getEvents', 'capture', 'scan', 'status', 'review']);
    assert.match(dialogs[1], /Stage 3 of 3/);
    assert.match(dialogs[1], /No ArtifactStore upload happens yet/);
    assert.match(dialogs[1], /No more model calls occur after approval/);
  });

  test(`${privacyMode} mode requires a separate deterministic render-export confirmation`, async () => {
    const { workflow, calls, session } = fixture({ canvas: false,
      answers: [{ action: 'accept', content: { readers: 'both', delivery: 'local', privacyMode } }],
    });
    let confirmations = 0;
    session.ui.confirm = async message => {
      confirmations += 1;
      assert.match(message, /No more model calls occur after approval/);
      assert.doesNotMatch(message, /sent to Copilot|using your quota/);
      assert.equal(calls.some(call => call[0] === 'generate'), false);
      return true;
    };
    await workflow.run({ sessionId });
    assert.equal(confirmations, 1);
    assert.deepEqual(calls.filter(call => call[0] === 'generate'), [['generate', {
      job: 'a'.repeat(32), review_id: 'review', choices: {}, confirmed: true, pipeline, abstraction_sha256: abstractionSha,
    }]]);
    assert.equal(calls.some(call => call[0] === 'publish'), false);
  });
}

test('rule-only values, display labels and missing privacy choices fail before source reads', async () => {
  for (const privacyMode of ['local', 'copilot', 'No redaction', 'Smart redaction (rules + Copilot)', undefined]) {
    const content = { readers: 'both', delivery: 'local' };
    if (privacyMode !== undefined) content.privacyMode = privacyMode;
    const { workflow, calls } = fixture({ answers: [{ action: 'accept', content }] });
    await assert.rejects(workflow.run({ sessionId }), /Invalid export choice|A required choice was not submitted/);
    assert.deepEqual(calls, [['pending_review', {}]]);
  }
});

test('CLI native forms keep private findings out of the tool result', async () => {
  const { workflow, calls, dialogs } = fixture({ canvas: false,
    answers: [{ action: 'accept', content: { readers: 'agent', delivery: 'local', privacyMode: 'llm' } }, { action: 'accept', content: { finding_1: 'remove' } }, true],
    findings: [{ id: 'F1', category: 'reputation', text: 'PRIVATE_ASIDE', contexts: ['PRIVATE_CONTEXT'] }],
  });
  const result = await workflow.tool.handler({}, { sessionId });
  assert.equal(JSON.stringify(result).includes('PRIVATE_ASIDE'), false);
  assert.equal(dialogs.some(dialog => typeof dialog === 'object' && renderedText(dialog.message).includes('PRIVATE_ASIDE')), true);
  const generate = calls.find(call => call[0] === 'generate')[1];
  assert.deepEqual(generate.choices, { F1: { action: 'remove' } });
  assert.equal(calls.some(call => call[0] === 'publish'), false);
});

test('a single accepted group expands only to its fifty bound finding IDs', async () => {
  const findings = Array.from({ length: 50 }, (unused, index) => ({
    id: `F${index + 1}`, text: 'PRIVATE_SAME_TEXT', contexts: ['PRIVATE_CONTEXT'],
    scope: { schema: 'baseline-field/v2', field_sha256: String(index).padStart(64, '0') },
    occurrences: [{ path: [index, 'data', 'content'], start: 0, end: 17 }],
  }));
  const { workflow, calls, dialogs, logs } = fixture({ canvas: false, findings,
    answers: [{ action: 'accept', content: { readers: 'both', delivery: 'local', privacyMode: 'llm' } }, { action: 'accept', content: { plan: 'customize' } }, { action: 'accept', content: { finding_1: 'remove' } }, true],
  });
  const result = await workflow.tool.handler({}, { sessionId });
  const forms = dialogs.filter(dialog => typeof dialog === 'object');
  assert.equal(forms.length, 3);
  assert.equal(forms[2].requestedSchema.required.length, 1);
  const generate = calls.find(call => call[0] === 'generate')[1];
  assert.equal(Object.keys(generate.choices).length, 50);
  for (const finding of findings) assert.deepEqual(generate.choices[finding.id], { action: 'remove' });
  assert.match(logs.join('\n'), /Stage 1 of 3/);
  assert.match(logs.join('\n'), /50 findings in 1 groups/);
  assert.match(logs.join('\n'), /50 of 50 choices accepted/);
  assert.match(logs.join('\n'), /Stage 3 of 3/);
  assert.equal(JSON.stringify(result).includes('PRIVATE_'), false);
  assert.equal(JSON.stringify(logs).includes('PRIVATE_'), false);
});

test('batch cancellation or missing, extra and invalid actions cannot generate or infer keep-all', async () => {
  for (const answer of [
    { action: 'cancel' },
    { action: 'accept', content: { finding_1: 'keep' } },
    { action: 'accept', content: { finding_1: 'keep', finding_2: 'keep', all: 'keep' } },
    { action: 'accept', content: { finding_1: 'keep', finding_2: 'recommended' } },
  ]) {
    const { workflow, calls } = fixture({ canvas: false,
      findings: [{ id: 'F1', text: 'same' }, { id: 'F2', text: 'same' }],
      answers: [{ action: 'accept', content: { readers: 'both', delivery: 'local', privacyMode: 'llm' } }, answer],
    });
    if (answer.action === 'cancel') assert.equal((await workflow.run({ sessionId })).cancelled, true);
    else await assert.rejects(workflow.run({ sessionId }), /required choice|Invalid or incomplete/);
    assert.equal(calls.some(call => ['generate', 'package', 'plan', 'publish'].includes(call[0])), false);
  }
});

test('cancelling a later batch preserves the scan and never treats remaining scopes as accepted', async () => {
  const findings = Array.from({ length: 6 }, (unused, index) => ({ id: `F${index + 1}`, text: 'same phrase' }));
  const { workflow, calls, logs } = fixture({ canvas: false, findings,
    answers: [{ action: 'accept', content: { readers: 'both', delivery: 'local', privacyMode: 'llm' } },
      { action: 'accept', content: Object.fromEntries(findings.slice(0, 5).map((finding, index) => [`finding_${index + 1}`, 'keep'])) },
      { action: 'cancel' }],
  });
  assert.deepEqual(await workflow.run({ sessionId }), { cancelled: true, job: 'a'.repeat(32) });
  assert.match(logs.join('\n'), /Review paused after 5 of 6 choices/);
  assert.equal(calls.filter(call => call[0] === 'scan').length, 1);
  assert.equal(calls.some(call => ['generate', 'publish'].includes(call[0])), false);
});

test('abort while a batch dialog is open rejects even an accepted response', async () => {
  const { workflow, calls, session } = fixture({ canvas: false, findings: [{ id: 'F1', text: 'private' }] });
  const controller = new AbortController();
  let dialogs = 0;
  session.ui.elicitation = async () => {
    dialogs += 1;
    if (dialogs === 1) return { action: 'accept', content: { readers: 'both', delivery: 'local', privacyMode: 'llm' } };
    controller.abort();
    return { action: 'accept', content: { finding_1: 'keep' } };
  };
  await assert.rejects(workflow.run({ sessionId, signal: controller.signal }), /cancelled/);
  assert.equal(calls.some(call => call[0] === 'generate'), false);
});

test('generalization still requires explicit unprefilled wording for the exact finding', async () => {
  const { workflow, calls, dialogs } = fixture({ canvas: false,
    findings: [{ id: 'F1', text: 'PRIVATE_NAME', alternative: 'UNREQUESTED_MODEL_REWRITE' }, { id: 'F2', text: 'technical failure' }],
    answers: [{ action: 'accept', content: { readers: 'both', delivery: 'local', privacyMode: 'llm' } },
      { action: 'accept', content: { finding_1: 'generalize', finding_2: 'keep' } }, 'a teammate', true],
  });
  await workflow.run({ sessionId });
  assert.match(dialogs[2], /1 listed finding\(s\) in group 1/);
  assert.ok(renderedText(dialogs[2]).includes('PRIVATE_NAME'));
  assert.equal(JSON.stringify(dialogs).includes('UNREQUESTED_MODEL_REWRITE'), false);
  assert.deepEqual(calls.find(call => call[0] === 'generate')[1].choices, {
    F1: { action: 'generalize', replacement: 'a teammate' }, F2: { action: 'keep' },
  });
});

test('cancelled or invalid generalization cannot complete a batch or dispatch generation', async () => {
  for (const replacement of [undefined, '', ' ', 'x'.repeat(501), true]) {
    const { workflow, calls } = fixture({ canvas: false,
      findings: [{ id: 'F1', text: 'private' }],
      answers: [{ action: 'accept', content: { readers: 'both', delivery: 'local', privacyMode: 'llm' } },
        { action: 'accept', content: { finding_1: 'generalize' } }, replacement],
    });
    if (replacement === undefined || replacement === '') assert.equal((await workflow.run({ sessionId })).cancelled, true);
    else await assert.rejects(workflow.run({ sessionId }), /explicit text/);
    assert.equal(calls.some(call => call[0] === 'generate'), false);
  }
});

test('older or mismatched capture acknowledgements fail before any scan', async () => {
  for (const captureMode of [null, 'llm']) {
    const { workflow, calls } = fixture({ captureMode,
      answers: [{ action: 'accept', content: { readers: 'both', delivery: 'local', privacyMode: 'full' } }],
    });
    await assert.rejects(workflow.run({ sessionId }), /did not confirm/);
    assert.equal(calls.some(call => ['scan', 'generate'].includes(call[0])), false);
  }
});

test('review mode mismatch and nonempty no-redaction findings fail without synthesized approvals', async () => {
  for (const options of [{ reviewMode: null }, { reviewMode: 'llm' }, { findings: [{ id: 'F1', text: 'flagged' }] }]) {
    const { workflow, calls } = fixture({ ...options,
      answers: [{ action: 'accept', content: { readers: 'both', delivery: 'local', privacyMode: 'full' } }],
    });
    await assert.rejects(workflow.run({ sessionId }), /does not match|unexpectedly contains/);
    assert.equal(calls.some(call => call[0] === 'generate'), false);
  }
});

test('smart redaction fails visibly on provider failure rather than falling back to rules only', async () => {
  for (const options of [{ status: 'error' }, { semanticStatus: 'failed' }, { semanticStatus: 'not_requested' }]) {
    const { workflow, calls } = fixture({ ...options,
      answers: [{ action: 'accept', content: { readers: 'both', delivery: 'local', privacyMode: 'llm' } }],
    });
    await assert.rejects(workflow.run({ sessionId }), /rules-only fallback/);
    assert.equal(calls.filter(call => call[0] === 'scan').length, 1);
    assert.equal(calls.some(call => call[0] === 'generate'), false);
    assert.equal(calls.some(call => call[0] === 'scan' && call[1].semantic !== true), false);
  }
});

test('cancelled generation does not synthesize approval', async () => {
  const { workflow, calls } = fixture({ canvas: false, answers: [{ action: 'accept', content: { readers: 'both', delivery: 'local', privacyMode: 'llm' } }, false] });
  assert.equal((await workflow.run({ sessionId })).cancelled, true);
  assert.equal(calls.some(call => call[0] === 'generate'), false);
});

test('export-control turn is excluded without altering prior trajectory', () => {
  const events = [...source, { type: 'user.message', data: { content: '导出 spec' } }, { type: 'tool.execution_start', data: { toolCallId: 'export' } }];
  assert.deepEqual(snapshotEvents(events, 'export'), source);
  assert.equal(events.length, 4);
});

test('aborted invocation cannot start capture', async () => {
  const { workflow, calls } = fixture();
  const controller = new AbortController();
  controller.abort();
  await assert.rejects(workflow.run({ sessionId, signal: controller.signal }), /cancelled/);
  assert.deepEqual(calls, []);
});

test('cancellation during a delayed approval never dispatches generation', async () => {
  const { workflow, calls, session } = fixture({ canvas: false,
    answers: [{ action: 'accept', content: { readers: 'both', delivery: 'local', privacyMode: 'llm' } }],
  });
  const controller = new AbortController();
  session.ui.confirm = async () => { controller.abort(); return true; };
  await assert.rejects(workflow.run({ sessionId, signal: controller.signal }), /cancelled/);
  assert.equal(calls.some(call => call[0] === 'generate'), false);
});

test('identity changes while a dialog is open cannot dispatch a read', async () => {
  const { workflow, calls, session } = fixture({ canvas: false });
  session.ui.elicitation = async () => {
    session.sessionId = '22222222-2222-4222-8222-222222222222';
    return { action: 'accept', content: { readers: 'both', delivery: 'local', privacyMode: 'llm' } };
  };
  await assert.rejects(workflow.run({ sessionId }), /identity/);
  assert.deepEqual(calls, [['pending_review', {}]]);
});

test('double trigger does not open duplicate dialogs or read another snapshot', async () => {
  const { workflow, calls, session } = fixture({ canvas: false });
  let release;
  let ready;
  const opened = new Promise(resolve => { ready = resolve; });
  session.ui.elicitation = () => new Promise(resolve => { release = resolve; ready(); });
  const first = workflow.run({ sessionId });
  await opened;
  assert.equal((await workflow.run({ sessionId })).status, 'already_opening');
  release({ action: 'cancel' });
  assert.equal((await first).cancelled, true);
  assert.deepEqual(calls, [['pending_review', {}]]);
});

function publishingFixture(answers, overrides = {}) {
  const pending = { ...savedReview('full', 0), delivery: 'artifactstore', audience: 'root' };
  const context = fixture({ canvas: false, pending, answers: [
    { action: 'accept', content: { continuation: 'resume' } }, true, ...answers,
  ] });
  context.session.ui.select = async (...args) => { context.dialogs.push(args); return 'Upload saved files'; };
  let plan;
  let dispatched = false;
  const original = context.bridge.call;
  context.bridge.call = async (operation, data) => {
    if (!['package', 'plan', 'publish'].includes(operation) && !(operation === 'status' && dispatched)) return original(operation, data);
    context.calls.push([operation, data]);
    if (overrides[operation]) return overrides[operation](data);
    if (operation === 'package') return { package_id: 'PRIVATE_PACKAGE_ID', audience: 'root', findings: [
      { id: 'PRIVATE_FINDING_ID', file: 'agent-spec.md', category: 'confidential', text: '[PRIVATE_QUOTE](https://evil.invalid)' },
    ], files: { 'agent-spec.md': {} } };
    if (operation === 'plan') {
      plan = { plan_id: 'PRIVATE_PLAN_ID', package_id: 'PRIVATE_PACKAGE_ID', site: data.site, team: null, audience: 'root' };
      return plan;
    }
    if (operation === 'publish') { dispatched = true; return { job: pending.job }; }
    return { status: 'done', publication: { ...plan, status: 'verified', url: `https://artifacts.turing.azure.com/sites/${plan.site}/` } };
  };
  return context;
}

const acceptedName = site => ({ action: 'accept', content: { site } });
const acceptedUpload = { action: 'accept', content: { upload_action: 'upload' } };

test('artifact names are anonymous valid defaults and invalid entries never reach the bridge', async () => {
  const first = defaultArtifactName();
  assert.match(first, /^spec-\d{8}-[a-f0-9]{8}$/);
  assert.notEqual(defaultArtifactName(), first);
  for (const invalid of ['中文名称', '', ' ', 'two words', '../site', 'a'.repeat(65), 5]) {
    const context = publishingFixture([acceptedName(invalid), acceptedName('valid-site'), acceptedUpload]);
    const result = await context.workflow.run({ sessionId });
    assert.equal(result.publication.status, 'verified');
    assert.deepEqual(context.calls.filter(call => call[0] === 'plan').map(call => call[1].site), ['valid-site']);
    assert.equal(context.calls.filter(call => call[0] === 'generate').length, 1);
    const nameForms = context.dialogs.filter(dialog => dialog?.requestedSchema?.properties.site);
    assert.equal(nameForms.length, 2);
    for (const request of nameForms) {
      assert.equal(request.requestedSchema.properties.site.title, 'Artifact name');
      assert.match(request.requestedSchema.properties.site.default, /^spec-/);
      assert.match(request.message, /not a file path/);
    }
    const uploadForm = context.dialogs.find(dialog => dialog?.requestedSchema?.properties.upload_action);
    assert.deepEqual(uploadForm.requestedSchema.required, ['upload_action']);
    assert.equal(uploadForm.requestedSchema.properties.upload_action.default, 'upload');
    assert.match(renderedText(uploadForm.message), /PRIVATE_QUOTE/);
    assert.equal(markdown.parse(uploadForm.message, {}).flatMap(token => token.children || []).some(token => token.type === 'link_open'), false);
    const publish = context.calls.find(call => call[0] === 'publish')[1];
    assert.deepEqual(publish.acknowledged, ['PRIVATE_FINDING_ID']);
    assert.equal(publish.confirm, 'PRIVATE_PLAN_ID');
    assert.equal(publish.package_id, 'PRIVATE_PACKAGE_ID');
    assert.doesNotMatch(JSON.stringify(result), /PRIVATE_/);
  }
});

test('missing, declined, cancelled and repeated invalid name submissions cannot upload or infer defaults', async () => {
  for (const answers of [
    [{ action: 'cancel' }],
    [{ action: 'accept', content: {} }],
    [acceptedName('valid-site'), { action: 'cancel' }],
    [acceptedName('valid-site'), { action: 'accept', content: {} }],
    [acceptedName('valid-site'), { action: 'accept', content: { upload_action: 'cancel' } }],
    [acceptedName('中文'), acceptedName('中文'), acceptedName('中文')],
  ]) {
    const context = publishingFixture(answers);
    const response = await context.workflow.tool.handler({}, { sessionId });
    assert.equal(context.calls.some(call => call[0] === 'publish'), false);
    assert.equal(context.calls.filter(call => call[0] === 'generate').length, 1);
    assert.doesNotMatch(response.textResultForLlm, /not_exported|PRIVATE_/);
    assert.equal(context.logs.some(log => log.startsWith('Your spec is ready')), true);
  }
});

test('duplicate artifact names offer a fresh default and never authorize an overwrite', async () => {
  const context = publishingFixture([acceptedName('existing-site'), { action: 'cancel' }], {
    plan: () => { throw Object.assign(new Error('PRIVATE_SERVER'), { code: 'artifact_exists' }); },
  });
  const result = await context.workflow.run({ sessionId });
  assert.equal(result.publication.status, 'cancelled');
  const forms = context.dialogs.filter(dialog => dialog?.requestedSchema?.properties.site);
  assert.equal(forms.length, 2);
  assert.notEqual(forms[0].requestedSchema.properties.site.default, forms[1].requestedSchema.properties.site.default);
  assert.match(forms[1].message, /never overwritten/);
  assert.equal(context.calls.some(call => call[0] === 'publish'), false);
});

test('pre-dispatch failures preserve exports and permit setup retry without regeneration', async () => {
  for (const code of ['artifact_auth_required', 'artifact_plan_failed']) {
    const answers = [acceptedName('valid-site'), acceptedName('valid-retry'), acceptedUpload];
    const overrides = { plan: () => { throw Object.assign(new Error('PRIVATE_TOKEN'), { code }); } };
    const context = publishingFixture(answers, overrides);
    const response = await context.workflow.tool.handler({}, { sessionId });
    const error = JSON.parse(response.textResultForLlm);
    assert.equal(error.status, 'not_published');
    assert.equal(error.error_code, code);
    assert.match(error.message, /specs are saved/);
    assert.match(error.message, /no upload was started|No upload was started/);
    assert.doesNotMatch(response.textResultForLlm, /PRIVATE_TOKEN/);
    assert.equal(context.calls.some(call => call[0] === 'publish'), false);
    delete overrides.plan;
    const result = await context.workflow.run({ sessionId });
    assert.equal(result.publication.status, 'verified');
    assert.equal(context.calls.filter(call => call[0] === 'generate').length, 1);
    assert.equal(context.calls.filter(call => call[0] === 'publish').length, 1);
  }
});

test('unverified or mismatched upload receipts cannot be called success and disable automatic retry', async () => {
  for (const publication of [undefined, { status: 'uploaded_unverified' }, { status: 'verified', url: 'https://evil.invalid' }]) {
    const context = publishingFixture([acceptedName('valid-site'), acceptedUpload], { status: () => ({ status: 'done', publication }) });
    const response = await context.workflow.tool.handler({}, { sessionId });
    assert.equal(JSON.parse(response.textResultForLlm).error_code, 'publication_unconfirmed');
    assert.equal(context.logs.some(log => log.startsWith('ArtifactStore upload verified')), false);
    context.session.ui.select = async (message, options) => { assert.equal(options.includes('Upload saved files'), false); return undefined; };
    await context.workflow.run({ sessionId });
    assert.equal(context.calls.filter(call => call[0] === 'publish').length, 1);
  }
});

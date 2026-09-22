import { randomUUID } from 'node:crypto';
import { availableSessionModels, currentSessionModel } from './model-selection.mjs';

const PIPELINE = 'abstract-then-redact/v1';
const preparationPhases = Object.freeze({
  draft: 'Drafting the private spec',
  checking: 'Checking the private draft',
  repair: 'Repairing the private draft',
  privacy: 'Reviewing draft privacy',
});

const actions = ['keep', 'remove', 'pseudonymize', 'generalize'];
const emptySchema = { type: 'object', properties: {}, additionalProperties: false };
const reviewBatchSize = 5;
const reviewFormByteLimit = 24000;

export function defaultArtifactName() {
  return `spec-${new Date().toISOString().slice(0, 10).replaceAll('-', '')}-${randomUUID().slice(0, 8)}`;
}

class PreparationFailure extends Error {
  constructor(code, message) {
    super(message);
    this.code = code;
  }
}

function completionMetadata(delivery, readers) {
  const names = { human: ['human-spec.html'], agent: ['agent-spec.md', 'evidence.md'], both: ['human-spec.html', 'agent-spec.md', 'evidence.md'] }[readers];
  if (!names || !delivery || !Array.isArray(delivery.files) || delivery.files.length !== names.length
    || new Set(delivery.files.map(file => file?.name)).size !== names.length
    || delivery.files.some(file => !file || !names.includes(file.name))
    || !delivery.bundle || typeof delivery.bundle !== 'object' || Array.isArray(delivery.bundle)) throw new Error('Invalid selected deliverable metadata. No local paths were exposed.');
  const project = (file, name) => {
    const result = { name };
    if (Number.isSafeInteger(file.bytes) && file.bytes >= 0) result.bytes = file.bytes;
    if (typeof file.sha256 === 'string' && /^[a-f0-9]{64}$/i.test(file.sha256)) result.sha256 = file.sha256;
    return result;
  };
  return { files: delivery.files.map(file => project(file, file.name)), bundle: project(delivery.bundle, 'deliverables.zip') };
}

function selectedFileLocations(delivery, readers, recovery) {
  if (recovery) {
    const names = { human: ['human-spec.html'], agent: ['agent-spec.md'], both: ['human-spec.html', 'agent-spec.md'] }[readers];
    if (!names || delivery?.kind !== 'unvalidated_draft' || !/^[a-f0-9]{64}$/.test(delivery.snapshot_id)
      || !Array.isArray(delivery.files) || delivery.files.length !== names.length
      || new Set(delivery.files.map(file => file?.name)).size !== names.length
      || delivery.files.some(file => !file || !names.includes(file.name))) throw new Error('The selected draft locations could not be verified.');
  } else completionMetadata(delivery, readers);
  const locations = [...delivery.files.map(file => [file.name, file.path]),
    ...(recovery ? [['Selected draft folder', delivery.folder]] : [['deliverables.zip', delivery.bundle.path]])];
  if (locations.some(([, path]) => typeof path !== 'string' || !path.trim())) throw new Error('The selected output locations are unavailable. Saved files remain local.');
  return locations.map(([name, path]) => `${safeMarkdown(name)}:\n\n${safeMarkdown(path)}`).join('\n\n');
}

function contextExcerpt(value, limit = 220) {
  if (typeof value !== 'string') return '';
  return value.length <= limit ? value : value.slice(0, limit) + '\n[Context shortened]';
}

function safeMarkdown(value) {
  return String(value).replace(/[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f\u202a-\u202e\u2066-\u2069]/g, character => `[U+${character.charCodeAt(0).toString(16).toUpperCase().padStart(4, '0')}]`)
    .split(/\r\n|\r|\n/).map(line => line.replace(/[!"#$%&'()*+,\-./:;<=>?@[\\\]^_`{|}~]/g, character => `&#${character.charCodeAt(0)};`).replace(/^\s+/, space => '&#32;'.repeat(space.length))).join('\n\n');
}

function findingActions(finding) {
  const allowed = Object.hasOwn(finding, 'allowed_actions') ? finding.allowed_actions : actions;
  if (!Array.isArray(allowed) || !allowed.length || new Set(allowed).size !== allowed.length || allowed.some(action => !actions.includes(action))) throw new Error('Invalid allowed privacy actions; no choices were inferred.');
  return actions.filter(action => allowed.includes(action));
}

function recommendedDefault(finding, allowed) {
  const action = finding.recommended;
  if (!['keep', 'remove', 'pseudonymize'].includes(action) || !allowed.includes(action)) return undefined;
  if (finding.necessity !== (action === 'keep' ? 'necessary' : 'unnecessary')) return undefined;
  const summary = finding.assessment_summary;
  if (!summary || summary.status !== 'agreement' || !Number.isSafeInteger(summary.count) || summary.count < 1
    || JSON.stringify(summary.necessities) !== JSON.stringify([finding.necessity])) return undefined;
  return action;
}


function suggestedChoice(finding, allowed) {
  if ((finding.necessity === 'necessary' || finding.assessment_summary?.status === 'disagreement'
    || finding.assessment_summary?.necessities?.includes('necessary')) && allowed.includes('keep')) return { action: 'keep' };
  const recommendation = recommendedDefault(finding, allowed);
  if (recommendation) return { action: recommendation };
  const categories = finding.categories ?? [finding.category];
  const personal = ['reputation', 'personal_life', 'health', 'financial'];
  if (Array.isArray(categories) && categories.length && categories.every(category => personal.includes(category)) && allowed.includes('remove')) return { action: 'remove' };
  if (Array.isArray(categories) && categories.length && categories.every(category => category === 'identifier') && allowed.includes('pseudonymize')) return { action: 'pseudonymize' };
  if (allowed.includes('keep')) return { action: 'keep' };
  if (allowed.includes('pseudonymize')) return { action: 'pseudonymize' };
  if (allowed.includes('remove')) return { action: 'remove' };
  return { action: 'generalize' };
}

function scoped(finding) {
  return finding.scope?.schema === 'baseline-field/v2' && /^[a-f0-9]{64}$/.test(finding.scope.field_sha256)
    && Array.isArray(finding.occurrences) && finding.occurrences.length > 0 && finding.occurrences.every(occurrence =>
      Array.isArray(occurrence.path) && Number.isSafeInteger(occurrence.path[0]) && occurrence.path[0] >= 0
      && occurrence.path.every(part => typeof part === 'string' || Number.isSafeInteger(part))
      && Number.isSafeInteger(occurrence.start) && occurrence.start >= 0 && Number.isSafeInteger(occurrence.end) && occurrence.end > occurrence.start
      && (!occurrence.field_sha256 || occurrence.field_sha256 === finding.scope.field_sha256));
}

function groupLabel(finding) {
  const labels = { environment: 'Workspace details', identifier: 'Identifying details', confidential: 'Possibly confidential context', reputation: 'Personal aside', personal_life: 'Personal context', health: 'Health context', financial: 'Financial context', inference: 'Related identifying details' };
  return labels[finding.category] || 'Review this detail';
}

function scopeStats(groups) {
  const occurrences = groups.flatMap(group => group.members.flatMap(member => scoped(member.finding) ? member.finding.occurrences : []));
  return { matches: occurrences.length, items: [...new Set(occurrences.map(occurrence => occurrence.path[0] + 1))].sort((first, second) => first - second) };
}

function compactItems(items) {
  const ranges = [];
  for (let position = 0; position < items.length; position += 1) {
    const first = items[position];
    while (position + 1 < items.length && items[position + 1] === items[position] + 1) position += 1;
    ranges.push(first === items[position] ? String(first) : `${first}–${items[position]}`);
  }
  return ranges.slice(0, 12).join(', ') + (ranges.length > 12 ? '; more items available through Review individually' : '');
}

function groupCard(group) {
  const stats = scopeStats([group]);
  const lines = [`Group ${group.index + 1}: ${group.label} · ${group.members.length} finding(s) · ${group.occurrences} listed occurrence(s) (may overlap)`,
    `Exact quote:\n\n${safeMarkdown(group.finding.text)}`,
    stats.items.length ? `Source items (${stats.items.length}): ${compactItems(stats.items)}.` : 'Original saved finding; detailed source locations are unavailable.',
    'Applies only to this group in the saved draft, not all matching text.'];
  if (group.individual) lines.push('Source offsets are zero-based, end-exclusive:', ...group.members.map(member => member.summary));
  lines.push(safeMarkdown(contextExcerpt(group.finding.reason)), safeMarkdown(contextExcerpt(group.finding.assessment_summary?.notice)));
  let contextsShown = 0;
  for (const member of group.members.slice(0, 2)) {
    const context = member.finding.contexts?.find(value => typeof value === 'string');
    if (context) {
      lines.push(`Source context excerpt:\n\n${safeMarkdown(contextExcerpt(context))}`);
      contextsShown += 1;
    }
  }
  if (group.members.length > 2) lines.push('Review individually to see each member and choose different actions.');
  const related = group.finding.related_contexts?.[0];
  if (contextsShown < 2 && related?.text) lines.push(`Related context (not a removal target):\n\n${safeMarkdown(contextExcerpt(related.text))}`);
  return lines.filter(Boolean).join('\n\n');
}

export function buildReviewGroups(findings) {
  if (!Array.isArray(findings)) throw new Error('Invalid saved findings; no privacy choices were inferred.');
  const identifiers = new Set();
  const fields = new Map();
  const groups = [];
  const matching = new Map();
  for (const [index, original] of findings.entries()) {
    if (!original || typeof original.id !== 'string' || !original.id || original.id.length > 256 || identifiers.has(original.id) || typeof original.text !== 'string' || !original.text) throw new Error('Invalid or duplicate finding identity; no privacy choices were inferred.');
    identifiers.add(original.id);
    const finding = structuredClone(original);
    const allowed = findingActions(finding);
    const bound = scoped(finding);
    const locations = bound ? finding.occurrences.map(occurrence => {
      const path = JSON.stringify(occurrence.path);
      if (!fields.has(path)) fields.set(path, fields.size + 1);
      const kind = occurrence.path.includes('arguments') ? 'tool input' : occurrence.path.includes('result') ? 'tool output' : occurrence.path.includes('content') ? 'message text' : 'source text';
      return `item ${occurrence.path[0] + 1}, ${kind} field ${fields.get(path)}, offsets ${occurrence.start}–${occurrence.end}`;
    }) : ['original finding scope (no detailed locations supplied)'];
    const member = { finding, index, summary: `Finding ${index + 1}: ${locations.join('; ')}.` };
    const key = JSON.stringify([finding.text, finding.category, finding.categories, finding.recommended, finding.alternative, allowed, finding.necessity, finding.assessment_summary, finding.detectors, finding.reason, finding.label]);
    let group = bound ? matching.get(key) : undefined;
    if (!group) {
      group = { finding, index: groups.length, key: `finding_${index + 1}`, label: groupLabel(finding), allowed, members: [], occurrences: 0 };
      groups.push(group);
      if (bound) matching.set(key, group);
    }
    group.members.push(member);
    group.occurrences += bound ? finding.occurrences.length : 0;
  }
  return groups;
}

function reviewForm(entries, batchIndex, batchCount) {
  const labels = { keep: 'Keep', remove: 'Remove', pseudonymize: 'Use a consistent pseudonym', generalize: 'Enter replacement wording', individually: 'Review individually' };
  return {
    entries,
    message: `Stage 2 of 3 · Customize draft privacy · Batch ${batchIndex + 1} of ${batchCount}.\nEvery field has a proposed default, not a privacy fact or inferred consent. Personal asides default to removal; identifiers to pseudonyms. Necessary or uncertain technical failures and corrections default to Keep. Change any choice or review members individually. Accept approves only the matched findings in this saved draft, never all matching text.\n\n${entries.map(entry => groupCard(entry)).join('\n\n---\n\n')}`,
    properties: Object.fromEntries(entries.map(entry => {
      const values = [...entry.allowed, ...(entry.members.length > 1 ? ['individually'] : [])];
      const recommended = suggestedChoice(entry.finding, entry.allowed).action;
      return [entry.key, { type: 'string', title: `Group ${entry.index + 1}: ${entry.label} (${entry.members.length} finding(s))`, description: 'Apply only to the matched findings in this saved draft, or choose individually where offered.', enum: values, enumNames: values.map(value => labels[value]), ...(recommended ? { default: recommended } : {}) }];
    })),
  };
}

function reviewFormBytes(form) {
  return Buffer.byteLength(JSON.stringify({ message: form.message, requestedSchema: {
    type: 'object', properties: form.properties, required: Object.keys(form.properties),
  } }), 'utf8');
}

function batchGroups(entries) {
  const groups = [];
  let current = [];
  for (const entry of entries) {
    const candidate = [...current, entry];
    if (current.length && (candidate.length > reviewBatchSize || reviewFormBytes(reviewForm(candidate, entries.length, entries.length)) > reviewFormByteLimit)) {
      groups.push(current);
      current = [];
    }
    current.push(entry);
    if (reviewFormBytes(reviewForm(current, entries.length, entries.length)) > reviewFormByteLimit) {
      throw new Error('A finding exceeds the native form size limit. The saved review is preserved; no quote was shortened and no choice was inferred.');
    }
  }
  if (current.length) groups.push(current);
  return groups.map((group, index) => reviewForm(group, index, groups.length));
}

export function buildReviewBatches(findings) {
  return batchGroups(buildReviewGroups(findings));
}

function unnecessaryIdentifier(finding) {
  return finding.category === 'identifier' && finding.necessity === 'unnecessary'
    && finding.assessment_summary?.status === 'agreement'
    && JSON.stringify(finding.assessment_summary.necessities) === '["unnecessary"]';
}

function preserveContextAroundIdentifiers(proposed) {
  const edits = proposed.filter(entry => entry.choice.action !== 'keep');
  for (const entry of proposed) {
    if (entry.choice.action !== 'keep' || !entry.group.allowed.includes('generalize')) continue;
    const candidates = new Set();
    let valid = true;
    let changed = false;
    for (const member of entry.group.members) {
      const original = Array.from(member.finding.text);
      for (const occurrence of member.finding.occurrences) {
        const nested = [];
        for (const edit of edits) for (const child of edit.group.members) for (const span of child.finding.occurrences) {
          if (child.finding.scope.field_sha256 !== member.finding.scope.field_sha256
            || JSON.stringify(span.path) !== JSON.stringify(occurrence.path) || span.start >= occurrence.end || span.end <= occurrence.start) continue;
          const start = span.start - occurrence.start;
          const end = span.end - occurrence.start;
          if (edit.choice.action !== 'generalize' || !edit.choice.replacement || start < 0 || end > original.length
            || original.slice(start, end).join('') !== child.finding.text) { valid = false; continue; }
          nested.push({ start, end, replacement: edit.choice.replacement });
        }
        nested.sort((first, second) => first.start - second.start || second.end - first.end);
        const accepted = [];
        for (const span of nested) {
          const previous = accepted.at(-1);
          if (previous && span.start < previous.end) {
            if (span.end > previous.end || (span.start === previous.start && span.end === previous.end && span.replacement !== previous.replacement)) valid = false;
          } else accepted.push(span);
        }
        let replacement = [...original];
        for (const span of accepted.reverse()) replacement.splice(span.start, span.end - span.start, span.replacement);
        replacement = replacement.join('');
        if (replacement !== member.finding.text) changed = true;
        if (!replacement.trim() || Array.from(replacement).length > 500) valid = false;
        candidates.add(replacement);
      }
    }
    if (valid && changed && candidates.size === 1) {
      entry.choice = { action: 'generalize', replacement: [...candidates][0] };
      entry.family = 'context';
    }
  }
}

export function buildReviewPlan(review, groups = buildReviewGroups(review.findings)) {
  const branches = new Map();
  const branchPattern = /^("?gitBranch"\s*:\s*")([A-Za-z0-9][A-Za-z0-9._/-]*)("?)$/;
  for (const group of groups) {
    const match = group.finding.text.match(branchPattern);
    if (match && unnecessaryIdentifier(group.finding) && !branches.has(match[2])) branches.set(match[2], `[BRANCH_${branches.size + 1}]`);
  }
  const proposed = [];
  for (const group of groups) {
    const finding = group.finding;
    const categories = finding.categories ?? [finding.category];
    if (!group.members.every(member => scoped(member.finding)) || !group.allowed.includes('generalize') || !Array.isArray(categories) || !categories.length || categories.some(category => !['identifier', 'environment'].includes(category))
      || (finding.recommended != null && !['pseudonymize', 'generalize'].includes(finding.recommended))) continue;
    const localHome = finding.category === 'environment' && JSON.stringify(finding.detectors) === '["local"]'
      && finding.necessity === 'uncertain' && finding.assessment_summary?.status === 'unavailable';
    let replacement;
    if ((localHome || unnecessaryIdentifier(finding)) && /^(?:\/(?:Users|home)\/[^/\s"'`<>\\]+|[A-Za-z]:\\Users\\[^\\\s"'`<>/]+)(?:[/\\][^\r\n"'`<>]*)?$/.test(finding.text)) {
      replacement = finding.text.replace(/^(?:\/(?:Users|home)\/[^/]+|[A-Za-z]:\\Users\\[^\\]+)/, '[USER_HOME]');
    } else if (unnecessaryIdentifier(finding)) {
      const match = finding.text.match(branchPattern);
      if (match && branches.has(match[2])) replacement = match[1] + branches.get(match[2]) + match[3];
      else if (branches.has(finding.text)) replacement = branches.get(finding.text);
    }
    if (replacement && replacement.length <= 500) proposed.push({ group, choice: { action: 'generalize', replacement } });
  }
  for (const group of groups) {
    if (proposed.some(entry => entry.group === group) || !group.members.every(member => scoped(member.finding))) continue;
    const choice = suggestedChoice(group.finding, group.allowed);
    if (choice.action !== 'generalize') proposed.push({ group, choice });
  }
  preserveContextAroundIdentifiers(proposed);
  const proposedGroups = new Set(proposed.map(entry => entry.group));
  const manual = groups.filter(group => !proposedGroups.has(group));
  const count = proposed.reduce((total, entry) => total + entry.group.members.length, 0);
  const stats = scopeStats(proposed.map(entry => entry.group));
  const lines = [`Stage 2 of 3 · Proposed draft privacy plan\n\n${review.findings.length} findings in ${groups.length} groups. Apply covers ${count} findings: ${stats.matches} matched occurrences across ${stats.items.length} source items, in this saved draft only. Counts may overlap.`,
    'Suggested defaults, not proven privacy facts or model consensus:'];
  let completeExamples = true;
  for (const kind of ['home', 'branch', 'context']) {
    const entries = proposed.filter(entry => entry.choice.action === 'generalize'
      && (entry.family || (entry.choice.replacement.startsWith('[USER_HOME]') ? 'home' : 'branch')) === kind);
    if (!entries.length) continue;
    const example = entries.filter(entry => entry.group.finding.text.length <= 140 && entry.choice.replacement.length <= 140).sort((first, second) => second.group.finding.text.length - first.group.finding.text.length)[0];
    if (!example) {
      completeExamples = false;
      continue;
    }
    const familyStats = scopeStats(entries.map(entry => entry.group));
    const description = kind === 'home' ? 'Home paths: replace the user-home prefix; retain folders and filenames.'
      : kind === 'context' ? 'Overlapping context: preserve the surrounding technical text; generalize only the nested identifiers.'
        : 'Branch labels: use consistent numbered aliases; retain surrounding syntax.';
    lines.push(`${description} ${entries.reduce((total, entry) => total + entry.group.members.length, 0)} findings, ${familyStats.matches} matched occurrences.\n\nExample: ${safeMarkdown(example.group.finding.text)} → ${safeMarkdown(example.choice.replacement)}`);
  }
  for (const [action, label] of [['remove', 'Remove personal/unnecessary details'], ['pseudonymize', 'Pseudonymize identifiers'], ['keep', 'Keep necessary or uncertain technical context (not certified safe)']]) {
    const entries = proposed.filter(entry => entry.choice.action === action);
    if (!entries.length) continue;
    const example = entries.find(entry => entry.group.finding.text.length <= 260);
    if (!example) { completeExamples = false; continue; }
    lines.push(`${label}: ${entries.reduce((total, entry) => total + entry.group.members.length, 0)} findings.\n\nExample: ${safeMarkdown(example.group.finding.text)}`);
  }
  const captureMatches = review.capture_rule_matches;
  if (captureMatches && ['secret_fields', 'secret_pattern_matches'].every(key => Number.isSafeInteger(captureMatches[key]) && captureMatches[key] >= 0) && captureMatches.secret_fields + captureMatches.secret_pattern_matches > 0) lines.push(`Recognized secret patterns were masked before review: ${captureMatches.secret_fields} secret field(s) and ${captureMatches.secret_pattern_matches} pattern match(es). Counts may overlap and are not unique credentials.`);
  const removals = review.hard_removals;
  if (removals && ['secret_pattern_matches', 'encoded_secret_fields'].every(key => Number.isSafeInteger(removals[key]) && removals[key] >= 0) && removals.secret_pattern_matches + removals.encoded_secret_fields > 0) lines.push(`Later preparation: ${removals.secret_pattern_matches} secret-pattern match(es) and ${removals.encoded_secret_fields} encoded-secret field(s) masked already; not unique credentials.`);
  if (manual.length) lines.push(`${manual.reduce((total, group) => total + group.members.length, 0)} findings still need your explicit choices in ${manual.length} groups (unbound scopes or custom wording).`);
  lines.push('Apply approves these changes for these matched findings only. Inspect details / customize shows exact groups and individual review. No guarantee all secrets were found. Generation and upload require separate confirmation.');
  const form = { message: lines.join('\n\n'), properties: { plan: { type: 'string', title: 'Privacy plan', enum: ['apply', 'customize'], enumNames: ['Apply this plan', 'Inspect details / customize'], default: 'apply' } } };
  return { ...form, proposed, manual, groups, available: proposed.length > 0 && completeExamples && form.message.trim().split(/\s+/).length <= 350 && reviewFormBytes(form) <= reviewFormByteLimit };
}

export function snapshotEvents(events, toolCallId) {
  let boundary = events.length;
  if (toolCallId) {
    const found = events.findIndex(event => event.type === 'tool.execution_start' && event.data?.toolCallId === toolCallId);
    if (found >= 0) boundary = found;
  }
  const selected = events.slice(0, boundary);
  const userIndex = selected.findLastIndex(event => event.type === 'user.message');
  if (userIndex >= 0 && /^(?:\/to-spec|to-spec|导出\s*spec|export\s+(?:this\s+session\s+(?:as\s+)?)?spec)[.!。！\s]*$/i.test(selected[userIndex].data?.content || '')) {
    return selected.slice(0, userIndex);
  }
  return selected;
}

export function createWorkflow({ getSession, getBridge, wait = milliseconds => new Promise(resolve => setTimeout(resolve, milliseconds)) }) {
  let currentCapture;
  let latestOutput;
  let preparingJob;
  let progressOpenedJob;
  let active;
  let openedInstance;
  const session = () => getSession();
  const bridge = () => getBridge();
  const assertSession = identifier => {
    if (!identifier || identifier !== session().sessionId) throw new Error('Current-session identity changed; no other conversation was read.');
  };
  const checkpoint = invocation => {
    assertSession(invocation.sessionId);
    if (invocation.signal?.aborted) throw new Error('Export cancelled. No further generation or upload was started. Saved work remains available.');
  };
  async function checked(invocation, operation) {
    checkpoint(invocation);
    const result = await operation();
    checkpoint(invocation);
    return result;
  }
  async function capture(invocation, privacyMode) {
    const events = snapshotEvents(await checked(invocation, () => session().getEvents()), invocation.toolCallId);
    currentCapture = await checked(invocation, () => bridge().call('capture', { events, privacy_mode: privacyMode, capture_order: PIPELINE }));
    if (currentCapture.session_id !== session().sessionId) throw new Error('Native snapshot identity mismatch.');
    if (currentCapture.privacy_mode !== privacyMode) throw new Error('Native bridge did not confirm the requested privacy mode. No scan or generation was started.');
    if (currentCapture.capture_order !== PIPELINE || !/^[a-f0-9]{64}$/.test(currentCapture.sha256)) throw new Error('Native bridge did not bind the abstract-first capture. No preparation was started.');
    return currentCapture;
  }
  async function completed(job, invocation, progress = 'Upload', publishing = false) {
    const started = Date.now();
    let lastProgress;
    while (Date.now() - started < 330 * 1000) {
      const value = await checked(invocation, () => bridge().call('status', { job }));
      if (value.status === 'error' && publishing) {
        throw new PreparationFailure('publication_unconfirmed', 'Local files remain available. The remote publication outcome is unknown; an upload may have partially or fully completed. Do not retry blindly. Check the remote outcome before attempting another upload.');
      }
      if (value.status === 'error' && value.stage === 'scan' && value.draft_available === true
          && ['draft_references_invalid', 'draft_structure_invalid', 'draft_quality_invalid', 'draft_privacy_invalid'].includes(value.error_code)) return value;
      if (value.status === 'error' && value.stage === 'scan' && value.error_code === 'cleanup_unconfirmed') {
        throw new PreparationFailure('cleanup_unconfirmed', 'Preparation could not confirm worker cleanup. Do not start another export until cleanup has been checked. Any saved work is preserved; no automatic restart or upload was started.');
      }
      if (value.status === 'error' && value.stage === 'scan' && value.error_code === 'timeout') {
        throw new PreparationFailure('timeout', 'Preparation timed out after reaching its configured time limit. Any saved work is preserved. Nothing restarted automatically and no upload was started. Time spent answering privacy forms does not count toward this limit.');
      }
      if (value.status === 'error' && value.stage === 'scan' && value.error_code === 'call_budget') {
        throw new PreparationFailure('call_budget', 'The three-call model budget was reached. Saved work is preserved; no additional model call, automatic restart or upload was started.');
      }
      if (value.status === 'error' && value.stage === 'scan' && ['draft_references_invalid', 'draft_structure_invalid'].includes(value.error_code)) {
        const cause = value.error_code === 'draft_references_invalid' ? 'missing or invalid source citations' : 'an invalid draft format';
        throw new PreparationFailure(value.error_code, `The draft contained ${cause}. One automatic repair was attempted but could not produce a valid draft. No final files were approved or uploaded. Saved diagnostics remain local. You can run /to-spec again to try a fresh draft; it will use additional model calls.`);
      }
      if (value.status === 'error') throw new PreparationFailure('export_failed', `${progress}: the export did not finish. No completed output was approved. Saved diagnostic details remain local; no automatic restart, rules-only fallback or upload was performed.`);
      if (value.status === 'done') return value;
      const label = value.stage === 'scan' && typeof value.phase === 'string' && Object.hasOwn(preparationPhases, value.phase)
        ? `Stage 1 of 3: ${preparationPhases[value.phase]}` : progress;
      if (label !== lastProgress) {
        lastProgress = label;
        await session().log(`Learning to Spec · ${label} is running.`, { ephemeral: false });
      }
      await wait(2000);
    }
    throw new Error(`Export job ${job} is still unresolved. No automatic retry was started.`);
  }
  async function form(invocation, message, properties, required = Object.keys(properties)) {
    const answer = await checked(invocation, () => session().ui.elicitation({ message, requestedSchema: { type: 'object', properties, required } }));
    if (answer.action !== 'accept') return null;
    const content = answer.content;
    if (!content || required.some(key => !(key in content))) throw new Error('A required choice was not submitted.');
    return content;
  }
  async function nativeExport(invocation) {
    const selection = await form(invocation, 'Export this conversation as a spec.\n\nAccept sends the original observable conversation to Copilot to draft privately BEFORE redaction, including any personal details, internal information or secrets. Both modes use your quota. Hidden/system/control content is excluded.\n\nNo redaction keeps sensitive content in the draft. Smart redaction then uses rules and Copilot on the draft, masking recognized secrets and asking about uncertain private details or asides. It can miss sensitive content.\n\nAfter your choices, a separate confirmation renders/exports without more model calls. Uploading requires final approval.', {
      readers: { type: 'string', title: 'For whom?', enum: ['both', 'human', 'agent'], enumNames: ['Both', 'People · HTML story', 'Agents · Markdown + evidence'], default: 'both' },
      delivery: { type: 'string', title: 'Save or share?', enum: ['local', 'artifactstore'], enumNames: ['Save files · no upload', 'ArtifactStore · final confirmation required'], default: 'local' },
      privacyMode: { type: 'string', title: 'Privacy', description: 'Both modes first draft from unredacted observable content with Copilot. No redaction skips privacy scanning. Smart redaction reviews the draft with rules and Copilot, never rules alone.', enum: ['full', 'llm'], enumNames: ['No redaction', 'Smart redaction (rules + Copilot)'], default: 'llm' },
    });
    if (!selection) return { cancelled: true };
    if (Object.keys(selection).length !== 3 || !['human', 'agent', 'both'].includes(selection.readers) || !['local', 'artifactstore'].includes(selection.delivery) || !['full', 'llm'].includes(selection.privacyMode)) throw new Error('Invalid export choice.');
    let audience = 'local';
    if (selection.delivery === 'artifactstore') {
      const target = await checked(invocation, () => session().ui.select('ArtifactStore audience', ['Just me · root', 'A team · inherited access']));
      if (!target) return { cancelled: true };
      if (!['Just me · root', 'A team · inherited access'].includes(target)) throw new Error('Invalid audience choice.');
      audience = target === 'Just me · root' ? 'root' : `team:${await checked(invocation, () => session().ui.input('Team slug')) || ''}`;
      if (audience === 'team:') return { cancelled: true };
    }
    const smart = selection.privacyMode === 'llm';
    const preparation = smart ? 'Stage 1 of 3: private Copilot draft, then rules + Copilot privacy review of the draft' : 'Stage 1 of 3: private Copilot draft; no privacy scan';
    await session().log(`Learning to Spec · ${preparation}.`, { ephemeral: false });
    const hostModel = await checked(invocation, () => currentSessionModel(session()));
    const source = await capture(invocation, selection.privacyMode);
    return prepareSnapshot(invocation, source, selection, audience, hostModel);
  }
  async function prepareSnapshot(invocation, source, selection, audience, hostModel, retryOf) {
    const smart = selection.privacyMode === 'llm';
    const preparation = 'Stage 1 of 3: private Copilot draft and separate fact/privacy checks';
    for (;;) {
      const scanned = await checked(invocation, () => bridge().call('scan', {
        session: source.source, readers: selection.readers, delivery: selection.delivery, audience,
        privacy_mode: selection.privacyMode, detection: smart ? 'copilot' : 'none', semantic: smart, pipeline: PIPELINE,
        ...(retryOf ? { retry_of: retryOf, retry_model: hostModel } : hostModel ? { host_model: hostModel } : {}),
      }));
      preparingJob = scanned.job;
      if (session().capabilities.ui?.canvases) {
        openedInstance = randomUUID();
        try {
          await checked(invocation, () => session().rpc.canvas.open({ canvasId: 'learning-to-spec', instanceId: openedInstance }));
          progressOpenedJob = scanned.job;
        } catch {
          checkpoint(invocation);
          await session().log('The progress panel could not open. Your export is still running; wait here for its result rather than starting another export.', { ephemeral: false });
        }
      }
      const prepared = await completed(scanned.job, invocation, preparation);
      if (prepared.status === 'error' && prepared.draft_available === true) {
        const recovery = { job: scanned.job, source, selection, audience, hostModel, errorCode: prepared.error_code };
        const decision = await recoverDraft(invocation, recovery);
        if (!decision.retryModel) return decision;
        retryOf = scanned.job;
        hostModel = decision.retryModel;
        continue;
      }
      const review = await checked(invocation, () => bridge().call('review', { job: scanned.job }));
      if (review.original_source_sha256 !== source.sha256 || review.preferences?.readers !== selection.readers
        || review.preferences?.destination !== selection.delivery || review.audience !== audience) throw new Error('Prepared draft does not match the captured source or export settings.');
      return reviewAndGenerate(invocation, scanned, selection, review);
    }
  }
  async function recoverDraft(invocation, recovery) {
    const draft = await checked(invocation, () => bridge().call('deliverables', { job: recovery.job }));
    if (draft.kind !== 'unvalidated_draft' || typeof draft.folder !== 'string' || !draft.folder
        || !/^[a-f0-9]{64}$/.test(draft.snapshot_id)) throw new Error('The private draft location could not be verified. Saved diagnostics remain local.');
    latestOutput = { job: recovery.job, readers: recovery.selection.readers, delivery: 'local', publicationAttempted: false, recovery };
    preparingJob = undefined;
    await session().log('An unfinished draft is saved, not as a verified spec. Validation did not pass; privacy review/redaction is incomplete and sensitive details may remain. You can keep it locally, retry with a selected model, or explicitly upload it with these risks.');
    if (session().capabilities.ui?.canvases && progressOpenedJob !== recovery.job) {
      openedInstance = randomUUID();
      await checked(invocation, () => session().rpc.canvas.open({ canvasId: 'learning-to-spec', instanceId: openedInstance }));
    }
    const causes = {
      draft_references_invalid: 'The draft has missing or invalid citations after its bounded repair.',
      draft_structure_invalid: 'The draft format is still invalid after its bounded repair.',
      draft_quality_invalid: 'The source-quality check found a factual/material defect or could not return a valid assessment.',
      draft_privacy_invalid: 'Privacy review could not finish within this attempt, or its quotes did not match the generated draft. The model-call limit was not exceeded.',
    };
    const answer = await form(invocation, `Your draft is saved.\n\nPrivate draft folder:\n\n${safeMarkdown(draft.folder)}\n\n${causes[recovery.errorCode] || 'Draft validation did not pass.'}\n\nRetry reuses the SAME captured conversation and reader/privacy choices with a model you select. It uses additional quota and a new bounded attempt; it never reruns the original coding task.\n\nUpload anyway sends the selected UNVALIDATED draft to ArtifactStore. Facts/citations may be wrong. Privacy review/redaction is incomplete: credentials, personal details or embarrassing asides may remain. Choose the audience and confirm the exact upload before anything is sent.`, {
      recovery_action: { type: 'string', title: 'What would you like to do?', enum: ['local', 'retry', 'upload'], enumNames: ['Keep local draft', 'Retry · choose model', 'Upload anyway · unvalidated draft'], default: 'local' },
    });
    const localResult = { status: 'draft_available', quality: 'unvalidated', privacy: 'incomplete', upload_allowed: false,
      message: 'The draft is saved locally. Use /to-spec for recovery choices. It is not validated or privacy-approved.' };
    if (!answer) return localResult;
    if (Object.keys(answer).length !== 1 || !['local', 'retry', 'upload'].includes(answer.recovery_action)) throw new Error('Invalid draft recovery choice. No retry or upload started.');
    if (answer.recovery_action === 'local') return localResult;
    if (answer.recovery_action === 'retry') {
      let models = await checked(invocation, () => availableSessionModels(session()));
      if (!models.length) {
        const current = await checked(invocation, () => currentSessionModel(session()));
        if (current) models = [current];
      }
      if (!models.length) {
        await session().log('Copilot did not expose a selectable model. The draft remains saved locally; change the Copilot model picker and run /to-spec again. No model was guessed and no retry started.');
        return localResult;
      }
      models = [...models.filter(model => model !== recovery.hostModel), ...models.filter(model => model === recovery.hostModel)];
      const selected = await form(invocation, 'Choose the model for this retry only. Available choices come from this Copilot session, not a fixed plugin list. The separate CLI worker still needs access to the model; rejection will not silently switch models. Your conversation model is not changed.', {
        retry_model: { type: 'string', title: 'Retry model', enum: models, default: models[0] },
      });
      if (!selected) return localResult;
      if (Object.keys(selected).length !== 1 || !models.includes(selected.retry_model)) throw new Error('Select a model offered by this Copilot session. No retry started.');
      await session().log('Retry explicitly requested with ' + safeMarkdown(selected.retry_model) + '. Reusing the captured source; the failed draft and receipts remain saved.', { ephemeral: false });
      return { retryModel: selected.retry_model };
    }
    let audience = recovery.audience;
    if (audience === 'local') {
      const target = await checked(invocation, () => session().ui.select('ArtifactStore audience for the unvalidated draft', ['Just me · root', 'A team · inherited access']));
      if (!target) return localResult;
      if (!['Just me · root', 'A team · inherited access'].includes(target)) throw new Error('Invalid audience choice.');
      audience = target === 'Just me · root' ? 'root' : `team:${await checked(invocation, () => session().ui.input('Team slug')) || ''}`;
      if (audience === 'team:') return localResult;
    }
    const publication = await publish(recovery.job, invocation, { snapshotId: draft.snapshot_id, audience });
    return { status: publication.status === 'verified' ? 'draft_uploaded' : 'draft_available', quality: 'unvalidated', privacy: 'incomplete',
      publication, message: 'Unvalidated draft retained. Any verified upload confirms only file transfer and audience, not content correctness or privacy.' };
  }
  async function reviewAndGenerate(invocation, scanned, selection, review) {
    const smart = selection.privacyMode === 'llm';
    if (review.privacy_mode !== selection.privacyMode || typeof review.review_id !== 'string' || !review.review_id || !Array.isArray(review.findings)) throw new Error('Saved review does not match the requested privacy mode. No generation was started.');
    if (review.pipeline !== PIPELINE || !/^[a-f0-9]{64}$/.test(review.abstraction_sha256)
      || !/^[a-f0-9]{64}$/.test(review.source_sha256)) throw new Error('Review lacks a bound abstract-first draft. Start a new snapshot; no legacy review was resumed.');
    if (smart && review.semantic?.status !== 'reviewed') throw new Error('Copilot privacy review did not complete. No rules-only fallback or generation was started.');
    if (!smart && review.findings.length) throw new Error('No-redaction preparation unexpectedly contains findings. No keep-all choices or generation were inferred.');
    review = structuredClone(review);
    const choices = {};
    const groups = smart ? buildReviewGroups(review.findings) : [];
    const recordChoice = (group, choice) => {
      for (const member of group.members) Object.defineProperty(choices, member.finding.id, { value: { ...choice }, enumerable: true });
    };
    await session().log(smart
      ? `Learning to Spec · Stage 2 of 3: ${review.findings.length} findings in ${groups.length} groups in the private draft. Approve a concrete plan or customize up to 5 groups per form; the scan may miss sensitive content.`
      : 'Learning to Spec · Stage 2 of 3: per-finding review skipped because you chose No redaction. No privacy scan or keep-all approval is substituted.', { ephemeral: false });
    let remaining = groups;
    if (groups.length) {
      const plan = buildReviewPlan(review, groups);
      if (plan.available) {
        const answer = await form(invocation, plan.message, plan.properties);
        if (!answer) return { cancelled: true, job: scanned.job };
        if (Object.keys(answer).length !== 1 || !['apply', 'customize'].includes(answer.plan)) throw new Error('Invalid privacy plan choice; no plan was applied.');
        if (answer.plan === 'apply') {
          for (const entry of plan.proposed) recordChoice(entry.group, entry.choice);
          remaining = plan.manual;
          await session().log(`Learning to Spec · Stage 2 of 3: plan explicitly approved for ${Object.keys(choices).length} of ${review.findings.length} findings. Remaining findings still require choices.`, { ephemeral: true });
        }
      }
    }
    async function chooseGroups(selected) {
      for (const batch of batchGroups(selected)) {
        await session().log(`Learning to Spec · Stage 2 of 3: ${Object.keys(choices).length} of ${review.findings.length} choices accepted; ${batch.entries.length} group(s) in this form.`, { ephemeral: true });
        const answer = await form(invocation, batch.message, batch.properties);
        if (!answer) return false;
        if (Object.keys(answer).length !== batch.entries.length || batch.entries.some(entry => !batch.properties[entry.key].enum.includes(answer[entry.key]))) throw new Error('Invalid or incomplete privacy batch; no batch choices were accepted.');
        const accepted = [];
        for (const entry of batch.entries) {
          if (answer[entry.key] === 'individually') {
            const individual = entry.members.map(member => ({ ...entry, finding: member.finding, index: member.index, key: `finding_${member.index + 1}`, members: [member], occurrences: member.finding.occurrences.length, individual: true }));
            if (!await chooseGroups(individual)) return false;
            continue;
          }
          const choice = { action: answer[entry.key] };
          if (choice.action === 'generalize') {
            const replacement = await checked(invocation, () => session().ui.input(`Stage 2 of 3 · Replacement for the ${entry.members.length} listed finding(s) in group ${entry.index + 1}\n\nExact quote:\n\n${safeMarkdown(entry.finding.text)}\n\nEnter replacement wording; it will apply only to this group's listed members.`, { minLength: 1, maxLength: 500 }));
            if (replacement === undefined || replacement === null || replacement === '') return false;
            if (typeof replacement !== 'string' || !replacement.trim() || replacement.length > 500) throw new Error('Replacement wording must be explicit text of 1–500 characters.');
            choice.replacement = replacement;
          }
          accepted.push([entry, choice]);
        }
        for (const [entry, choice] of accepted) recordChoice(entry, choice);
        await session().log(`Learning to Spec · Stage 2 of 3: ${Object.keys(choices).length} of ${review.findings.length} choices accepted for this invocation.`, { ephemeral: true });
      }
      return true;
    }
    if (!await chooseGroups(remaining)) {
      await session().log(`Review paused after ${Object.keys(choices).length} of ${review.findings.length} choices. The private draft and review are saved; no final export or upload was started.`);
      return { cancelled: true, job: scanned.job };
    }
    if (Object.keys(choices).length !== review.findings.length || review.findings.some(finding => !Object.hasOwn(choices, finding.id))) throw new Error('Incomplete scoped privacy choices; no generation was started.');
    const generationMessage = smart
      ? 'Stage 3 of 3 · Apply your privacy choices and render/export the saved draft now? No more model calls occur after approval. No ArtifactStore upload happens yet.'
      : 'Stage 3 of 3 · Render/export the saved draft without redaction? It may contain personal details, internal information or secrets. No more model calls occur after approval. No ArtifactStore upload happens yet.';
    if (!await checked(invocation, () => session().ui.confirm(generationMessage))) return { cancelled: true, job: scanned.job };
    await session().log('Learning to Spec · Stage 3 of 3: deterministic render/export; no more model calls or upload yet.', { ephemeral: false });
    await checked(invocation, () => bridge().call('generate', { job: scanned.job, review_id: review.review_id, choices, confirmed: true, pipeline: PIPELINE, abstraction_sha256: review.abstraction_sha256 }));
    await completed(scanned.job, invocation, 'Stage 3 of 3: render/export');
    const files = await checked(invocation, () => bridge().call('deliverables', { job: scanned.job }));
    const metadata = completionMetadata(files, selection.readers);
    latestOutput = { job: scanned.job, readers: selection.readers, delivery: selection.delivery, publicationAttempted: false };
    await session().log('Your spec is ready.\n' + metadata.files.map(file => file.name).join('\n') + '\nZIP: deliverables.zip');
    if (!session().capabilities.ui?.canvases) await session().log('To find these saved files, run /to-spec and choose Show saved paths. Their exact locations appear only in the native form.');
    if (session().capabilities.ui?.canvases && progressOpenedJob !== scanned.job) {
      const instanceId = randomUUID();
      await checked(invocation, () => session().rpc.canvas.open({ canvasId: 'learning-to-spec', instanceId }));
      openedInstance = instanceId;
    }
    preparingJob = undefined;
    const publication = selection.delivery === 'artifactstore' ? await publish(scanned.job, invocation) : undefined;
    return { status: 'done', job: scanned.job, ...metadata, ...(publication ? { publication } : {}) };
  }
  async function offerSavedReview(invocation) {
    const response = await checked(invocation, () => bridge().call('pending_review', {}));
    if (!response || !Object.hasOwn(response, 'review')) throw new Error('Saved-review discovery returned an invalid response. No snapshot or scan was started.');
    const saved = response.review;
    if (saved === null) {
      if (Number.isSafeInteger(response.legacy_reviews_unavailable) && response.legacy_reviews_unavailable > 0) await session().log('Older scan-first reviews are preserved but cannot resume under abstract-first consent. Choose a new snapshot explicitly.');
      return nativeExport(invocation);
    }
    if (!saved || !/^[a-f0-9]{32}$/.test(saved.job) || typeof saved.review_id !== 'string' || !saved.review_id
      || !/^[a-f0-9]{64}$/.test(saved.snapshot_sha256) || !['full', 'llm'].includes(saved.privacy_mode)
      || saved.pipeline !== PIPELINE || !/^[a-f0-9]{64}$/.test(saved.abstraction_sha256) || !/^[a-f0-9]{64}$/.test(saved.review_source_sha256)
      || !['human', 'agent', 'both'].includes(saved.readers) || !['local', 'artifactstore'].includes(saved.delivery)
      || typeof saved.audience !== 'string' || !saved.audience || saved.audience.length > 256
      || (saved.delivery === 'local' && saved.audience !== 'local')
      || (saved.delivery === 'artifactstore' && saved.audience !== 'root' && !/^team:[a-zA-Z0-9_-]+$/.test(saved.audience))
      || !Number.isSafeInteger(saved.findings) || saved.findings < 0 || (saved.privacy_mode === 'full' && saved.findings !== 0)) {
      throw new Error('Saved-review identity or settings are invalid. No snapshot, scan or generation was started.');
    }
    const modeLabel = saved.privacy_mode === 'full' ? 'No redaction' : 'Smart redaction (rules + Copilot)';
    const answer = await form(invocation, `A completed private draft and review are saved for this conversation.\nPrivacy: ${modeLabel}\nReaders: ${saved.readers}\nDelivery: ${saved.delivery}\nAudience: ${safeMarkdown(saved.audience)}\nFindings: ${saved.findings}\n\nContinue uses that snapshot, not messages added later. It makes no new drafting or privacy-review call. Incomplete prior choices are not assumed; the plan and any remaining groups require new approval. Final render/export needs separate confirmation but no more model calls. Upload needs final approval.${saved.privacy_mode === 'full' ? '\nNo redaction can retain personal details, internal information or secrets in the draft.' : '\nSmart redaction may miss sensitive content.'}\n\nChoose a new snapshot to change the privacy mode, readers, delivery or audience. The saved job will not be deleted.`, {
      continuation: { type: 'string', title: 'How would you like to continue?', enum: ['resume', 'new'], enumNames: ['Continue saved review', 'Start a new snapshot'] },
    });
    if (!answer) return { cancelled: true, job: saved.job };
    if (Object.keys(answer).length !== 1 || !['resume', 'new'].includes(answer.continuation)) throw new Error('Invalid saved-review choice. No snapshot or scan was started.');
    if (answer.continuation === 'new') return nativeExport(invocation);
    const review = await checked(invocation, () => bridge().call('review', { job: saved.job }));
    if (!review || review.review_id !== saved.review_id || review.original_source_sha256 !== saved.snapshot_sha256
      || review.source_sha256 !== saved.review_source_sha256 || review.pipeline !== saved.pipeline || review.abstraction_sha256 !== saved.abstraction_sha256
      || review.privacy_mode !== saved.privacy_mode || review.preferences?.readers !== saved.readers
      || review.preferences?.destination !== saved.delivery || review.audience !== saved.audience
      || !Array.isArray(review.findings) || review.findings.length !== saved.findings) {
      throw new Error('Saved review changed after selection. No choices, new scan or generation were inferred.');
    }
    await session().log('Learning to Spec · Stage 1 of 3: reusing the bound saved preparation; no new capture or privacy scan.', { ephemeral: false });
    return reviewAndGenerate(invocation, { job: saved.job }, {
      readers: saved.readers, delivery: saved.delivery, privacyMode: saved.privacy_mode,
    }, review);
  }
  async function publish(job, invocation, recovery) {
    let dispatched = false;
    try {
      const manifest = await checked(invocation, () => recovery
        ? bridge().call('recovery-package', { job, snapshot_id: recovery.snapshotId, audience: recovery.audience, delivery: 'artifactstore', accept_unvalidated: true })
        : bridge().call('package', { job, evidence: true }));
      if (recovery && (manifest.schema !== 'unvalidated-share-package/v1' || manifest.recovery_id !== recovery.snapshotId
          || manifest.audience !== recovery.audience || manifest.quality !== 'unvalidated' || manifest.privacy !== 'incomplete')) throw new Error('Recovery package does not match the accepted draft and audience.');
      if (!recovery && manifest.schema === 'unvalidated-share-package/v1') throw new Error('Unvalidated drafts require the explicit recovery workflow.');
      let plan;
      let notice = '';
      let proposedName = defaultArtifactName();
      for (let attempt = 0; attempt < 3; attempt += 1) {
        const answer = await form(invocation, `${notice}Name this upload\n\nA unique name is ready. Accept it or edit it. This is the name in https://artifacts.turing.azure.com/sites/NAME/, not a file path. No upload happens yet.`, {
          site: { type: 'string', title: 'Artifact name', description: 'Use letters, numbers, hyphens or underscores; start with a letter or number. Do not put personal details or secrets in the URL.', minLength: 1, maxLength: 64, default: proposedName },
        });
        if (!answer) return { status: 'cancelled', local_files_available: true };
        if (Object.keys(answer).length !== 1 || typeof answer.site !== 'string' || !/^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$/.test(answer.site)) {
          notice = 'That name is not supported. Use the ready-made name below, or letters, numbers, hyphens and underscores.\n\n';
          continue;
        }
        try {
          plan = await checked(invocation, () => bridge().call('plan', { job, site: answer.site }));
          if (!plan || plan.site !== answer.site || typeof plan.plan_id !== 'string' || !plan.plan_id
            || plan.package_id !== manifest.package_id || plan.audience !== manifest.audience
            || (plan.audience === 'root' ? plan.team !== null : plan.audience !== `team:${plan.team}`)) throw new Error('Upload plan does not match the selected package or audience');
          break;
        } catch (error) {
          if (error.code !== 'artifact_exists') throw error;
          notice = 'That name is already taken. A new unique name is ready; existing uploads are never overwritten.\n\n';
          proposedName = defaultArtifactName();
        }
      }
      if (!plan) throw new PreparationFailure('artifact_name_invalid', 'Your specs are saved. The upload name was not accepted; no upload was started. Open the saved files or try a valid unique name without regenerating.');
      const residual = manifest.findings.map(finding => `${safeMarkdown(finding.file)} · ${safeMarkdown(finding.category)}: ${safeMarkdown(finding.text)}`).join('\n\n');
      const warning = recovery ? 'UNVALIDATED DRAFT — facts/citations may be wrong. Privacy review/redaction did NOT complete. These files may contain credentials, personal details or embarrassing asides. Uploading will not fix or validate them.\n\n' : '';
      const message = `${warning}Upload the saved ${recovery ? 'draft' : 'spec'}?\n\nName: ${safeMarkdown(plan.site)}\nAudience: ${plan.team ? `Team ${safeMarkdown(plan.team)} · inherited access` : 'Just me · root (plus service administrators)'}\nFiles: ${Object.keys(manifest.files).map(safeMarkdown).join(', ')}\n\n${residual ? `These flagged details remain in the final files. Upload keeps them:\n\n${residual}\n\n` : ''}Accept explicitly uploads these exact files to this audience. Cancel keeps your local files. No more model calls.`;
      const uploadAction = recovery ? 'upload_unvalidated' : 'upload';
      const properties = { upload_action: { type: 'string', title: 'Ready to upload?', enum: [uploadAction, 'cancel'], enumNames: [recovery ? 'Upload anyway · accept incomplete privacy and validation' : 'Upload this spec', 'Keep local files only'], default: recovery ? 'cancel' : 'upload' } };
      if (reviewFormBytes({ message, properties }) > reviewFormByteLimit) throw new PreparationFailure('artifact_review_too_large', 'Your specs are saved. Too many residual disclosures remain for a safe upload confirmation. Review the local files and privacy choices; no upload was started.');
      const approved = await form(invocation, message, properties);
      if (!approved || approved.upload_action === 'cancel') return { status: 'cancelled', local_files_available: true };
      if (Object.keys(approved).length !== 1 || approved.upload_action !== uploadAction) throw new Error('Invalid upload choice');
      dispatched = true;
      if (latestOutput?.job === job) latestOutput.publicationAttempted = true;
      await checked(invocation, () => bridge().call('publish', { job, confirm: plan.plan_id, package_id: manifest.package_id, acknowledged: manifest.findings.map(finding => finding.id), publish_intent: true, ...(recovery ? { accept_unvalidated: true } : {}) }));
      const result = await completed(job, invocation, 'Upload', true);
      const publication = result.publication;
      if (publication?.status !== 'verified' || publication.plan_id !== plan.plan_id || publication.package_id !== manifest.package_id
        || (recovery && (publication.quality !== 'unvalidated' || publication.privacy !== 'incomplete' || publication.risk_override !== true))
        || publication.site !== plan.site || publication.url !== `https://artifacts.turing.azure.com/sites/${plan.site}/`) {
        throw new PreparationFailure('publication_unconfirmed', 'Local files remain available. The upload did not return a matching verified receipt. Check the remote outcome before any retry.');
      }
      await session().log(`${recovery ? 'Unvalidated draft uploaded by your choice. File transfer and audience verified; content/privacy remain unvalidated.' : 'ArtifactStore upload verified.'} ${publication.url}`);
      return { status: publication.status, url: publication.url, ...(recovery ? { quality: 'unvalidated', privacy: 'incomplete' } : {}) };
    } catch (error) {
      if (error instanceof PreparationFailure) throw error;
      if (dispatched) throw new PreparationFailure('publication_unconfirmed', 'Your specs are saved. The upload outcome is unknown; inspect this exact artifact before any retry. No automatic upload retry occurred.');
      if (error.code === 'artifact_auth_required') throw new PreparationFailure('artifact_auth_required', 'Your specs are saved. ArtifactStore sign-in is unavailable. Complete Azure CLI sign-in in your Microsoft tenant, then upload the saved files; no upload was started.');
      throw new PreparationFailure('artifact_plan_failed', 'Your specs are saved. The upload setup could not complete. No upload was started. Private diagnostic details were saved locally; use the saved files rather than regenerating.');
    }
  }
  async function run(invocation) {
    checkpoint(invocation);
    if (active) return { status: 'already_opening', note: 'Use the existing export; no duplicate operation started.' };
    if (!session().capabilities.ui?.elicitation) throw new Error('This host does not support native privacy forms. Update Copilot to a version with extensions and elicitation. No browser was opened and no approval was inferred.');
    active = (async () => {
      const existing = latestOutput;
      if (existing) {
        const outputChoice = session().capabilities.ui?.canvases ? 'Open saved files' : 'Show saved paths';
        const options = [outputChoice, ...(existing.recovery && !existing.publicationAttempted ? ['Retry or upload draft'] : []), ...(latestOutput.delivery === 'artifactstore' && !latestOutput.publicationAttempted ? ['Upload saved files'] : []), 'Create a new snapshot'];
        const choice = await checked(invocation, () => session().ui.select(existing.recovery ? 'An unvalidated private draft is saved for this conversation.' : 'An export is already ready for this conversation.', options));
        if (!choice) return { cancelled: true };
        if (choice === outputChoice) {
          if (session().capabilities.ui?.canvases) {
            if (preparingJob && preparingJob !== existing.job) openedInstance = randomUUID();
            preparingJob = undefined;
            await checked(invocation, () => session().rpc.canvas.open({ canvasId: 'learning-to-spec', instanceId: openedInstance }));
            return { status: 'opened_saved_files', job: existing.job };
          }
          const files = await checked(invocation, () => bridge().call('deliverables', { job: existing.job }));
          const message = `${existing.recovery ? 'Unvalidated draft · facts/citations may be wrong and privacy review is incomplete.' : 'Your selected files are saved locally.'}\n\nOpen or copy these exact paths on the computer running Copilot. Keep the Markdown files together. No file was opened or uploaded by this form.\n\n${selectedFileLocations(files, existing.readers, Boolean(existing.recovery))}`;
          const properties = { location_action: { type: 'string', title: 'Saved file locations', enum: ['done'], enumNames: ['Done'], default: 'done' } };
          if (reviewFormBytes({ message, properties }) > reviewFormByteLimit) throw new Error('The selected output paths exceed the native form size limit. Saved files remain local.');
          const answer = await form(invocation, message, properties);
          if (!answer) return { cancelled: true, job: existing.job };
          if (Object.keys(answer).length !== 1 || answer.location_action !== 'done') throw new Error('Invalid saved-file acknowledgement. No file was opened or uploaded.');
          return { status: 'saved_paths_shown', job: existing.job };
        }
        if (choice === 'Upload saved files' && options.includes(choice)) return { status: 'done', publication: await publish(latestOutput.job, invocation) };
        if (choice === 'Retry or upload draft' && options.includes(choice)) {
          const recovery = existing.recovery;
          const decision = await recoverDraft(invocation, recovery);
          if (!decision.retryModel) return decision;
          return prepareSnapshot(invocation, recovery.source, recovery.selection, recovery.audience, decision.retryModel, recovery.job);
        }
        if (choice !== 'Create a new snapshot') throw new Error('Invalid export choice.');
        return nativeExport(invocation);
      }
      return offerSavedReview(invocation);
    })();
    try { return await active; } finally { active = null; }
  }
  return {
    run,
    command: { name: 'to-spec', description: 'Export this conversation as a human story or agent handoff', handler: async context => {
      if (context.args?.trim()) { await session().log('Use /to-spec without a path. It exports the current conversation.'); return; }
      try { await run(context); } catch (error) { await session().log(error.message, { level: 'error' }); }
    } },
    tool: { name: 'learning_to_spec', description: 'Export this current conversation to a spec. Trigger on “to-spec”, “导出 spec”, or “export spec”. Opens native Copilot forms for human HTML/agent Markdown, privacy choices and delivery. No session ID or file path is accepted. Never executes the historical task.', parameters: emptySchema,
      handler: async (args, invocation) => {
        if (args && Object.keys(args).length) throw new Error('This tool only exports the current conversation; no arguments are accepted.');
        try {
          return { textResultForLlm: JSON.stringify(await run(invocation)), resultType: 'success' };
        } catch (error) {
          if (!(error instanceof PreparationFailure)) throw error;
          const publication = error.code === 'publication_unconfirmed' || error.code.startsWith('artifact_');
          return { textResultForLlm: JSON.stringify({ status: publication ? 'not_published' : 'not_exported', error_code: error.code, message: error.message,
            next_action: error.code === 'publication_unconfirmed'
              ? 'Explain that local files remain available and the remote publication outcome is unknown. Do not retry blindly or start another upload automatically. Check the remote outcome first.'
              : publication ? 'Explain that the specs are already saved and no upload started. Fix upload setup and choose Upload saved files; do not regenerate or retry automatically.'
              : 'Explain this bounded failure to the user. Do not retry or start a new export automatically.' }), resultType: 'success' };
        }
      } },
    canvas: { id: 'learning-to-spec', displayName: 'Learning to Spec', description: 'Read-only preview and downloads of completed, selected exports. Use /to-spec to generate them with native privacy forms.', inputSchema: emptySchema,
      open: async context => {
        assertSession(context.sessionId);
        if (preparingJob) {
          const progress = await checked(context, () => bridge().call('progress_canvas', { job: preparingJob }));
          return { url: progress.url, title: 'Your spec · Learning to Spec', status: 'Live progress · read-only · no upload authority' };
        }
        if (!latestOutput) throw new Error('No completed export yet. Use /to-spec; this preview cannot authorize generation or uploads.');
        const output = await checked(context, () => bridge().call('output_canvas', { job: latestOutput.job }));
        return { url: output.url, title: 'Your spec · Learning to Spec', status: 'Read-only selected files · no upload authority' };
      } },
  };
}

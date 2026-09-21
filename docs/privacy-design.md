# Privacy review, not just redaction

The plugin reduces disclosure while preserving a useful engineering story. It does not guarantee anonymity, legal compliance, complete detection or factual correctness.

## Explicit reader and delivery choices

New exports require a Human, Agent or both-reader choice, and local-files or ArtifactStore delivery. The Studio has no preselected reader, destination or detection method. These choices join the source/audience-bound privacy review. Findings then need explicit keep/remove/pseudonymize/generalize decisions and a final confirmation before generation. Choosing local files blocks the upload path; choosing ArtifactStore still does not authorize publication.

The generator reviews a joint draft in the private workspace, but the delivery folder and single ZIP contain only the selected reader files. Agent-only delivery and publication contain Markdown only. The Human HTML is not smuggled into an Agent-only package as a landing page. Existing pre-selection reviews can still be inspected with legacy tools, but new selected delivery requires a fresh review.

## Three different questions

For each finding, ask **what could this reveal**, **is it necessary for the technical task**, and **is this audience entitled to see it**. A name can be unnecessary without being a secret. A failed test can be uncomfortable but essential. A hospital location and a separate appointment reference can jointly reveal private context even without a name.

The detector distinguishes identifiers, linkable workspace context, health, personal finances, private life/protected attributes, possible reputational asides, confidential third-party/business content and cross-context inference risks. “Potentially uncomfortable” is a suggestion to review, not a claim about the user's feelings or competence.

## Trust boundaries

1. **Local preflight:** snapshot the source read-only; remove recognized credentials, hidden reasoning and binary payload fields. Known encoded secret forms are checked locally. This is bounded detection, not arbitrary decoding/OCR.
2. **Private review:** group repeated findings and expose exact spans, surrounding text, related clues, occurrence locations, necessity, rationale and alternatives. Choose keep, remove, pseudonymize or generalize. Personal/contextual findings never have a bulk recommendation, even when a model calls them unnecessary: a model can wrongly include an adjacent technical instruction in the same span. Bulk suggestions are limited to identifiers, environment details and user-added exact phrases, and never overwrite prior individual choices. Recognized credentials cannot be restored using a keep choice.
3. **Optional contextual model review:** Copilot receives locally pre-masked text only after explicit consent. Other private context can still be present. Without consent, local rules/manual choices work without a model. No cloud fallback happens silently. Model suggestions must match a literal source quote; all repeated occurrences are grouped, never fuzzy-matched. Invalid suggestions stop rather than becoming edits. Semantic findings with necessary/uncertain task necessity have no bulk recommendation: the user must choose individually. Cross-window review is bounded and may miss unflagged combinations.
4. **Reduced-source generation:** the normal generation/review pipeline sees the transformed Copilot events, never the original session. Keep source roles, event order, tool outcomes and technical failures. A pseudonym is not a runnable address, credential or path.
5. **Final-file review:** inspect both reader documents and evidence. Build a strict allowlist: `index.html` (the Human story), `agent-spec.md`, optionally `evidence.md`. No `_support`, source transcript, private decisions, reversal map or model cache. Rescan exactly those bytes; bind approval to their hashes and the reviewed audience.
6. **Intentional upload:** show a fresh destination/access plan. Refuse existing artifact names. Root publication first establishes an empty placeholder with owner-only access, verifies that policy, and only then sends approved content. Teams inherit their displayed policy; it is rechecked before upload. Network uncertainty does not trigger an automatic retry or deletion.

Original sessions are never overwritten. Review files remain private local data, not encrypted storage. Deleting a publication does not revoke copies previously obtained by recipients. Team permissions can change later. Service administrators may have access even when root sharing is owner-only.

## Choosing an operation

| Choice | Appropriate use | Important limitation |
|---|---|---|
| Keep | Essential, audience-appropriate detail | Explicit decision, not a declaration that the value is harmless everywhere |
| Pseudonymize | Preserve relationships without literal identity | Consistent aliases still allow linking and inference; not anonymity |
| Remove | Incidental private detail | Select a span, not a whole turn; don't erase a technical failure with the aside |
| Generalize | Retain the constraint at a safer level | User supplies/approves the replacement; do not invent success, consent or causality |

Generalizations are marked in the reduced source as `GENERALIZED_DETAIL`, not silently substituted as historical quotations. The generator and reviewer must not use them as proof of execution or original authorization. Alias and removal markers carry no reverse map into a shared document.

Reopened older reviews retain their explicit decisions and provenance, but their bulk suggestions are filtered through the current individual-choice policy in both CLI and Studio. A saved legacy recommendation cannot re-enable bulk deletion of personal/contextual findings.

Removing a broader span overrides narrower replacement edits within it, but never silently overrides an explicit keep: overlapping keep/removal choices block generation. Partially overlapping replacements also require a consistent selection. Inspect the resulting documents. If a detail is necessary but cannot safely be disclosed, retain the dependency and require the receiving Agent to obtain it from an authorized current environment.

## What "embarrassing" does and does not mean

Removing the literal words is not enough: a story should not needlessly announce that the user had a personal aside or infer what a removed span meant. Whole-edition review has a separate `data_minimization` check and a bounded reduced-source/candidate index. Every indexed event requires an explicit exclusion/technical-necessity/unresolved disposition, literal reduced-source grounding, and exact authored locations for retained or problematic context. Unresolved findings block publication through the existing bounded repair budget. No original removed values or reversal maps are reintroduced to make this check.

The index is not a classifier: markers can also be legitimate code examples in a privacy-engineering task. It does not prohibit words such as “private” or erase failures to flatter the user. Up to 32 marked events and 32000 characters are indexed, with omissions reported; the reviewer must still read uncited prose and the complete edition. Its checked dispositions remain private, not narrative content. Exact quotation validation checks provenance, not semantic truth. A missed paraphrase remains possible, and literal-canary absence is a separate, weaker endpoint.

Index v2 first covers distinct transformation markers and source roles instead of taking the first 32 marked events. Repeated earlier paths must not silently consume the entire attention budget before a later selected disclosure. This policy uses only reduced marker syntax; it does not inspect original values or infer a person's sensitivity. Coverage counts include omissions, and a marker's presence may still be a literal software example. Old v1 receipts keep their original chronological interpretation during offline validation.

The system does not rank people, infer emotional states, or label a user incompetent. It proposes source-grounded *disclosure choices*. Self-deprecation, interpersonal blame, anxiety about asking questions, job dissatisfaction, intimate-life asides and another person's HR information can be incidental to an engineering story. In contrast, a wrong hypothesis, a failing build, repeated debugging and a user's correction often explain the solution and must remain.

Example: "I feel like a fraud. The migration failed twice; do not deploy before the rollback test passes." The removable unit is the personal aside. The failed attempts, negative outcome and deployment prerequisite are not reputation cleanup. If those are intertwined, offer a user-approved generalization that retains the constraint; do not rewrite history into a success.

The same rule applies to personal health and scheduling: preserve an operational time constraint if needed, without asserting a diagnosis or copying the reason for the appointment. A domain term in product code, such as a health endpoint or a salary fixture, is not evidence of a person's health or finances. The user can keep appropriate details, add exact phrases the detector missed, and revise decisions before regenerating.

## Threat model and implementation mechanics

| Boundary | Failure to prevent | Mechanism | Remaining limitation |
|---|---|---|---|
| Transcript → detector | Credentials enter a model prompt | Local secret/field removal; bounded base64, hex, URL/entity checks | Not arbitrary decoding, OCR, nested encrypted payload analysis or exhaustive secret detection |
| Detector → edit | Model invents or paraphrases a deletion span | Exact slot/literal matching, staged validation, one bounded repair, private diagnostics | Detection itself can miss context or propose unnecessary removal |
| Multiple turns → recipient | Separately harmless clues jointly reveal a fact | Individual-window review plus a bounded cross-window pass; explicit inference-risk category | Long-range unflagged combinations may not fit the final window |
| Review → generation | Defaults silently decide the user's comfort | Per-finding decisions; uncertain semantic cases excluded from bulk suggestions; source/audience binding | The user still needs to inspect the narrative and evidence |
| Generation → share package | Evidence, caches or reversal maps undo redaction | Three-file allowlist, final-byte rescan, explicit residual findings review | Final contextual review is human-led; a regex rescan is not a semantic proof |
| Package → service | Wider defaults, changed team policy or replaced files | Package hashes, fresh access plan, harmless placeholder, ACL checks before/after content, hash verification | Service admins and later ACL changes remain outside the plugin's guarantee |

There is no numeric "privacy score" that authorizes publishing. Such a score would hide uncertainty about unknown disclosures. The release gate is instead a set of inspectable conditions: valid source mapping, complete choices, exact final files, acknowledged residual findings and a confirmed current audience.

Private review directories contain source text, replacement decisions and diagnostics. They are not encrypted by the plugin; keep them outside shared/synced folders, restrict local account access, and apply an appropriate retention policy. Do not share studio access fragments, diagnostic files or model-call caches. The localhost UI requires an unguessable Bearer capability, rejects other origins/hosts, disables caching and keeps the Human preview in a no-script sandbox. It is not a defense against another process already running as the same user.

## Evaluation, without misleading success metrics

Measure credential leakage, private-aside semantic cues, repeated copies, composed disclosure and engineering utility separately. Merely changing an identifier in "my appointment at X" / "X is an oncology center" does not break the inference. Merely removing one word from a sensitive sentence does not demonstrate that its meaning disappeared.

Report uncertain proposals separately from actual user-approved edits. Preserve tool success flags and event count, and check both task-specific outcomes and acceptance boundaries. Finite tests do not prove that every useful fact in the original session survives, or that every possible paraphrase is private. Keep experiment fixtures and reports outside the distributed plugin.

## Research grounding

- [Rescriber, CHI 2025](https://arxiv.org/abs/2410.11876): user-led detection, replace/abstract/revert and visible control.
- [IBM contextual privacy, ACL Findings 2025](https://arxiv.org/abs/2502.18509): intent and necessity before reformulation.
- [Operationalizing Data Minimization, 2025](https://arxiv.org/abs/2510.03662): privacy–utility trade-offs cannot be reduced to deleting the most characters.
- [CI-Work, ACL Industry 2026](https://aclanthology.org/2026.acl-industry.103/): dense enterprise context and information-flow boundaries.
- [PiSAs, July 2026](https://arxiv.org/abs/2607.05318): task appropriateness and recipient visibility are separate.
- [ToolMinimize, August 2026](https://arxiv.org/abs/2608.24957): different minimization operations, not only allow/block.
- [TOP-R, revised September 2026](https://arxiv.org/abs/2512.16310): individually incomplete clues can compose into sensitive disclosure.
- [AgentDAM, NeurIPS D&B 2025](https://arxiv.org/abs/2503.09780): evaluate actual disclosure and task utility, not merely privacy-aware answers.

Implementation design also draws on Presidio's recognizer/operator separation, Gitleaks' credential/encoding checks, and LLM Guard's input/output boundary checks. These reference projects are not runtime dependencies. PII token classifiers alone do not solve private-aside or inference detection. The local rules and model-assisted review are complementary, fallible layers.

## Testing boundaries

Synthetic SWE-chat injections must use invented, nonfunctional credentials and invented people. Evaluate direct leakage, encoded/repeated copies, indirect self-disclosure, cross-turn composition and technical negative controls separately. Verify all shipped files, including Evidence; a clean Human page with a leaking attachment is a failure. Report model-review failures and preservation failures, not just successful cases. Never equate finite synthetic coverage with real-world recall.
# Cross-turn local review

New occurrences added by contextual model review are limited to the supplied field and fields with exactly equal complete text in the locally sanitized baseline (decoded strings, not raw session-file bytes). Different surrounding text is not matched merely because it contains the same quote. A field with different surrounding text needs its own finding or explicit custom redaction. Identical full fields still share a choice, including exact repeated echoes across roles.

A successful contextual review promotes a fresh local-only `privacy-review/v1` scan to `privacy-review/v2`, using the `baseline-field/v2` scope policy. Automatic local groups are partitioned by complete sanitized field text. Within one field context, the same exact quote shares one decision across local and model detectors, including different categories; each local judgment, model assessment, detector category, and its occurrence provenance remain separate in the private audit. Identical full fields still share that decision. Explicit custom redactions remain global because the user selected that exact text globally. Every scoped record requires an explicit choice, including local identifiers; contextual review does not add bulk defaults.

This partition makes keeping a literal technical fixture while removing the same identifier from a different context expressible without overriding a global keep. Model assessments are canonically retained with their supplied slot and review-pass provenance. Conflicting necessities become uncertain and clear the shared replacement suggestion, even if the underlying suggestions agree; incompatible replacement suggestions are also cleared rather than choosing the latest answer. Original assessments remain intact. These are model assessments, not verified sensitive attributes. Raw reasons, alternatives and assessments are private audit data, not authorization to display inferred diagnoses, status or motives, or to prefill a rewrite. A presentation layer must preserve exact source scope and decision IDs while distinguishing model assessments from source facts. This change does not solve rationale hallucination or establish semantic correctness.

Promotion preserves the exact parent review bytes in a hash-bound private `audit/review-<sha256>.json` snapshot and binds that parent into the new approval identity. Earlier choices are not migrated or overwritten; their old identity cannot authorize the new review. Subsequent v2 reviews retain their audit ancestry and require a newly bound approval. Legacy v1 reviews still load and transform under their old policy, but a v1 review with any prior semantic findings must start a fresh scan before using v2. Old global semantic occurrences are never guessed into contexts or silently narrowed. No audit directory is part of a share package.

Publication first checks any existing destination audit against the exact input-review bytes, before constructing or invoking a model backend. New snapshots use unique same-directory temporary files, flush and verify their complete bytes, then publish with an atomic no-overwrite hard link. Existing audit files are never repaired or replaced. Unsupported hard links or I/O failures fail closed; an interrupted staging write cannot leave a partial final snapshot. An already conflicting final audit requires explicit investigation outside this operation, not an automatic overwrite. Normal exception cleanup removes only the invocation's staging files; a process crash may leave an unreferenced private staging file. This is not a guarantee against disk corruption or power-loss durability failures.

A nonblocking OS file lock serializes the initial preflight and the complete final revalidation/audit/publication transaction for each review directory. Model work does not hold the lock. A competing completed review makes the older pass stale and prevents its publication; source/baseline identity is revalidated under the final lock. Busy publication is refused rather than waiting or dispatching again. Review and diagnostic writes use unique verified staging files. The private `.privacy-review.lock` file remains in place to preserve one lock identity; the operating system releases ownership when the handle/process closes. Do not delete this file to bypass an active publisher. The lock coordinates these semantic-review publishers, not arbitrary external writers or an older running implementation.

V2 loading additionally checks nonempty assessment source records, bound ancestor review IDs, recognized pass-name syntax, and exact baseline slot/path/field agreement. Retained local judgments must match the originating v1 audit metadata and complete-field occurrence partition. These are structural provenance checks, not proof of which provider executed a pass, the precise historical prompt-window membership, or the truth of a model rationale. An actor able to rewrite reviews and all their hashes is still outside this integrity boundary; no signature or provider attestation is implied.

V2 pseudonyms use a consistent alias for the same literal across contextual partitions. Incompatible non-keep edits on the same exact span are refused; equivalent edits coalesce deterministically. Keep-versus-edit and partial-overlap conflicts fail closed, including conflicts with explicit global custom choices. Full containment retains the existing larger-span behavior. Receipt operation counts measure selected finding-occurrences, not unique physical edits. Hard-secret removal, source integrity checks, and technical-preservation requirements are unchanged.

Local review also links first-person workplace, location, schedule, affiliation and household clues across different user turns when more than one facet is present. These are uncertain, source-linked suggestions, not inferred identities or proven disclosures; each still requires an individual decision. Quoted/fenced examples and non-user tool output are excluded from this particular heuristic. It is intentionally incomplete: third-party, paraphrased and other semantic combinations can escape it. Contextual review remains opt-in, not silently enabled.

Common English/Chinese clause boundaries separate a private aside from a following technical preservation instruction. This is a narrow heuristic, not a language parser or guarantee. Inspect exact spans before applying changes. Do not remove technical failures, corrections, negations or acceptance conditions to make a story look better. New unseen fixtures must measure detection, over-proposal and technical preservation separately from user-selected removals.

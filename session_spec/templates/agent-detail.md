# Actionable Agent handoff

Optimize for the receiving agent's next correct decision, not an impressive retrospective. The reader has not seen the conversation. It should resume at the last reliable checkpoint, avoid disproved routes, and adapt the successful mechanism without reading the whole archive first. Do not promise seamless execution when repo/access/runtime is missing: state the boundary and the smallest useful check. Naturally follow the source conversation's language; prescribe a target only if an explicit OUTPUT_LANGUAGE_CONTRACT is supplied.

## Three reading paths

1. Resume: last known workspace, active goal, established facts, unresolved acceptance, first useful move. A completed task can have no immediate action; do not fabricate work.
2. Continue or transfer: concrete conditional procedures, expected observations, mismatch branches, stop conditions and adaptation points. Future instructions are not evidence they were performed.
3. Explain: a decision trajectory, not a tool transcript. Connect meaningful actions, findings, decisions and state changes. Name tools inline only when they help explain a turning point. Preserve consequential mistakes, corrections and failed routes, not every invocation or retry.

## Trajectory compression

The unit of history is a changed understanding or task state, not a tool call. Group repeated searches, reads, edits and checks that answer the same question into one semantic step. Omit routine navigation, polling, unchanged retries, incidental command syntax and raw request/result payloads from prose. Usually one to three meaningful steps explain a phase; this is guidance, not a quota or truncation rule. Split only when a distinct finding changes the next decision.

For each retained step, explain what was attempted, what was learned (including failure or uncertainty), and why the next action followed. Preserve user corrections, disproved assumptions, the successful mechanism and unresolved acceptance. Retain an exact command, flag, error signature or short snippet only if needed to reproduce a discriminating check, avoid a known failure, or understand the mechanism. Do not paste entire tool arguments, source files, diffs or logs. The renderer does NOT insert raw payloads or add unselected calls; essential meaning must be in your finding/decision, with refs for lookup. Full sanitized evidence remains separate.

Give each field a distinct job: summary = trigger and direction; tool_steps = key action -> finding -> decision; rationale = additional recorded/inferred choice rationale, not a repeat of each step (not_recorded when absent); observation = net result and acceptance boundary; next_state = what that enabled or left blocked. Do not repeat the same paragraph in these fields or in paths. Short acknowledgements still map to human_refs but need no separate prose unless they changed the task.

`article.agent_markdown` starts with one H1 title, then the working contract and mechanism map: effective goals/constraints, exact artifacts/interfaces, final values/units and causal implementation. Use meaningful paths, symbols and snippets, not every file inspected. The renderer inserts resume after the H1 and adds continuation, recipes, history and paths. Do NOT duplicate these in agent_markdown or add a free-floating historical-command catalogue.

Include actual E000001-style source refs next to material facts in agent_markdown itself, as well as refs in the structured record. Prefer 1–3 discriminating anchors per claim, not long ID lists. The publisher replaces visible IDs with compact role/tool links to a SEPARATE evidence.md companion (role, tool/target, source excerpt and evidence limit). Do not author an evidence appendix yourself. Essential findings must still be explained inline; a citation cannot replace an explanation. Full ref mappings and raw events remain auxiliary evidence, not required to understand the first correct action.

## One owner per fact, consistent across reading paths

Source strength follows the payload, not only the outer event role. A delegated agent's tool result reporting "build passed" is an implementer report unless the source also exposes the actual matched build output. Do not upgrade it to independent verification. An unpaired log can establish that output was recorded but not which command/revision it belongs to. Attribute these distinctions consistently in brief, Human narrative, Agent checkpoint and trajectory.

Resume owns the historical checkpoint, portability and first move. Do not prefix its fields with repeated labels such as "Last reliable state:". Start agent_markdown with the H1 followed directly by the working-contract heading, not a second checkpoint paragraph. The contract owns goals/invariants; the mechanism map owns entry points and final parameters; continuation owns actionable future branches; trajectory owns the historical explanation. A brief reminder is fine, but do not copy full acceptance, path lists or disclaimers into every section. In continuation, expected describes a positive observable result; otherwise describes the mismatch branch. Do not put the failure case inside expected.

Unrecorded goal-level acceptance is not the same as an accepted task or a new feature request. When continuation of the original task calls for the missing acceptance, make it the first route after a present-state check, not conditional on another complaint. If runtime/access is missing, mark that route blocked with its prerequisite; do not silently replace it with "wait until the bug returns". A genuinely completed task can have no immediate action. Historical next_state says what actually followed or remained unresolved at that point, never instructions to today's receiving agent that contradict resume/continuation. Check this across the last phase, paths, first move and acceptance.

The actual human commission defines the goal level. Local edits and specified focused tests can be the entire commissioned outcome; do not automatically add integration, commit, deployment or third-party approval as completion requirements. Conversely, local checks do not replace an explicitly requested runtime outcome. A conditional instruction to review before a later integration does not require that integration now. Each continuation done_when ends at that route's authorized goal; keep excluded later stages out of its necessary conditions, even for a proposed route. Missing authorization is a stopping boundary, not unfinished local work.

Tool names alone ("Read, Edit, Bash") are insufficient. Each semantic tool step includes 1–4 `usage` summaries, grouped by meaningful operation rather than invocation. Each summary names the actual tool, the concrete object/entry point and what was done there. For example: Read — locate FAQSection in app/[locale]/page.tsx; Edit — insert the two sections before FAQ while preserving the existing route; Bash — use the targeted Remotion typecheck, not a whole-repo build. The following `finding` explains the actual result and limitations; `decision` explains the consequence. Keep representative commands/flags when they change the outcome, never entire edit payloads or source files. Every usage item must cite actual tool events in that step's tool_refs; do not invent a tool that the historical host did not record.

If the source genuinely omitted a tool name or a uniquely paired request, do not invent one to satisfy the display format. `tool: "unknown"` is allowed only for that source condition: explain the missing identity/pairing in action/finding and preserve the known operation/result at its evidenced scope. Prefer a named representative when one is actually available, or omit a redundant unknown item while keeping its material finding. A reviewer must not require unavailable upstream metadata.

The first useful move should establish present state before replaying a historical plan. If any proposed reproduction can create live orders, charge payments, send messages, deploy or destroy state, put current authorization, controlled test-data/environment and stop/ask prerequisites at the resume entry, not only in a later footnote. Prefer an existing-log/read-only or non-production discriminator when those prerequisites are absent. Diagnostic sufficiency is a checkpoint for choosing a repair, not goal-level acceptance: do not use "either the feature works OR the logs identify a fault" as done_when. A blocked diagnosis can be handed off honestly without claiming task completion.

Receiving-agent acceptance: the Markdown alone supplies (a) current checkpoint and what remains, (b) entry files/symbols, dependencies and relevant final values, (c) first safe discriminating action, expected result and mismatch branch, (d) successful method and failure to avoid, (e) goal-level acceptance and stopping boundary, (f) how to adapt the mechanism to another checkout. State missing access/repo/runtime/commit information instead of promising literal seamless execution. Distinguish historical validation from future recommendations. Generalities such as "check the repo" or "use the tools" do not satisfy this contract.

## Required structured record

Use `article.agent_detail` with this shape. Replace placeholder descriptions with source-grounded content, in the source conversation's language:

{
  "schema": "agent-detail/v3",
  "resume": {
    "checkpoint": "Last reliable task state, outstanding request if any, done vs unverified, known date/commit.",
    "workspace": "Repo/branch/commit/cwd and dirty state when recorded; prerequisites/artifacts/runtime needed to resume. Historical state is not current state. Distinguish machine paths from portable repo-relative paths. Unknown stays unknown.",
    "next_action": "Smallest useful first move and trigger: exact entry point or clearly proposed command, not 'review the repo'. If complete, explain when new work is warranted.",
    "verification_boundary": "What need not be repeated while premises hold, what needs a drift check, and missing goal-level acceptance.",
    "refs": ["E000001"]
  },
  "continuation": [{
    "title": "Concrete continuation route", "basis": "proposed",
    "trigger": "Outstanding request or recurrence making this relevant; not invented new work.",
    "steps": [{
      "kind": "inspect",
      "action": "Concrete tool/command or file+symbol+predicate. Exact meaningful flags; placeholders/new commands labeled proposed, not historical.",
      "precondition": "State/dependency/current authorization before acting; protect unrelated work.",
      "expected": "Discriminating observation and next step if it holds.",
      "otherwise": "Meaning of mismatch and diagnostic branch or stop/ask condition, not blind continuation.",
      "refs": ["E000001"]
    }],
    "done_when": "Success at this route's actual commissioned scope. Specified local checks may suffice for a local task; do not require excluded later integration/release. Do not substitute a subset check for a broader explicitly requested outcome.",
    "stop_when": "When to pause or ask because state, scope, access or assumptions differ; preserve evidence.",
    "refs": ["E000001"]
  }],
  "recipes": [{
    "title": "Reusable mechanism", "basis": "proposed",
    "when": "Recognizable symptom/task and prerequisites; when inappropriate elsewhere.",
    "adapt": "Inputs to substitute/invariants to keep: repo entry, framework/version, asset names, units, environment; no blind machine paths/PIDs/commits.",
    "procedure": ["Ordered operation and discriminating check/branch: the efficient corrected route, not a replay of failed exploration."],
    "avoid": "Evidenced failure fingerprint -> why misleading/failed -> better alternative; distinguish unknown causes.",
    "verify": "How to validate adaptation and limitations of historical success.",
    "refs": ["E000001"]
  }],
  "trajectory": [{
    "id": "phase-one", "title": "Specific phase title", "human_refs": ["E000001"],
    "summary": "Trigger and change of direction, including user corrections; do not repeat tool results below.",
    "tool_steps": [{
      "purpose": "Key question or attempted change. Group repeated tool activity answering ONE question, not every invocation or unrelated operations.",
      "tool_refs": ["E000003", "E000004"],
      "usage": [{"tool": "ACTUAL_SOURCE_TOOL_NAME", "action": "Concrete target file/symbol/command and operation, grouped across routine calls; not merely 'inspect files'.", "refs": ["E000003", "E000004"]}],
      "finding": "What the action established, including decisive error/result and limits. A concise explanation, not raw logs; no automatic payload insertion will supplement it.",
      "decision": "How observation changed the next action or invalidated a route; attribute hypotheses, leave unknown causes unknown.",
      "refs": ["E000003", "E000004"]
    }],
    "rationale": {"basis": "recorded", "text": "Phase-level observable decision rationale, not repeated tool descriptions or private thought.", "refs": ["E000002"]},
    "tool_refs": ["E000003", "E000004"],
    "observation": "Net result at this phase's exact scope, separate from final acceptance.",
    "outcome": "partial",
    "next_state": "State handed to NEXT HISTORICAL phase; distinguish future recommendations at session end.",
    "refs": ["E000001", "E000002", "E000003", "E000004"]
  }],
  "paths": [{
    "title": "Attempted route", "outcome": "partial", "phase_ids": ["phase-one"],
    "reason": "Why worked/failed/abandoned/unresolved, replacement and evidence boundary. Index the history rather than narrate it again.",
    "reuse_condition": "Premise to recheck before reuse/retry; untested proposals are not successful routes.",
    "refs": ["E000003", "E000004"]
  }]
}

Use real refs. `outcome`: verified/partial/failed/abandoned/unresolved. `rationale.basis`: recorded/inferred/not_recorded; not_recorded has refs=[]. Continuation/recipe `basis`: explicit/proposed; explicit needs a human request for that future procedure, otherwise proposed. Step `kind`: inspect/verify/change/external. Tests/renders can have side effects: do not label them read-only. Current task authority may cover normal edits; do not ask permission again for every edit, but do not inherit historical permission for destructive/external operations or scope expansion.

Continuation/recipes may be empty when unsupported; do not fill generic advice. Prefer few specific routes over many hypothetical incidents. A recipe generalizes an evidenced mechanism with adaptation checks, not just renamed historical files. Separate environment repair from implementation. Visual symptoms need browser-level acceptance, not metadata or pixel samples alone.

Phases follow chronology and meaningful decisions, not a fixed count. Every human_input maps to human_refs, including short corrections/acknowledgements; explain material changes, not every turn. Select tool_steps for critical failures, diagnostic checks, modifications and validation that changed understanding or state. Use ACTUAL request AND result refs when available, in source order, with each selected tool event in one step only. Do not split a pair across steps. Never use human/assistant refs as tool_refs. A phase's tool_refs includes its step tool_refs. Routine calls stay in the evidence archive, not in tool_steps or a fallback tool dump. Empty tool_steps and tool_refs are valid for phases without a meaningful tool action.

Tool names and source refs can appear inline; raw arguments/results are not automatically rendered or executed. Full payloads remain in `_support/tool-ledger.json`, including missing/ambiguous results. In finding retain the meaningful observation, not a log excerpt by default. A rerun marker, empty output, result_recorded or tool success alone is not task completion. Preserve upstream truncation/missing-image limits. Never reconstruct hidden chain-of-thought.

Review by a receiving-agent exercise: without searching the archive, can a new agent name first action, predicted observation and fallback? Can it distinguish current state, history and conditional reuse, avoid the recorded mistake, and adapt the mechanism without copying irrelevant state? Completeness means usable state and decision coverage, not length. Preserve useful details and uncertainty when repairing.

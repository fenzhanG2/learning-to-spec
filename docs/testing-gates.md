# Test small before testing the App

Do not use a native App demo to debug schema contracts, CLI arguments, privacy transforms or rendering. A release candidate proceeds through the following gates in order. A failed gate blocks the next one; keep failed runs and fix the pipeline rather than editing their generated documents.

## 1. Offline contract and component tests

Run Python tests and the native Node workflow/bridge/output tests in CI. Provider-facing unit tests must use `tests/offline_provider.py`, including worker teardown, so a missed mock cannot spend quota or use a real profile. Distinguish mock model responses from actual provider results.

Cover invalid CLI model/options, complete draft and repair schemas, source-reference roles, quality-review failures, privacy choices and scoped replacements, exact approval hashes, download integrity, timeouts/process cleanup and safe error projections. An offline test pass establishes software behavior, not model output quality.

## 2. Real provider component gate, without the App

Use a hash-pinned public SWE-chat or wholly synthetic fixture. Give every attempt a fresh session UUID and isolated output/profile directory. Never discover or select unrelated local sessions. Run the production `NativeBridge` operations, not a parallel generation implementation:

1. Capture the fixture with explicit readers, local delivery and Smart-redaction selection.
2. Scan through actual Copilot CLI inference: joint abstraction, then rules and combined source-quality/privacy review. Keep the shared time and call limits enabled.
3. Require completed source-quality and privacy review before export. Bind explicit test decisions to the exact review and abstraction. Mark these as synthetic-fixture decisions, not real user consent.
4. Generate deterministically and verify Human HTML, Agent Markdown, separate evidence, ZIP and read-only download hashes. No additional inference occurs after approval.
5. Read both outputs against the source. Check original problem, decisions, failed paths, uncertainty, next action and technical preservation. Check injected privacy canaries in every selected output; lexical absence alone is not proof of anonymity.

Save the source/code/prompt hashes, exact command and runtime capability metadata, stage transitions, raw model candidates, actual provider usage, elapsed time, failures and output hashes. Report process launches separately from actual model requests, and preparation separately from approval time and export. Use provider-reported tokens; do not estimate them from text length.

For a current candidate, run fresh fixtures through at least both-reader Smart mode and the no-redaction branch before claiming full mode coverage. Exercise the production default without a model override; an explicitly pinned diagnostic model is a separate experiment, not a substitute for the default gate. Record the requested and provider-resolved models separately and test unavailable overrides without fallback. One passed fixture is a smoke test, not a reliability or quality benchmark. An invalid CLI argument or schema is a component failure, not an App acceptance result.

## 3. Native host acceptance

Only after the relevant offline and real-provider component gates pass, install the exact tested candidate and use a fresh fixture conversation for one bounded native acceptance run. Check `/to-spec`, current-session binding, displayed defaults, privacy decisions, approval, progress/error handling and usable selected downloads. Leave security/trust prompts to the user.

Record host-side routing and latency separately from the backend's model calls. Passing the headless component gate does not establish App startup, UI, host permissions or installation behavior. Conversely, a successful install or command-discovery probe is not a completed export.

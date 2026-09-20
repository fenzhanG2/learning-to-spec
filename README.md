# Learning to Spec

Turn a GitHub Copilot session into a readable engineering story, an actionable Agent handoff, or both — with explicit privacy and delivery choices.

## Choose before generating

The plugin asks you to choose, rather than treating defaults as permission:

1. **Reader:** Human HTML, Agent Markdown, or both. Agent output has a separate `evidence.md`; there is no Agent HTML viewer.
2. **Delivery:** local files / one downloadable ZIP, or ArtifactStore. ArtifactStore additionally requires a root or team audience and a final upload confirmation.
3. **Privacy review:** local rules or an optional contextual Copilot review. You choose keep, remove, pseudonymize or generalize for each finding, including potentially uncomfortable asides.
4. **Final confirmation:** inspect the chosen transformations before generation and the exact selected files before any upload.

Recognized credentials, hidden reasoning and binary payloads are always excluded. Contextual findings never receive a bulk deletion recommendation. Technical failures, corrections and acceptance boundaries are not embarrassing details to erase.

## Install in Copilot App or CLI

Requirements: Python 3.10+, Node.js 18+, and an authenticated GitHub Copilot CLI. Rendering dependencies are bundled: installing a published plugin does not require npm or a manual clone. ArtifactStore is an optional Microsoft-tenant service and additionally needs an authorized Azure CLI sign-in; local export works without it.

A standalone public repository can be installed directly, without playground access:

```text
copilot plugin install OWNER/learning-to-spec
```

In Copilot App, open **Customize → Installed** to confirm the plugin is enabled, then start a fresh session. For custom sources, use **Add plugin** with the published repository. Enterprise policies may restrict public plugins; this is not a way to bypass an organization's policy. Python, Node and Copilot authentication remain prerequisites, not dependencies silently installed by the plugin.

Once the plugin is merged into the marketplace:

```text
copilot plugin marketplace add agency-microsoft/playground
copilot plugin install learning-to-spec@agency-playground
```

For a local checkout with the bundled renderer:

```text
copilot --plugin-dir "/absolute/path/to/plugins/learning-to-spec"
```

Ask Copilot: “Use learning-to-spec to export this session; ask me which readers, privacy transformations and delivery I want.”

Check prerequisites with `python scripts/doctor.py`. Copilot App and CLI share the plugin/skill format. Restart your Copilot session after an update. Native App plugin recognition and the explicit-choice gate have been tested separately from CLI generation; this is not a claim that every App version or policy configuration behaves identically.

## Private review Studio

```text
python scripts/session_spec.py studio --session "SESSION_UUID_OR_EVENTS_JSONL"
```

Open the private localhost URL printed by the command. The four-stage Studio guides reader/delivery selection, disclosure review, selected-file downloads and optional publication. Keep the process running; launch background processes with hidden windows on Windows. Do not share the URL's access fragment.

“Local” means no ArtifactStore publication, not offline model inference. Local privacy rules make no model calls; optional contextual review sends pre-masked context to Copilot with consent. Generation sends the approved reduced session to Copilot and uses your quota.

## Terminal workflow

After the user explicitly chooses a Human-only local file:

```text
python scripts/session_spec.py privacy-scan SESSION --out PRIVATE_REVIEW --readers human --delivery local --audience local
python scripts/session_spec.py privacy-choose PRIVATE_REVIEW --out DECISIONS_JSON
python scripts/session_spec.py private-story PRIVATE_REVIEW --decisions DECISIONS_JSON --out PRIVATE_OUTPUT --confirm-choices
python scripts/session_spec.py validate-story PRIVATE_OUTPUT/story
```

Use `--readers agent` or `--readers both` only for that user choice. Add `--allow-copilot-review` to scanning only after consent. `--redact "exact phrase"` flags an additional concern. `privacy-choose` prompts for each action; `--recommended` is for explicitly accepted available suggestions, not authorization to decide unresolved personal findings.

The delivered folder is `PRIVATE_OUTPUT/deliverables/`. `PRIVATE_OUTPUT/deliverables.zip` contains exactly those selected files. A joint working draft and validation evidence remain in the private `story/` workspace to preserve consistent source-grounded review; do not share the entire workspace. CLI result paths and Studio downloads point only to selected deliverables.

Reader, audience and delivery choices are bound to the privacy review. Changing them requires a new review. `private-story --resume` requires unchanged source and choices, plus renewed explicit confirmation. Old reviews lacking reader/delivery choices must be scanned again for the new workflow.

## Optional ArtifactStore publication

Choose `--delivery artifactstore` and `--audience root` or `--audience team:SLUG` when scanning. After inspecting the selected output:

```text
python scripts/session_spec.py share-package PRIVATE_OUTPUT/story --out PRIVATE_PACKAGE --audience root --readers human
python scripts/session_spec.py share-approve PRIVATE_PACKAGE --package-id CURRENT_ID --acknowledge FINDING_IDS --reviewed-all-files
python scripts/session_spec.py artifact-plan PRIVATE_PACKAGE --site NEW_NAME --out UPLOAD_PLAN
python scripts/session_spec.py artifact-publish PRIVATE_PACKAGE --plan UPLOAD_PLAN --confirm USER_APPROVED_PLAN_ID
```

Do not acknowledge files/findings or confirm an upload for the user. Root publication verifies owner-only access before uploading session content. Teams inherit their displayed, rechecked viewer policy. Existing names are refused; uncertain writes are not automatically retried. Agent-only packages contain Markdown only, including the staging placeholder. Upload uses an in-memory Azure CLI token, never browser cookies.

The package allowlist is the selected subset of `index.html`, `agent-spec.md` and `evidence.md`. Raw sessions, reversal maps, privacy reviews, caches and `_support/` never enter the package. Final approval is bound to exact file hashes.

ArtifactStore decorates HTML when serving it. Readback checks every authored byte after recognizing only its specific security/favicon/editor additions; Markdown remains byte-for-byte checked. Unknown changes fail closed. Verification records the actual response hashes and does not claim to attest the underlying storage blob.

If an upload stopped after creating remote content, inspect the receipt and recheck without another upload:

```text
python scripts/session_spec.py artifact-verify PRIVATE_PACKAGE --plan UPLOAD_PLAN
```

This command performs read-only remote requests and rechecks file names, content and sharing policy. It never overwrites, deletes, retries an upload or broadens access.

## Pipeline and boundaries

Read-only snapshot → local hard-secret removal → optional contextual suggestions → exact-span user choices → reduced Copilot events → shared draft → source-grounded editorial review/repairs → selected deliverables → optional final-file approval and upload.

Human stories explain the original problem, meaningful goals/boundaries, causal trajectory, implementation architecture when supported, outcomes and transferable lessons. Agent handoffs prioritize the current state, first useful action, conditional continuation, tools used inline, successful/failed paths and verification boundaries. Sources resolve to the separate evidence file. Neither output reconstructs hidden reasoning or replays historical commands.

Language naturally follows the source conversation unless the user explicitly requests translation. Empty non-goals or architecture sections are omitted, not fabricated. Structural consistency and model review are not correctness proofs.

See [output contract](docs/output-contract.md), [privacy threat model and research](docs/privacy-design.md) and [source/license notices](third_party/NOTICES.md). Reference projects are not runtime dependencies.

## Compatibility

This project was previously named Copilot Session Spec. New local state defaults to `~/.learning-to-spec/`; configuration falls back to `~/.copilot-session-spec/config.json` when the new config is absent. Existing sessions and old outputs are not moved. Historical schema IDs such as `copilot-session-spec/v1` remain stable for compatibility. The internal Python package/entry script remains `session_spec` / `scripts/session_spec.py`.

Direct `story`, `export`, `refresh-story` and `validate` commands remain for explicitly requested legacy workflows; they do not establish privacy approval or permission to upload.

## Development

```text
npm ci --ignore-scripts
npm run build
python -m unittest discover -s tests -v
node --check session_spec/web/studio.js
npm audit --omit=dev --audit-level=moderate
```

Real Copilot generation tests consume quota and belong outside shared folders. Use synthetic privacy fixtures and never commit actual sessions, model responses, review choices or credentials. Research experiments and SWE-chat datasets are not part of this plugin.

Commit the generated renderer bundle, its manifest and its license companion after changing renderer sources or the dependency lockfile. Runtime checks reject stale/tampered bundles. The build tool is development-only; no reference project is needed to build or run this plugin.

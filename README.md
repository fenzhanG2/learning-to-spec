# Learning to Spec

Turn a GitHub Copilot session into a readable engineering story, an actionable Agent handoff, or both — with explicit privacy and delivery choices.

**A native workflow plugin, not only a skill:** 12 typed MCP tools, persistent local jobs, a private approval UI, source-grounded generation/review, selected-file delivery and verified ArtifactStore publishing. The skill only guides conversation. [Architecture and recovery](docs/native-runtime.md).

## Choose before generating

The plugin asks you to choose, rather than treating defaults as permission:

1. **Reader:** Human HTML, Agent Markdown, or both. Agent output has a separate `evidence.md`; there is no Agent HTML viewer.
2. **Delivery:** local files / one downloadable ZIP, or ArtifactStore. ArtifactStore additionally requires a root or team audience and a final upload confirmation.
3. **Privacy review:** local rules or an optional contextual Copilot review. You choose keep, remove, pseudonymize or generalize for each finding, including potentially uncomfortable asides.
4. **Final confirmation:** inspect the chosen transformations before generation and the exact selected files before any upload.

Recognized credentials, hidden reasoning and binary payloads are always excluded. Contextual findings never receive a bulk deletion recommendation. Technical failures, corrections and acceptance boundaries are not embarrassing details to erase.

## Install in Copilot App or CLI

Requirements: Python 3.10+, Node.js 18+, and an authenticated GitHub Copilot CLI. Rendering dependencies are bundled: installing a published plugin does not require npm or a manual clone. ArtifactStore is an optional Microsoft-tenant service and additionally needs an authorized Azure CLI sign-in; local export works without it.

### 1. Install

This public repository does not require playground or Microsoft organization access. Register its marketplace once, then install:

```text
copilot plugin marketplace add fenzhanG2/learning-to-spec
copilot plugin install learning-to-spec@learning-to-spec
copilot plugin list
```

Copilot currently also accepts `copilot plugin install fenzhanG2/learning-to-spec`, but warns that direct repository installs are deprecated. Prefer the marketplace commands above. Do not install both variants.

In Copilot App, open **Customize → Installed** to confirm the plugin is enabled, then start a fresh session. If it is missing, verify that App and CLI use the same local profile. Enterprise policies may restrict public plugins; this is not a way to bypass an organization's policy. Python, Node and Copilot authentication remain prerequisites, not dependencies silently installed by the plugin. See [GitHub's CLI installation guide](https://docs.github.com/en/copilot/how-tos/copilot-cli/cli-getting-started) if `copilot` is not found.

### 2. Export a session

Start a new Copilot conversation in **Interactive** mode and ask:

```text
Use learning-to-spec to export my session about [topic].
Ask me which readers, privacy transformations and delivery I want.
```

You can supply a session UUID or an absolute `events.jsonl` path instead of a topic. The plugin lists local candidates if needed; it must not guess which session you meant. It then asks for Human / Agent / Both, local files / ArtifactStore, and local / contextual privacy detection. Inspect individual findings and confirm the changes before generation. Choosing local files does not disable Copilot model calls or quota usage.

The native `open_studio` tool opens a private browser without returning its capability to the model. The Studio has three steps: **Choose → Protect → Use & share**. Choose readers and delivery, decide what to do with flagged details, then click **Generate spec**. No duplicate save action or “I reviewed” checkbox. Advanced writing/contextual-review settings are folded away; local detection is the default. Contextual Copilot review still requires an explicit selection explaining what leaves the device. Keep the interactive Copilot host session open during work: hosts may terminate plugin processes on exit. Saved jobs/checkpoints survive restarts; `list_jobs` and Recent work reopen them without automatic replay. Download only selected files/ZIP: `human-spec.html`, `agent-spec.md`, `evidence.md`. Keep Markdown files together so citations resolve. Never share the private workspace or Studio capability.

For ArtifactStore, **Upload to ArtifactStore** prepares the selected files and checks the destination without writing remotely or granting approval. The final **Upload** action explicitly approves the current bytes, individually accepted residual disclosures and displayed audience. Approval records the publish action, not an invented claim that the user read every file. Changed bytes or permissions invalidate the plan. There is no automatic upload and no repeat-write button after an upload attempt.

### 3. Update or troubleshoot

```text
copilot plugin update learning-to-spec@learning-to-spec
```

Restart your Copilot session after installing or updating. If CLI lists the plugin but the running App still reports `Skill not found`, do not assume it loaded: retry after restarting the App when other active work can safely stop. A new conversation or switching Customize tabs may not refresh the App's plugin registry. Autopilot mode may report that the user is unavailable to answer; use Interactive mode for native questions, or the Studio's explicit-choice UI.

Ask Copilot to call `health`. A visible skill without native tools is not a successful full-plugin installation. If prerequisites fail, bundled `scripts/doctor.py` checks Python, Node, CLI and renderer integrity without model calls. Authentication is checked during generation. No npm/manual clone is required. Use `python3` if that is your installed Python command.

Copilot App and CLI share the plugin/skill format. Native App plugin recognition and the explicit-choice gate have been tested separately from CLI generation; this is not a claim that every App version or policy configuration behaves identically. ArtifactStore requires separate, authorized Microsoft Azure CLI access; a personal GitHub account alone does not grant it. Local export does not need Azure.

For a development checkout only, use `copilot --plugin-dir "/absolute/path/to/learning-to-spec"`. The public repository is independent of the proposed playground submission; there is no requirement to add the private playground marketplace.

## Private review Studio

```text
python scripts/session_spec.py studio --session "SESSION_UUID_OR_EVENTS_JSONL"
```

Open the private localhost URL printed by the command. The three-step Studio guides reader/delivery selection, disclosure review, selected-file downloads and optional publication. Keep the process running; launch background processes with hidden windows on Windows. Do not share the URL's access fragment.

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

If App can open Studio but generation reports authentication failure, first check the configured GitHub host rather than repeatedly signing in. The App account and the generation CLI's selected host can differ, including an inherited legacy `gh_host`. To deliberately use an existing github.com `gh` login, set `"gh_host": "github.com"` in `~/.learning-to-spec/config.json` (preserve any other settings), or pass `--gh-host github.com` to the CLI. For Enterprise, choose that authorized host instead. No tokens belong in this file. Finish active jobs and use the native `shutdown` tool before restarting the runtime so it reads the new selection. Reopen the same job and explicitly retry; privacy decisions and failed attempts remain. Studio keeps full local diagnostics under **Technical details**; authentication is never retried or changed automatically.

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

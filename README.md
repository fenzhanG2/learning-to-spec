# Learning to Spec

Turn a GitHub Copilot session into a readable engineering story, an actionable Agent handoff, or both — with explicit privacy and delivery choices.

**A native session extension:** the plugin registers `/to-spec` and a no-argument `learning_to_spec` tool, not a Skill or prompt template. The extension binds to the current Copilot session, uses native forms in both App and CLI, and runs source-grounded generation, privacy review and delivery. App versions that route slash text through the model can invoke the native tool instead; command interception is host-dependent. A read-only App canvas shows progress, then the selected downloads. No session path, copied extension code, or external browser is required. [Native extension architecture](docs/native-extension.md).

## Choose before generating

The plugin asks you to choose, rather than treating defaults as permission:

1. **Reader:** Human HTML, Agent Markdown, or both. Agent output has a separate `evidence.md`; there is no Agent HTML viewer.
2. **Delivery:** local files / one downloadable ZIP, or ArtifactStore. ArtifactStore additionally requires a root or team audience and a final upload confirmation.
3. **Privacy:** **No redaction**, or **Smart redaction (rules + Copilot)**. The defaults are Both, local files and Smart redaction; you must explicitly accept them. Both modes first abstract the session into a private draft. Smart mode then combines deterministic rules with contextual LLM review; incomplete review blocks approved delivery and upload rather than falling back to rules alone.
4. **Final confirmation:** accept or customize the proposed privacy plan, then export the saved draft. Any upload needs a separate confirmation of the exact selected files and audience.

No redaction can leave personal details, internal information and credentials in local output; it generates a spec, not a verbatim transcript. Smart redaction removes recognized credentials and proposes one bounded plan for contextual findings, including uncomfortable asides. Accept it in one step or customize scope-specific keep/remove/pseudonymize/generalize choices; findings without safe bound defaults still need individual choices. Technical failures, corrections and acceptance boundaries are not embarrassing details to erase. Hidden reasoning, control/UI data and binary payloads remain outside the observable source in both modes. ArtifactStore still applies separate final-file secret checks and explicit upload confirmation, even with No redaction.

**A useful draft instead of nothing:** if a bounded draft repair fails, or source-quality review finds a material defect, the native workflow saves the recoverable Human HTML and/or Agent Markdown as an **unvalidated private draft**. The panel explains the failure and opens the files or their folder; CLI also prints the folder. Facts and citations may be wrong, and privacy review/redaction is incomplete. This fallback makes no extra model calls, never gains approval or upload authority, and is not counted as a successful quality result. Check it against the original session before reuse or sharing. Missing provider output, authentication failures and unsafe filesystem state cannot produce a recovered draft.

## Install in Copilot App or CLI

Requirements: a Copilot version supporting plugin-shipped extensions and native canvases or elicitation, Python 3.10+, Node.js 18+, and authenticated Copilot CLI generation. Extension APIs are currently experimental upstream; a host or organization policy that disables them cannot be bypassed by installing this plugin. Rendering dependencies are bundled: no npm, source edits, extension scaffolding or manual clone is needed. ArtifactStore additionally needs authorized Azure CLI access; local export works without it.

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

Stay in the conversation you want to export, in **Interactive** mode, and enter:

```text
/to-spec
```

Or ask **“export spec”** / **“导出 spec”**: Copilot can call the extension's no-argument `learning_to_spec` tool. The SDK supplies the current-session identity and observable events; no most-recent-session guess, directory scan or user-supplied path is used. A frozen snapshot prevents later export interactions from changing the reviewed input. Hidden/control data is excluded. Both modes retain observable source content privately for abstraction; Smart redaction runs after drafting, not before capture. Snapshots and intermediate drafts can contain sensitive values and must remain private.

Both **Copilot App and CLI** use native forms: choose readers, delivery and one of the two privacy modes, review any flagged details, then generate. These privacy choices determine what may enter the exported files, not whether the conversation already exists in Copilot. Private findings go to the user interface, not the model's tool result. Smart review and spec generation use Copilot quota, even with local delivery. Nothing uploads automatically. Keep the host conversation active while work runs. Finished App exports open in a native side panel; it has no generation, privacy-approval or upload authority. Open the selected HTML/Markdown reports or their output folder, or download a ZIP copy; keep the Markdown files together.

Automatic processing has a **five-minute limit**, excluding time spent answering forms. The usual path makes two backend model calls, with at most one repair call. The plugin respects the host's active model when available; it does not require a fixed named model. The App panel updates every three seconds with the current stage and elapsed processing time, and tells you when it is waiting for your choices. Timeouts stop the attempt with a clear status rather than restarting silently. Longer timeout does not mean faster generation or guarantee completion.

The finished App panel shows the saved output folder and offers **Open Human story**, **Open Agent handoff**, **Open Evidence**, **Open output folder** and **Copy folder path**. Reports open the existing HTML/Markdown in this computer's default application, not a silent duplicate download. Each open action revalidates the approved selected-output snapshot. Markdown without a Windows file association falls back to Notepad. No model is called and no report is regenerated. Local paths are shown only in the authenticated user panel, not in model-facing tool results.

**Download ZIP copy** is secondary: Copilot/browser controls its download location, which is distinct from the displayed original output folder. The panel checks SHA-256 before enabling report cards or ZIP download, with a shared 30-second fetch deadline, an 8 MiB per-file limit and a 24 MiB aggregate payload limit; browser overhead is additional. A verified ZIP remains cached until the panel closes or reloads. Opening needs the local runtime; if disconnected, the saved path and cached ZIP provide recovery. An open response means the OS accepted the launch request, not proof that a window appeared; a download request is not proof that the host saved a file. **About this export** explains these boundaries. The panel cannot edit, generate, approve privacy choices or upload.

The output panel is a compact launcher, not an embedded report reader: there is no “A look inside” section. Open Human story launches the complete saved HTML; Agent and Evidence remain Markdown files. Report content is never injected into the panel DOM, and opening does not alter the selected files or their hashes.

New Agent exports keep complete selected sanitized payloads in the separate Evidence file, including uniquely paired tool requests/results; the Agent itself stays a trajectory, not a raw tool log. This does not recover upstream truncation or redactions. The fresh-reader check rejects a rendered pair above 240,000 characters instead of silently dropping evidence or approving an unreviewed pair. Older exports retain their recorded rendering policy.

For ArtifactStore, native forms collect the audience and propose an anonymous unique URL name, then show the exact selected files and remaining flagged details in one final upload confirmation. Preparing a package or checking the destination does not authorize an upload. Approval records the publish action, not an invented claim that the user read every file. Changed bytes or permissions invalidate the plan. There is no automatic upload; the read-only output panel has no upload control.

### 3. Update or troubleshoot

```text
copilot plugin update learning-to-spec@learning-to-spec
```

Restart your Copilot session after installation or update. Confirm `/to-spec` appears in command completion and the extension is running in the host's extension manager. Merely seeing a skill is not success. Existing App processes may need a restart; preserve other active work. Use Interactive mode for human privacy decisions. Unsupported hosts receive an explicit compatibility error, not a silent browser fallback.

Before updating or uninstalling, finish jobs and close the relevant Copilot session. Runtime state stays outside the installation. If Windows reports a file in use, close the App when safe and retry the official plugin command; do not change ACLs or force-remove an active installation. Hosts can still end idle plugin processes.

If prerequisites fail, bundled `scripts/doctor.py` checks Python, Node, CLI and renderer integrity without model calls. Authentication is checked during generation. No npm or manual clone is required. [Legacy MCP and manual Studio](docs/legacy/README.md) remain explicit integrations, not the native Copilot entry point. The Codex manifest is metadata only: it registers no tools or skills and does not provide `/to-spec` in Codex.

Copilot App and CLI load the extension from the installed plugin; neither needs separately installed user extension files. ArtifactStore requires separate authorized Microsoft Azure access. Compatibility and observed native E2E results must be checked for the host version; historical MCP/Studio tests are not proof of this new extension path.

For a development checkout only, use `copilot --plugin-dir "/absolute/path/to/learning-to-spec"`. The public repository is independent of the proposed playground submission; there is no requirement to add the private playground marketplace.

## Legacy manual Studio

This browser workflow is separate from the 0.7 native extension. Its session picker, saved-job controls and **Local files & download help** are not controls in the native output panel. Using Studio is optional and explicit, not a prerequisite for `/to-spec`.

```text
python scripts/session_spec.py studio --session "SESSION_UUID_OR_EVENTS_JSONL"
```

Open the private localhost URL printed by the command. The three-step Studio guides reader/delivery selection, disclosure review, selected-file downloads and optional publication. Keep the process running; launch background processes with hidden windows on Windows. Do not share the URL's access fragment.

“Local” means no ArtifactStore publication, not offline model inference. Local privacy rules make no model calls; optional contextual review sends pre-masked context to Copilot with consent. Generation sends the approved reduced session to Copilot and uses your quota.

## Explicit terminal workflow

After the user explicitly chooses a Human-only local file:

```text
python scripts/session_spec.py privacy-scan SESSION --out PRIVATE_REVIEW --readers human --delivery local --audience local --allow-copilot-review
python scripts/session_spec.py privacy-choose PRIVATE_REVIEW --out DECISIONS_JSON
python scripts/session_spec.py private-story PRIVATE_REVIEW --decisions DECISIONS_JSON --out PRIVATE_OUTPUT --confirm-choices
python scripts/session_spec.py validate-story PRIVATE_OUTPUT/story
```

Use `--readers agent` or `--readers both` only for that user choice. `--allow-copilot-review` explicitly permits private abstraction of the observable session followed by rules-plus-Copilot review of the draft; use it only after consent. It is not a local-only scan. `--redact "exact phrase"` flags an additional concern. `privacy-choose` prompts for each action; `--recommended` is for explicitly accepted available suggestions, not authorization to decide unresolved personal findings.

The delivered folder is `PRIVATE_OUTPUT/deliverables/`. `PRIVATE_OUTPUT/deliverables.zip` contains exactly those selected files. A joint working draft and validation evidence remain in the private `story/` workspace to preserve consistent source-grounded review; do not share the entire workspace. CLI result paths and Studio downloads point only to selected deliverables.

Generation includes a fresh receiving-reader probe of the rendered Agent/Evidence pair, followed by full-source adjudication of its findings across both editions. The probe sees no original transcript or prior feedback; mistaken or out-of-scope suggestions are rejected, not automatically applied. Before publication, the final Markdown bytes must match the latest reader probe. Repairs that change those bytes trigger a new reader check; new findings receive source-grounded adjudication before any further edit. Unchanged pairs reuse the same check. All calls count toward the existing budget; exhaustion blocks publication rather than approving an unreviewed final repair. This improves review coverage, but is neither actual task execution nor a guarantee of factual correctness. Private receipts retain every probe, disposition and measured cost; none of that audit machinery is added to the readable story.

New final-reader receipts bind UTF-8 Markdown bytes before publication and preserve the exact editorial contract for later source-grounding validation. Matching resumes retain known reader concerns and consumed repair attempts; a cleaner later probe cannot erase an unresolved concern. Failed reader attempts use separate immutable locations. Missing or mismatched recovery evidence blocks resume instead of silently resetting review. Legacy receipts remain readable but do not acquire these newer guarantees retroactively. Local hashes check consistency, not signed provenance or semantic truth.

`--resume` continues the same review identity; changing the model, review contract or editorial feedback requires a new output directory. For source-matched editorial revisions, `story --revise-from PREVIOUS_OUTPUT --out NEW_OUTPUT` reuses the prior candidate as starting material and reviews it afresh, without modifying the previous deliverables or resetting their audit trail.

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

Current-session snapshot → explicit privacy mode → either unchanged observable content, or local rules + required contextual LLM review + exact-span user choices → approved Copilot events → shared draft → source-grounded editorial review/repairs → selected deliverables → optional final-file approval and upload. The advanced legacy Studio/terminal interfaces retain their separately documented manual controls; they are not the native two-choice form.

Human stories explain the original problem, meaningful goals/boundaries, causal trajectory, implementation architecture when supported, outcomes and transferable lessons. Agent handoffs prioritize the current state, first useful action, conditional continuation, tools used inline, successful/failed paths and verification boundaries. Sources resolve to the separate evidence file. Neither output reconstructs hidden reasoning or replays historical commands.

Language naturally follows the source conversation unless the user explicitly requests translation. Empty non-goals or architecture sections are omitted, not fabricated. Structural consistency and model review are not correctness proofs.

Each retained Agent rationale receives a separate, source-local review disposition in the existing editorial call: explicit reason, inference, action/result only, retrospective explanation or unsupported. Literal quotations, source roles, declared references and later-phase boundaries are checked before accepting it. A recorded failure or successful edit is not itself evidence of a speaker's motive. These internal checks neither expose hidden reasoning nor add a quote catalogue to the handoff; semantic entailment still requires model judgment and is not guaranteed.

Phase citations are not exclusive time ranges: one message can explain a decision and introduce the next phase. Potentially later sources require an explicit temporal adjudication, not an automatic backdating verdict or duplicated aggregate citations. This judgment is still semantic, not a chronology proof.

Failed source-quote checks report the observed payload channel and a bounded literal navigation hint. An assistant-authored tool argument still uses the tool channel; rendered Markdown is not raw source text. Diagnostics never silently rewrite a quote, change its source role or accept the finding. The existing bounded review retry must still return valid evidence, and literal evidence does not establish semantic correctness.

See [output contract](docs/output-contract.md), [privacy threat model and research](docs/privacy-design.md) and [source/license notices](third_party/NOTICES.md). Reference projects are not runtime dependencies.

## Compatibility

This project was previously named Copilot Session Spec. New local state defaults to `~/.learning-to-spec/`; configuration falls back to `~/.copilot-session-spec/config.json` when the new config is absent. Existing sessions and old outputs are not moved. Historical schema IDs such as `copilot-session-spec/v1` remain stable for compatibility. The internal Python package/entry script remains `session_spec` / `scripts/session_spec.py`.

If native forms work but generation reports authentication failure, first check the configured GitHub host rather than repeatedly signing in. The App account and the generation CLI's selected host can differ, including an inherited legacy `gh_host`. To deliberately use an existing github.com `gh` login, set `"gh_host": "github.com"` in `~/.learning-to-spec/config.json` (preserve any other settings), or pass `--gh-host github.com` to the explicit terminal workflow. For Enterprise, choose that authorized host instead. No tokens belong in this file. Finish active jobs before restarting the relevant Copilot session so the extension reads the new selection. Native `/to-spec` has no `shutdown` tool or Recent work/session-picker UI; do not invoke historical MCP tools as native recovery steps. Private receipts retain failed attempts. Legacy Studio recovery and its **Technical details** panel are documented separately in [the legacy runtime guide](docs/native-runtime.md). Authentication is never retried or changed automatically.

Direct `story`, `export`, `refresh-story` and `validate` commands remain for explicitly requested legacy workflows; they do not establish privacy approval or permission to upload.

## Development

Follow the [ordered testing gates](docs/testing-gates.md): offline contracts first, then actual Copilot CLI generation/privacy/export on an isolated public or synthetic fixture, and only then native App acceptance. Do not use an App demo to discover CLI argument, schema or rendering failures. Mock passes are not real-provider or semantic-quality passes.

```text
npm ci --ignore-scripts
npm run build
python -m unittest discover -s tests -v
node --check session_spec/web/studio.js
node --test tests/native_extension.mjs tests/native_output.cjs tests/native_bridge_integration.mjs
npm audit --omit=dev --audit-level=moderate
```

Real Copilot generation tests consume quota and belong outside shared folders. Use synthetic privacy fixtures and never commit actual sessions, model responses, review choices or credentials. Research experiments and SWE-chat datasets are not part of this plugin.

Commit the generated renderer bundle, its manifest and its license companion after changing renderer sources or the dependency lockfile. Runtime checks reject stale/tampered bundles. The build tool is development-only; no reference project is needed to build or run this plugin.

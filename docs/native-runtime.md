# Legacy MCP / Studio runtime

This guide describes the explicit pre-0.7 MCP integration, not the current native Copilot extension. For install-only `/to-spec`, native privacy forms and the read-only output canvas, see [the native extension](native-extension.md). Legacy configurations and the historical skill are archived under [docs/legacy](legacy/README.md); they are not auto-discovered by the current package.

```text
Copilot App / CLI
  ├─ thin interaction skill (questions and handoff)
  └─ plugin-discovered stdio MCP (12 typed tools)
       └─ capability-authenticated loopback service
            ├─ durable jobs + transition history + single-writer lease
            ├─ private approval Studio (findings never in tool responses)
            ├─ local scan → optional consented contextual review
            ├─ approved reduced source → draft → bounded repair/review
            ├─ Human HTML / Agent MD + Evidence MD → allowlisted ZIP
            └─ final-byte approval → live destination plan → upload → readback
```

## Explicit legacy integration

The archived example `docs/legacy/copilot-mcp.json` follows [Agent Plugins 1.0](https://agent-plugins.org/schemas/1.0.0/mcp.schema.json); `docs/legacy/claude-mcp.json` retains the previous compatibility configuration. These examples are deliberately outside automatic discovery paths and are not registered by the metadata-only Codex manifest. Copilot uses the `/to-spec` extension, not these legacy tools or the legacy skill. An explicit integration must supply the appropriate plugin-root expansion for its host. The Node launcher selects installed Python ≥3.10 without downloading dependencies. MCP is a protocol adapter, not a shell-command prompt; generation logs never enter its stdout.

The background service starts lazily. `~/.learning-to-spec/runtime/<profile-hash>` stores private state outside the installation directory, scoped to the resolved Copilot home so different profiles cannot share jobs, inherited authentication or approvals. `LEARNING_TO_SPEC_HOME` explicitly overrides this root for tests. Existing local config supplies optional model, GitHub host and call budget. OS file leases serialize startup and prevent competing writers. The service binds only `127.0.0.1`, checks Host/Origin/capability, refuses cross-origin requests and never follows redirects with its capability.

Starting the native service or checking health does not enumerate Copilot sessions. Exact-session review/open and saved-job recovery do not implicitly populate the session picker. Session discovery occurs only through an explicit `list_sessions` call or `open_studio` without a session/job (opening the picker); a manual Studio with no exact source also opens that picker. Reading one exact session queries only its matching metadata row, not every title in the local database. This limits metadata access as well as transcript access; it is not an OS sandbox or an encryption guarantee.

| Tool | Implemented behavior |
| --- | --- |
| `health` | Protocol/version and active-operation count; no model call |
| `list_sessions` | IDs, sizes and dates; private titles remain in Studio |
| `list_jobs` / `get_job` | Persisted stages, history and safe counts |
| `open_studio` | Open browser at a source/job; capability withheld from model |
| `start_review` | Validate explicit choices and start actual privacy scan |
| `generate` | Consume user-saved decisions, validate hashes, run engine asynchronously |
| `deliverables` | Verify selected files/ZIP and return paths, sizes, SHA-256 |
| `read_deliverable` | Bounded read of selected approved documents only |
| `prepare_publish` | Build allowlisted package, expose counts only; no upload |
| `verify_publish` | Read existing artifact back, verify audience/content; no remote write |
| `shutdown` | Stop idle service, retain state, never stop Copilot |

There is no MCP approval shortcut, arbitrary filesystem read, shell execution or raw-review tool. Individual disclosure approval and final upload confirmation happen in the human-controlled Studio. Having an Agent make privacy choices would defeat the product. The rest is code-driven E2E, not instructions asking a model to join scripts together.

Studio uses progressive disclosure: Choose, Protect, Use & share. The single **Generate spec** action saves explicit per-finding choices and starts work; there is no separate save/allow button or review-attestation checkbox. Optional contextual review is a clearly labeled opt-in selection, with its cloud-disclosure notice next to the action. Upload preparation and destination checks are read-only previews, not approval. The final Upload click approves the exact package and displayed plan; residual findings still require individual choices. The receipt distinguishes a publish-action approval from the legacy CLI reviewed-all-files attestation. Planning never authorizes publication.

## Recovery

`job.json` is atomically replaced at operation start/completion and after synchronous approvals. It contains private decisions and paths and must never be published. Stage/status/time history contains no session text. Source, baseline, review, decisions, delivery, package and remote plan have separate hashes.

Interrupted operations restore as errors, never automatic replay. An unchanged failed generation can reuse its checkpoint after explicit retry; completed generation is idempotent. Changed privacy choices invalidate package/plan and block old delivery until regeneration. Source mutation invalidates review. An existing upload receipt blocks repeat publication; use read-only verification instead. ArtifactStore publication is not atomic: a placeholder or unverified upload may remain after failure.

The service is separate from individual MCP requests, but not guaranteed to outlive host process-tree cleanup. Actual Copilot CLI testing showed that exiting the host terminates plugin descendants on Windows. Native App testing also observed the listener disappear after an approximately ten-minute idle session shutdown while the App window remained open; an open window alone is not a lifetime guarantee. `start_review`, `generate` and `verify_publish` wait for their stage to finish, preventing batch CLI exit mid-operation. Saved jobs survive a host restart; interrupted work resumes explicitly. `shutdown` refuses while work runs. Version mismatch requires idle shutdown. Manual foreground `studio` requires its own process to stay alive. Operations are serialized; this is not a distributed queue or an installed OS daemon.

If Studio loses its connection, return to Copilot and ask it to reopen the exact learning-to-spec job shown in the error. The native `get_job` and `open_studio` tools restart the local runtime when needed and restore its saved state; do not create a new review. Use the newly opened page, not the old port or access fragment. Unsaved choices may need selecting again. A failed network response does not prove generation or upload failed: check saved status, explicitly resume interrupted generation, and verify any attempted publication instead of repeating its write. Studio blocks server-dependent actions on the disconnected page and never restarts or replays them automatically; the cached-download exception below is local only. For a longer review independent of host idle cleanup, use the documented manual foreground Studio fallback. The plugin does not change host timeout or security settings.

When an approved completed output is shown or reopened, Studio automatically stages only its selected exported documents and selected-deliverables ZIP in ephemeral browser-memory Blobs. It never stages raw privacy reviews or source sessions. Fetches are sequential, stop after a shared 15-second deadline, and enforce an 8 MiB per-file / 24 MiB aggregate payload bound while reading the response stream (browser allocation overhead is additional). At most the three allowlisted documents and one ZIP are eligible. Unrecognized reader/file selections are not fetched; their ZIP is not cached. No local storage, IndexedDB, service worker, disk persistence or background host service is added.

Only fully received files become cached-download buttons. The prominent output line shows short readiness text; detailed cache limits, missing files and exported-snapshot caveats are in **Local files & download help**. Oversize, incomplete, refused or timed-out files remain explicitly unavailable; successfully cached siblings remain usable. Use the saved local files or reopen the same job for unavailable downloads, not duplicate generation. Clicking a cached download makes no network request and still works if the local runtime subsequently disappears. This is an **exported approved snapshot**, not current or revalidated server state; offline cache use does not authorize generation, retry, upload, verification or any server mutation. All other actions remain blocked after a connection failure.

The disconnected page keeps an explicit **server status unknown / cached downloads only / generation, upload and retry blocked** notice even after a cached-download click. Connection diagnostics remain available; the click does not clear the disconnected state. The UI reports a download **request**, not proof that the browser saved the file, a restored connection or successful server revalidation. A screenshot of that state must not be interpreted as a live-runtime or mutation-readiness check.

Cached snapshots, pending fetches, previews and download object URLs are invalidated when privacy choices, setup, job or generation changes. Late responses cannot populate another job/generation. Reloading or closing the page discards the in-memory cache; host termination before complete staging cannot be repaired offline. A timeout or failed cache fetch is not a generation failure, and staging never starts a model, retries generation or changes approval/authorization rules. A cache is not a privacy or revocation guarantee: exported documents may contain details the user chose to keep.

Each cache is bound to the verified status response's generation/decision/manifest identity and per-file SHA-256 hashes. File requests must present that identity; the server validates and reads the pinned generation under its action/state locks, refusing missing or changed identities. The browser checks the returned identity and hashes each complete Blob with WebCrypto before accepting it. An external same-job regeneration can leave an explicitly incomplete old snapshot, never mix newer siblings into it. Missing manifest metadata or browser hashing disables staging; reopen with a compatible backend rather than bypass the checks.

## Boundaries

- Contextual review requires separate cloud-disclosure consent; a local UI does not make inference offline.
- Generation sends only approved reduced input. MCP status/errors omit raw findings, titles, capability URLs and arbitrary exceptions.
- Selected documents can contain details the user explicitly kept; reading them into Copilot is not anonymous processing.
- Private state relies on OS account protection: owner-only directory mode on Unix, inherited user-profile ACLs on Windows. It is not encrypted at rest or isolated from other same-user processes.
- Historical commands are untrusted data; the drafting backend disables tools.
- ArtifactStore root is owner-only plus service administrators; teams inherit live policy. Authentication is not upload consent.
- Rule and contextual detection both miss disclosures and produce false positives. There is no anonymity guarantee.
- CLI/protocol testing does not prove an already-running App loaded the plugin; App refresh depends on host version, profile and policy.

## Diagnostics

Model prompts use a temporary UTF-8 input file rather than a blocking pipe write. A CLI that stalls before reading stdin therefore cannot bypass the model-call timeout on Windows. The file is closed after the attempt; it is not a share artifact. The termination command and stream-drain waits are bounded separately; usage collection and filesystem teardown are not a proven end-to-end wall-time bound. A failed tree-termination command, drain, or worker cleanup remains an explicit uncertainty in private diagnostics. Teardown failures do not replace an earlier timeout warning. Check the local job before retrying when cleanup is unconfirmed; root-process exit alone does not confirm termination of its descendants.

Generation call receipts retain elapsed time and provider-reported usage from the temporary isolated Copilot profile before it is removed. `session_usage` preserves token categories and resolved models as reported by Copilot; cache reads must not be added again to a provider's inclusive input-token total. Missing metrics remain unavailable, not zero or a character-based estimate. The user's normal session store is never searched for metrics. Resumed story and editorial receipts retain `previous_runs`, including failed calls; top-level story call lists and editorial subsets describe overlapping calls and must not be summed together. Receipts are private diagnostics, not part of the share package.

`python scripts/doctor.py` checks prerequisites. `node scripts/plugin_mcp.cjs` speaks newline-delimited MCP JSON-RPC. Private runtime logs/job errors are for local inspection only. `python scripts/session_spec.py studio --session SESSION` remains an explicit fallback. Never modify the host's registry files to conceal native-loading failures.

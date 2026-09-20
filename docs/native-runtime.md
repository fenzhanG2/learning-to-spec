# Native plugin runtime

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

## Native integration

The portable `mcp.json` follows [Agent Plugins 1.0](https://agent-plugins.org/schemas/1.0.0/mcp.schema.json); `.mcp.json` provides legacy integration. The host expands plugin-root variables. The Node launcher selects installed Python ≥3.10 without downloading dependencies. MCP is a protocol adapter, not a shell-command prompt; generation logs never enter its stdout.

The background service starts lazily. `~/.learning-to-spec/runtime/<profile-hash>` stores private state outside the installation directory, scoped to the resolved Copilot home so different profiles cannot share jobs, inherited authentication or approvals. `LEARNING_TO_SPEC_HOME` explicitly overrides this root for tests. Existing local config supplies optional model, GitHub host and call budget. OS file leases serialize startup and prevent competing writers. The service binds only `127.0.0.1`, checks Host/Origin/capability, refuses cross-origin requests and never follows redirects with its capability.

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

## Recovery

`job.json` is atomically replaced at operation start/completion and after synchronous approvals. It contains private decisions and paths and must never be published. Stage/status/time history contains no session text. Source, baseline, review, decisions, delivery, package and remote plan have separate hashes.

Interrupted operations restore as errors, never automatic replay. An unchanged failed generation can reuse its checkpoint after explicit retry; completed generation is idempotent. Changed privacy choices invalidate package/plan and block old delivery until regeneration. Source mutation invalidates review. An existing upload receipt blocks repeat publication; use read-only verification instead. ArtifactStore publication is not atomic: a placeholder or unverified upload may remain after failure.

The service is separate from individual MCP requests, but not guaranteed to outlive host process-tree cleanup. Actual Copilot CLI testing showed that exiting the host terminates plugin descendants on Windows. Keep an interactive host session open while using Studio. `start_review`, `generate` and `verify_publish` wait for their stage to finish, preventing batch CLI exit mid-operation. Saved jobs survive a host restart; interrupted work resumes explicitly. `shutdown` refuses while work runs. Version mismatch requires idle shutdown. Manual foreground `studio` requires its own process to stay alive. Operations are serialized; this is not a distributed queue or an installed OS daemon.

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

`python scripts/doctor.py` checks prerequisites. `node scripts/plugin_mcp.cjs` speaks newline-delimited MCP JSON-RPC. Private runtime logs/job errors are for local inspection only. `python scripts/session_spec.py studio --session SESSION` remains an explicit fallback. Never modify the host's registry files to conceal native-loading failures.

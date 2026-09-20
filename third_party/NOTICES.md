# Incorporated code and design references

This plugin does not import, launch, fetch or require any of the seven reference projects at runtime. It uses its own Copilot adapter, evidence model, pipeline, prompts, validators and renderers. General-purpose rendering dependencies are listed separately in package.json/package-lock.json.

The standalone renderer includes the runtime npm dependencies. Their complete licenses and prebundled notices are preserved in `rendering-dependencies.txt`, generated from the lockfile's installed packages by `npm run build`. The JavaScript bundle also retains its linked license companion. These notices do not grant a license to unrelated research snapshots or private repository content.

Two small algorithms were ported into `session_spec/story_context.py` from pinned MIT-licensed sources. Their original notices are preserved in `MIT-notices.txt`:

- **Logex** — `iamtouchskyer/logex`, commit `12f59ecb1c1de6b8b400fa8e5d363e93e4971c76`, `src/pipeline/prompt.ts`, selection loop in `buildArticlePrompt`: score-ordered, character-budgeted narrative windows. The Python adaptation skips oversize windows rather than ending selection, restores chronological order, and keeps complete evidence alongside the hints. No upstream article template or minimum-length gate is executed.
- **Pi** — `badlogic/pi-mono`, commit `734ab3434fb311f745f63a7974305b7162398af5`, `packages/agent/src/harness/compaction/utils.ts`, `createFileOps`, `extractFileOpsFromMessage`, `computeFileLists`: set-based file-operation accumulation and read-only/modified separation. The adaptation accepts normalized Copilot tool events, retains refs, and calls them requests rather than proven writes. It does not copy Pi's thinking serialization or truncate tool evidence.

The following are design/format references, independently implemented here, not copied source modules or runtime dependencies:

| Reference | Our implementation |
|---|---|
| sctxx `37d60265877e79724dd5412096659fccc0eaf31e` | Atomic requirement lifecycle, provenance checks and resumable cached stages in prompts/pipeline/validation |
| Entire `ae303f31dae58da1a19b67907ca24166755996bf` | Intent, outcome, friction, learning and unresolved-state separation in canonical extraction |
| Compound Engineering `d9a3144088aa9b5afb505f4c1b57def232787e15` | Evidence-grounded transferable lessons with applicability and non-claims |
| Claude-Mem `4d323afe728514e772d4d25dac8cd1e4af0fd5ed` | Investigated, learned, completed and next-work distinctions; no observer database or memory service |
| create-task-for-analysis `d602f29013cf940974e6fffff369e08c81820135` | Coverage of every real user input, including short prompts and explicit feedback; no taxonomy bundle or publishing workflow |

The private playground repository's redistribution license was not confirmed. Its source, schema and taxonomy are not distributed. This is selective code porting plus independent synthesis, not a claim to have merged all seven repositories or reproduced their native workflows. Raw research snapshots, comparisons and experiment runners remain outside this plugin.

# Archived integration examples

Version 0.7 targets the native GitHub Copilot App/CLI extension. Install that plugin and run `/to-spec`; neither an MCP configuration nor a Skill is required.

`copilot-mcp.json`, `claude-mcp.json` and `skills/learning-to-spec/SKILL.md` preserve the earlier explicit MCP/Studio integration for reference. Their host-specific plugin-root variables need deliberate configuration by an integrator. They are not automatically registered, are not a standalone installable plugin, and do not establish native App support.

The repository retains a schema-valid `.codex-plugin/plugin.json` for compatibility metadata only. It declares no skills, MCP servers, capabilities or starter prompts. Installing this metadata in Codex does not provide export functionality. Do not copy these examples back into conventional root MCP/Skill paths: doing so can auto-register historical session-discovery tools alongside the native extension.

The [legacy runtime guide](../native-runtime.md) documents Studio and saved-job recovery for explicitly configured integrations. Its tools and browser controls are not available in the native `/to-spec` output panel.

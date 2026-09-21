import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/build_plugin.py"
SPEC = importlib.util.spec_from_file_location("build_plugin", SCRIPT)
BUILD = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BUILD)


class ReleaseTests(unittest.TestCase):
    def test_public_marketplace_resolves_root_plugin(self):
        root = SCRIPT.parents[1]
        manifest = json.loads((root / 'plugin.json').read_bytes())
        catalog = json.loads((root / '.github/plugin/marketplace.json').read_bytes())
        self.assertEqual(catalog['name'], 'learning-to-spec')
        self.assertEqual(len(catalog['plugins']), 1)
        entry = catalog['plugins'][0]
        self.assertEqual((root / entry['source']).resolve(), root)
        self.assertEqual(entry['name'], manifest['name'])
        self.assertEqual(entry['version'], manifest['version'])
        self.assertEqual(json.loads((root / '.plugin/plugin.json').read_bytes())['version'], manifest['version'])
        self.assertEqual(manifest['extensions'], ['./extensions/'])
        self.assertEqual(manifest['skills'], [])
        self.assertEqual(manifest['mcpServers'], {})
        self.assertFalse((root / 'mcp.json').exists())
        self.assertFalse((root / '.mcp.json').exists())
        self.assertFalse(list((root / 'skills').rglob('SKILL.md')))
        self.assertFalse((root / entry['source'] / 'skills/learning-to-spec/SKILL.md').is_file())

    def test_codex_metadata_is_inert_and_versions_match(self):
        root = SCRIPT.parents[1]
        native = json.loads((root / 'plugin.json').read_bytes())
        compatibility = json.loads((root / '.codex-plugin/plugin.json').read_bytes())
        self.assertEqual(compatibility['name'], native['name'])
        self.assertEqual(compatibility['version'], native['version'])
        for component in ('skills', 'mcpServers', 'apps', 'hooks', 'extensions'):
            self.assertNotIn(component, compatibility)
        self.assertEqual(compatibility['interface']['capabilities'], [])
        self.assertEqual(compatibility['interface']['defaultPrompt'], [])
        self.assertIn('Copilot', compatibility['description'])
        self.assertIn('not Codex', compatibility['description'])
        self.assertEqual(json.loads((root / 'package.json').read_bytes())['version'], native['version'])
        self.assertIn(f'version = "{native["version"]}"', (root / 'pyproject.toml').read_text(encoding='utf-8'))

    @unittest.skipUnless(shutil.which('node'), 'Node.js is needed for renderer integration')
    def test_release_renders_without_npm_or_node_modules(self):
        from test_story_pipeline import article, insights
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            result = BUILD.build(root / 'plugin.zip')
            with zipfile.ZipFile(result['release']) as archive:
                archive.extractall(root / 'installed')
            plugin = root / 'installed/plugins/learning-to-spec'
            self.assertFalse((plugin / 'node_modules').exists())
            for name in ('extension.mjs', 'bridge.mjs', 'workflow.mjs'):
                self.assertTrue((plugin / 'extensions/learning-to-spec' / name).is_file())
            self.assertTrue((plugin / 'scripts/native_bridge.py').is_file())
            self.assertFalse((plugin / 'mcp.json').exists())
            self.assertFalse((plugin / '.mcp.json').exists())
            self.assertTrue((plugin / 'docs/legacy/copilot-mcp.json').is_file())
            self.assertTrue((plugin / 'docs/legacy/claude-mcp.json').is_file())
            self.assertFalse((plugin / 'skills').exists())
            self.assertTrue((plugin / 'docs/legacy/skills/learning-to-spec/SKILL.md').is_file())
            compatibility = json.loads((plugin / '.codex-plugin/plugin.json').read_bytes())
            self.assertNotIn('mcpServers', compatibility)
            self.assertNotIn('skills', compatibility)
            for name in ('native-output.js', 'native-output.html', 'native-output.css'):
                self.assertTrue((plugin / 'session_spec/web' / name).is_file())
            protocol = subprocess.run([sys.executable, str(plugin / 'scripts/plugin_mcp.py')],
                input='{"jsonrpc":"2.0","id":1,"method":"initialize"}\n{"jsonrpc":"2.0","id":2,"method":"tools/list"}\n',
                cwd=root, capture_output=True, text=True, encoding='utf-8', timeout=30)
            self.assertEqual(protocol.returncode, 0, protocol.stderr)
            self.assertEqual(len(json.loads(protocol.stdout.splitlines()[1])['result']['tools']), 12)
            notices = (plugin / 'third_party/rendering-dependencies.txt').read_text(encoding='utf-8')
            for dependency in ('markdown-it@', 'lucide@', '@dagrejs/dagre@', '@dagrejs/graphlib@'):
                self.assertIn(dependency, notices)
            support = root / 'support'
            support.mkdir()
            for name, data in {'article.json': article(), 'insights.json': insights(),
                               'agent-presentation.json': {'schema': 'agent-presentation/v2', 'mode': 'markdown-files'}}.items():
                (support / name).write_text(json.dumps(data), encoding='utf-8')
            output = root / 'human.html'
            for language, disclaimer in (
                ('zh-CN', '不代表为这份文档重新执行过这些检查'),
                ('en', 'the checks were not rerun for this document'),
            ):
                with self.subTest(language=language):
                    (support / 'language.json').write_text(json.dumps({'language': language}), encoding='utf-8')
                    render = subprocess.run([sys.executable, '-c',
                        'from pathlib import Path; import sys; from session_spec.story_pipeline import render_story; render_story(Path(sys.argv[1]), Path(sys.argv[2]))',
                        str(support), str(output)], cwd=plugin, capture_output=True, text=True, encoding='utf-8', timeout=60)
                    self.assertEqual(render.returncode, 0, render.stderr)
                    html = output.read_text(encoding='utf-8')
                    self.assertIn('<!doctype html>', html)
                    self.assertIn(disclaimer, html)
                    self.assertNotIn('_support', html)
                    self.assertNotIn('data-open-agent', html)
            source = plugin / 'session_spec/web/story-page.cjs'
            source.write_bytes(source.read_bytes() + b'\n')
            check = subprocess.run([sys.executable, '-c',
                'from session_spec.story_pipeline import renderer_entry; renderer_entry()'],
                cwd=plugin, capture_output=True, text=True, encoding='utf-8', timeout=60)
            self.assertNotEqual(check.returncode, 0)
            self.assertIn('stale or changed', check.stderr)

    def test_marketplace_contains_only_allowlisted_code(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            plugin = root / "plugin"
            plugin.mkdir()
            (plugin / "plugin.json").write_text(json.dumps({"name": "learning-to-spec", "version": "0.3.0", "description": "Test"}))
            for name in ("session_spec/main.py", "docs/legacy/skills/learning-to-spec/SKILL.md", "skills/learning-to-spec/SKILL.md", "mcp.json", ".mcp.json", "reviews/private.json", "source/events.jsonl", "session_spec/__pycache__/main.pyc", "node_modules/sample/index.js"):
                target = plugin / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text("fixture")
            with patch.object(BUILD, "ROOT", plugin):
                result = BUILD.build(root / "release.zip")
            with zipfile.ZipFile(result["release"]) as archive:
                names = archive.namelist()
                self.assertIn(".github/plugin/marketplace.json", names)
                self.assertIn("plugins/learning-to-spec/docs/legacy/skills/learning-to-spec/SKILL.md", names)
                for stale in ('skills/learning-to-spec/SKILL.md', 'mcp.json', '.mcp.json'):
                    self.assertNotIn('plugins/learning-to-spec/' + stale, names)
                self.assertFalse(any(part in name for name in names for part in ("reviews", "source/", "__pycache__", "node_modules")))
                manifest = json.loads(archive.read("release-manifest.json"))
                self.assertEqual(len(manifest["files"]), 3)


if __name__ == "__main__":
    unittest.main()

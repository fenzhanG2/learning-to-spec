import html
import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path


@unittest.skipUnless(shutil.which("node"), "Node.js required for preview rendering")
class EvidencePreviewTests(unittest.TestCase):
    def test_literal_dollars_and_html_survive_round_trip_without_markup_execution(self):
        payload = '# Evidence companion\n\n### E000001\n\n` $${VALUE} $& $` $\' </textarea><script>alert(1)</script> `\n'
        program = """
const fs = require('node:fs');
const { enhanceSplitAgent } = require('./session_spec/web/evidence-preview.cjs');
const payload = JSON.parse(fs.readFileSync(0, 'utf8'));
const page = '<html><head></head><body><dialog><p class="dialog-note">old</p><div class="dialog-controls"></div><textarea readonly id="agent-source">handoff</textarea></dialog><script>old</script></body></html>';
process.stdout.write(enhanceSplitAgent(page, '# Handoff', payload, 'en', true));
"""
        result = subprocess.run([shutil.which("node"), "-e", program], input=json.dumps(payload), text=True, encoding="utf-8", capture_output=True, check=True, cwd=Path(__file__).resolve().parents[1])
        match = re.search(r'<textarea\b[^>]*id="evidence-source"[^>]*>(.*?)</textarea>', result.stdout, re.S)
        self.assertEqual(html.unescape(match.group(1)), payload)
        self.assertNotIn('<script>alert(1)</script>', result.stdout)

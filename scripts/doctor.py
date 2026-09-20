import json
import shutil
import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from session_spec.backend import find_copilot
from session_spec.story_pipeline import renderer_entry


def main():
    checks = {'python_3_10_or_newer': sys.version_info >= (3, 10), 'node': bool(shutil.which('node'))}
    try:
        checks['copilot_cli'] = bool(find_copilot())
    except (ValueError, FileNotFoundError):
        checks['copilot_cli'] = False
    try:
        checks['bundled_renderer'] = renderer_entry().name == 'render-story.bundle.cjs'
    except (ValueError, OSError):
        checks['bundled_renderer'] = False
    print(json.dumps({'ready': all(checks.values()), 'checks': checks,
                      'azure_cli_optional_for_artifactstore': bool(shutil.which('az')),
                      'note': 'No model call, credential read, runtime installation or upload. Copilot authentication is checked when generation starts.'}, indent=2))
    return int(not all(checks.values()))


if __name__ == '__main__':
    raise SystemExit(main())

"""Build an installable ZIP containing only the distributable plugin files."""

import re
import zipfile
from pathlib import Path

root = Path(__file__).resolve().parents[1]
version = re.search(r"^version:\s*(\S+)", (root / "metadata.yaml").read_text(), re.M)[1]
files = (
    "__init__.py",
    "main.py",
    "runner.py",
    "app_server.py",
    "notifications.py",
    "_conf_schema.json",
    "metadata.yaml",
    "requirements.txt",
    "README.md",
    "LICENSE",
    "CHANGELOG.md",
)
output = root / "dist" / f"astrbot_plugin_codex_cli-v{version}.zip"
output.parent.mkdir(parents=True, exist_ok=True)
with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
    for name in files:
        archive.write(root / name, f"astrbot_plugin_codex_cli/{name}")
print(output)

"""Load the plugin from a standalone repository without an AstrBot data tree."""

import importlib.util
import sys
from pathlib import Path

package = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "astrbot_plugin_codex_cli",
    package / "__init__.py",
    submodule_search_locations=[str(package)],
)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)

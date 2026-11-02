"""Collect installed dependency notices without any user configuration."""
import importlib.metadata as metadata
import json
import shutil
import sys
from pathlib import Path

root = Path(__file__).resolve().parents[1]
destination = root / "build" / "desktop-licenses"
destination.mkdir(parents=True, exist_ok=True)
inventory = []
for distribution in metadata.distributions():
    name = distribution.metadata["Name"]
    inventory.append({"name": name, "version": distribution.version,
                      "license": distribution.metadata.get("License-Expression") or distribution.metadata.get("License"),
                      "project_urls": distribution.metadata.get_all("Project-URL") or []})
    for file in distribution.files or []:
        if not any(token in file.name.lower() for token in ("license", "copying", "notice")):
            continue
        source = Path(distribution.locate_file(file))
        if source.is_file() and source.suffix.lower() not in (".py", ".pyc", ".pyd", ".dll", ".exe"):
            target = destination / name / Path(*[p for p in file.parts if p not in ("..", ".")])
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
python_license = Path(sys.base_prefix) / "LICENSE.txt"
if python_license.is_file():
    shutil.copy2(python_license, destination / "Python-LICENSE.txt")
(destination / "dependency-inventory.json").write_text(json.dumps(inventory, indent=2), encoding="utf-8")
for source in (root / "packaging/licenses").glob("*"):
    shutil.copy2(source, destination / source.name)
print("Dependency notices collected")

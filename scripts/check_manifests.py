"""Check that plugin manifests, the skill and pyproject agree (run in CI)."""

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
errors = []
# Regex instead of tomllib so this also runs on Python 3.10.
version = re.search(r'^version\s*=\s*"([^"]+)"', (ROOT / "pyproject.toml").read_text(), re.M).group(1)

for path in [
    "plugin.json",
    "gemini-extension.json",
    ".claude-plugin/plugin.json",
    ".claude-plugin/marketplace.json",
    ".codex-plugin/plugin.json",
    ".cursor-plugin/plugin.json",
]:
    try:
        data = json.loads((ROOT / path).read_text())
    except (OSError, json.JSONDecodeError) as e:
        errors.append(f"{path}: {e}")
        continue
    if data.get("name") != "tomeshift":
        errors.append(f"{path}: name should be 'tomeshift'")
    if "version" in data and data["version"] != version:
        errors.append(f"{path}: version {data['version']} != pyproject {version}")
    skills = data.get("skills")
    if isinstance(skills, str) and not (ROOT / skills).is_dir():
        errors.append(f"{path}: skills path {skills} does not exist")
    ctx = data.get("contextFileName")
    if ctx and not (ROOT / ctx).is_file():
        errors.append(f"{path}: contextFileName {ctx} does not exist")

for skill in sorted((ROOT / "skills").glob("*/SKILL.md")):
    text = skill.read_text()
    m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    if not m:
        errors.append(f"{skill.relative_to(ROOT)}: missing YAML frontmatter")
        continue
    front = m.group(1)
    name = re.search(r"^name:\s*(\S+)", front, re.M)
    if not name or name.group(1) != skill.parent.name:
        errors.append(f"{skill.relative_to(ROOT)}: name must match folder '{skill.parent.name}'")
    desc = re.search(r"^description:\s*(>?)\s*\n?(.*?)(?=^\S|\Z)", front, re.M | re.S)
    desc_text = " ".join(desc.group(2).split()) if desc else ""
    if not 20 <= len(desc_text) <= 1024:
        errors.append(f"{skill.relative_to(ROOT)}: description must be 20-1024 chars (got {len(desc_text)})")

if errors:
    print("\n".join(errors))
    sys.exit(1)
print(f"manifests OK (version {version})")

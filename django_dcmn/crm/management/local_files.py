"""Local output of the ops commands (`zoho_schema` run logs and `--json`, e2e manifests).

They hold Zoho ids, metadata and test addresses, so each directory gets a `.gitignore` of `*` when it is made.
"""

from pathlib import Path

GITIGNORE = '# Written by the DCMN ops commands (Zoho ids, test addresses): never committed.\n*\n'


def private_dir(path: Path) -> Path:
    """Create `path` (and parents) with a `.gitignore` that ignores everything in it."""
    path.mkdir(parents=True, exist_ok=True)
    marker = path / '.gitignore'
    if not marker.exists():
        marker.write_text(GITIGNORE, encoding='utf-8')
    return path

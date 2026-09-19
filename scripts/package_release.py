from __future__ import annotations

import sys
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = PROJECT_ROOT.parent / "JAS-Master-LoA-Revision.zip"
EXCLUDED_PARTS = {".git", ".pytest_cache", ".venv", "__pycache__", "backups", "generated_documents", "private_uploads", "rendered"}
EXCLUDED_FILES = {"jas.db", "JAS-Master-LoA-Revision.zip"}


def main() -> None:
    output = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else DEFAULT_OUTPUT.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(output, "w", ZIP_DEFLATED) as archive:
        for path in sorted(PROJECT_ROOT.rglob("*")):
            relative = path.relative_to(PROJECT_ROOT)
            if not path.is_file() or any(part in EXCLUDED_PARTS for part in relative.parts):
                continue
            if path.name in EXCLUDED_FILES or path.suffix in {".pyc", ".bak"}:
                continue
            if relative.as_posix() == ".streamlit/secrets.toml":
                continue
            archive.write(path, Path("journal-administration-system") / relative)
    print(output)


if __name__ == "__main__":
    main()

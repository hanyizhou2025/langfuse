"""Direct launcher for the portable annotation workbench."""

from __future__ import annotations

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'src'
for path in (ROOT, SOURCE):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from dataset.annotation_tool.app import main  # noqa: E402


if __name__ == '__main__':
    raise SystemExit(main())

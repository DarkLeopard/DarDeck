"""Гарантируем импорт локального engine (репозиторий) вместо pip-установленной копии."""

import sys
from pathlib import Path

REPO_ROOT = str(Path(__file__).resolve().parent.parent)
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

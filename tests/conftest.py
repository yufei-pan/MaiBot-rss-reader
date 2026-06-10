"""pytest 配置：确保能导入 plugin 模块及同 monorepo 内的 maibot-plugin-sdk。"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_SDK_ROOT = _ROOT.parent / "maibot-plugin-sdk"

for path in (_ROOT, _SDK_ROOT):
    normalized = str(path)
    if path.is_dir() and normalized not in sys.path:
        sys.path.insert(0, normalized)

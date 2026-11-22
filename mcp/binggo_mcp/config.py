"""A host-local profile, with no cookie or LLM credential copies."""
from __future__ import annotations
import json
import os
import sys
from pathlib import Path


def load_profile():
    path = os.environ.get("BINGGO_CONTROL_PROFILE")
    if not path:
        raise RuntimeError("BINGGO_CONTROL_PROFILE is required; run scripts/install_mcp_control.py")
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    root = Path(value["source_root"]).resolve()
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    home = Path(value["user_home"]).resolve()
    if home == root or home == root / "data":
        raise RuntimeError("The installed profile must use its separate user-data directory")
    value["source_root"], value["user_home"] = str(root), str(home)
    return value

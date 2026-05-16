import json
from pathlib import Path
from typing import Any, Dict, List


def read_jsonl_tail(path: Path, limit: int) -> List[Dict[str, Any]]:
    if limit < 1:
        return []
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()
    rows: List[Dict[str, Any]] = []
    for line in lines[-limit:]:
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows

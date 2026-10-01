"""
A pipeline's state between runs — one small JSON per pipeline in the lake:
``{state}/{pipeline}.json`` = the last successful run (id, when) and its
watermark. This is what lets bronze run every 15 minutes and silver every
hour: silver reads only what arrived after its watermark.
"""

from __future__ import annotations

import json
from typing import Any, Dict

from .sip import _filesystem


def _path(location: str, pipeline: str):
    fs, base = _filesystem(location)
    return fs, base.rstrip("/"), f"{base.rstrip('/')}/{pipeline}.json"


def read_state(location: str, pipeline: str) -> Dict[str, Any]:
    """The pipeline's last recorded state, or ``{}`` before its first successful run."""
    import pyarrow.fs as pafs

    fs, _, path = _path(location, pipeline)
    if fs.get_file_info(path).type == pafs.FileType.NotFound:
        return {}
    with fs.open_input_stream(path) as f:
        return json.loads(f.read().decode("utf-8"))


def write_state(location: str, pipeline: str, record: Dict[str, Any]) -> str:
    fs, folder, path = _path(location, pipeline)
    fs.create_dir(folder, recursive=True)
    with fs.open_output_stream(path) as f:
        f.write(json.dumps(record, indent=2, default=str).encode("utf-8"))
    return path

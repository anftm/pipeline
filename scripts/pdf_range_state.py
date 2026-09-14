"""Persistent PDF assessment state shared by all asset publishers."""
import json
from pathlib import Path
from huggingface_hub.errors import HfHubHTTPError

MANIFEST_NAME = "pdf_range_manifest.json"


def empty_state():
    return {"version": 1, "files": {}, "inventories": {}}


def remote_state(api, repo, revision=None):
    try:
        path = api.hf_hub_download(repo_id=repo, repo_type="dataset", filename=MANIFEST_NAME,
                                   revision=revision)
    except HfHubHTTPError as error:
        if getattr(error.response, "status_code", None) == 404:
            return empty_state()
        raise
    state = json.loads(Path(path).read_text())
    if state.get("version") != 1 or not isinstance(state.get("files"), dict):
        raise ValueError("invalid PDF range assessment state")
    return state


def apply_optimized(files, manifest, state):
    for key, entry in (state or {}).get("files", {}).items():
        if entry.get("status") != "optimized":
            continue
        base = manifest.get("files", {}).get(key, {})
        if entry.get("source_kind") == "generated":
            if (base.get("status") != "ready" or base.get("reader_mode") != "pdf"
                    or base.get("sha256") != entry.get("input_sha256")
                    or base.get("profile") != entry.get("input_profile")):
                continue
        elif base.get("status") == "ready":
            # A new conversion/repair is authoritative over an older raw source.
            continue
        path = entry.get("path", "")
        if path.startswith("objects/") and path.endswith("/document.pdf") and ".." not in path.split("/"):
            files[key] = {"s": 2, "m": "p", "p": path}

import hashlib
import json
import re
import httpx2 as httpx


async def dispatch(
    payload: dict[str, object],
    event_type: str,
    github_token: str,
    repository: str,
    max_characters: int = 60_000,
    max_properties: int = 10,
    blob_chunk_characters: int = 60_000,
) -> None:
    url = f"https://api.github.com/repos/{repository}/dispatches"
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {github_token}",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    dispatch_payload: dict[str, object] = payload
    async with httpx.AsyncClient(timeout=20) as client:
        if len(payload) > max_properties or len(serialized) > max_characters:
            blob_url = f"https://api.github.com/repos/{repository}/git/blobs"
            blob_shas: list[str] = []
            for offset in range(0, len(serialized), blob_chunk_characters):
                chunk = serialized[offset:offset + blob_chunk_characters]
                blob_response = await client.post(
                    blob_url, headers=headers, json={"content": chunk, "encoding": "utf-8"},
                )
                if blob_response.status_code != 201:
                    raise RuntimeError(f"pipeline payload upload failed with HTTP {blob_response.status_code}")
                sha: object = blob_response.json().get("sha")
                if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{40,64}", sha):
                    raise RuntimeError("pipeline payload upload returned an invalid blob SHA")
                blob_shas.append(sha)
            dispatch_payload = {
                "payload_blobs": blob_shas,
                "payload_sha256": hashlib.sha256(serialized.encode("utf-8")).hexdigest(),
                "payload_characters": len(serialized),
                "request_id": payload.get("request_id"),
            }
        response = await client.post(
            url, headers=headers,
            json={"event_type": event_type, "client_payload": dispatch_payload},
        )
    if response.status_code != 204:
        raise RuntimeError(f"pipeline dispatch failed with HTTP {response.status_code}")

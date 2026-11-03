#!/usr/bin/env python3
"""Real tiny ffmpeg fixtures for native reuse and unsupported-codec conversion.

Explicit runner integration check; no network, source uploads or bucket writes.
"""

import argparse
import hashlib
import json
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import patch

try:
    from . import convert_reader_assets as converter
    from .pilot_media_stream import build_one
except ImportError:
    import convert_reader_assets as converter
    from pilot_media_stream import build_one


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    results = []
    with tempfile.TemporaryDirectory(prefix="reader-codecs-") as directory:
        work = Path(directory)
        cases = (("native-mp3", "mp3", ["-f", "lavfi", "-i", "sine=frequency=440:duration=1", "-c:a", "libmp3lame"], True),
                 ("adts-aac", "aac", ["-f", "lavfi", "-i", "sine=frequency=440:duration=1", "-c:a", "aac", "-f", "adts"], False),
                 ("alac-m4a", "m4a", ["-f", "lavfi", "-i", "sine=frequency=440:duration=1", "-c:a", "alac"], False),
                 ("native-h264", "mp4", ["-f", "lavfi", "-i", "color=c=red:s=160x120:d=1", "-c:v", "libx264", "-pix_fmt", "yuv420p"], True),
                 ("mpeg4-mp4", "mp4", ["-f", "lavfi", "-i", "color=c=red:s=160x120:d=1", "-c:v", "mpeg4"], False))
        for name, extension, options, native in cases:
            source = work / f"{name}.{extension}"
            subprocess.run(["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
                            *options, str(source)], check=True, timeout=60)
            raw = source.read_bytes()
            def download(_url, target):
                target.write_bytes(raw)
                return hashlib.sha256(raw).hexdigest(), len(raw)
            with patch.object(converter, "download_source", side_effect=download):
                entry, uploads = build_one({"repo": "VoiceOfML/Fixture", "path": source.name,
                    "extension": extension, "revision": "fixture", "source_bytes": len(raw)},
                    work / name, "vomebook/reader-assets-v2")
            artifact = Path(uploads[entry["object"]])
            assert (entry["source_sha256"] == entry["sha256"]) is native, entry
            assert (entry["profile"] == converter.NATIVE_MEDIA_PROFILE) is native, entry
            assert Path(entry["object"]).name == (f"audio.{extension}" if native and extension == "mp3"
                else "audio.mp3" if entry["mode"] == "audio" else "video.mp4"), entry
            if not native:
                converter.validate_media_output(artifact, entry["mode"])
            results.append({"fixture": name, "native": native, "entry": entry,
                            "probe": converter.media_probe(artifact), "passed": True})
            print(f"{name}: native={native} object={entry['object']} passed", flush=True)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps({"results": results, "passed": True}, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

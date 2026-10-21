#!/usr/bin/env python3
"""Exclusive account ownership for PDF render and OCR work."""

import argparse
import hashlib
import json
import re
from pathlib import Path


DEFAULT_CONFIG = Path(__file__).resolve().parents[1] / "pdf-worker-lanes.json"
NAME = re.compile(r"[a-z0-9][a-z0-9-]*")
OWNER = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}")


def _validate_config(config, *, allow_legacy=True):
    if not isinstance(config, dict) or config.get("version") != 1:
        raise ValueError("invalid PDF worker configuration")
    native = config.get("native_lanes")
    converted = config.get("converted_lane")
    if not isinstance(native, list) or not native or not isinstance(converted, dict):
        raise ValueError("PDF workers require native size ranges and a converted lane")
    publisher = str(config.get("publisher_repository") or "").split("/")
    if len(publisher) != 2 or not OWNER.fullmatch(publisher[0]) or not re.fullmatch(r"[A-Za-z0-9._-]+", publisher[1]):
        raise ValueError("invalid PDF publisher repository")
    cursor, names, owners = 0, set(), set()
    for index, lane in enumerate([*native, converted]):
        if not isinstance(lane, dict):
            raise ValueError("invalid PDF worker lane")
        name, owner = lane.get("name"), lane.get("owner")
        if (not isinstance(name, str) or not NAME.fullmatch(name) or name in names
                or not isinstance(owner, str) or not OWNER.fullmatch(owner) or owner.lower() in owners):
            raise ValueError("PDF worker lane names and account owners must be unique")
        names.add(name)
        owners.add(owner.lower())
        repository = lane.get("repository", f"{owner}/pipeline")
        if (not isinstance(repository, str) or not repository.lower().startswith(owner.lower() + "/")
                or not re.fullmatch(r"[A-Za-z0-9-]+/[A-Za-z0-9._-]+", repository)):
            raise ValueError("worker repository must belong to its configured account")
        if index >= len(native):
            continue
        lower, upper = lane.get("min_bytes"), lane.get("max_bytes")
        if type(lower) is not int or lower != cursor:
            raise ValueError("native PDF size ranges must cover all sizes without gaps or overlap")
        if index == len(native) - 1:
            if upper is not None:
                raise ValueError("last native PDF size range must be unbounded")
        elif type(upper) is not int or upper <= lower:
            raise ValueError("invalid native PDF size range")
        cursor = upper
    if allow_legacy:
        legacy = config.get("legacy_configs", [])
        if not isinstance(legacy, list):
            raise ValueError("legacy PDF worker configurations must be a list")
        for prior in legacy:
            if not isinstance(prior, dict) or "legacy_configs" in prior:
                raise ValueError("invalid legacy PDF worker configuration")
            _validate_config(prior, allow_legacy=False)
    return config


def load_config(path=DEFAULT_CONFIG):
    return _validate_config(json.loads(Path(path).read_text(encoding="utf-8")))


def lane_for_owner(config, owner):
    for lane in [*config["native_lanes"], config["converted_lane"]]:
        if lane["owner"].lower() == owner.lower():
            return lane["name"]
    raise ValueError("account has no PDF worker lane")


def config_identity(config):
    # Ignore compatibility snapshots: the fingerprint represents only the
    # routing contract that created the queue.
    routing = {
        "version": config["version"],
        "publisher_repository": config["publisher_repository"],
        "native_lanes": [{key: value for key, value in lane.items() if key != "repository"}
                         for lane in config["native_lanes"]],
        "converted_lane": {key: value for key, value in config["converted_lane"].items()
                           if key != "repository"},
    }
    return hashlib.sha256(json.dumps(routing, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def repository_for_owner(config, owner):
    for routing in [config, *config.get("legacy_configs", [])]:
        for lane in [*routing["native_lanes"], routing["converted_lane"]]:
            if lane["owner"].lower() == owner.lower():
                return lane.get("repository", f"{lane['owner']}/pipeline")
    raise ValueError("account has no PDF worker repository")


def config_for_repository(config, repository):
    """Resolve the queue routing snapshot for a current or legacy worker repo."""
    for routing in [config, *config.get("legacy_configs", [])]:
        for lane in [*routing["native_lanes"], routing["converted_lane"]]:
            candidate = lane.get("repository", lane["owner"] + "/pipeline")
            if candidate.lower() == repository.lower():
                return routing
    raise ValueError("PDF worker repository is not configured")


def config_for_queue(config, queue):
    identity = queue.get("worker_config")
    for routing in [config, *config.get("legacy_configs", [])]:
        if identity == config_identity(routing):
            return routing
    raise ValueError("PDF worker queue configuration is unknown")


def routing_bytes(item):
    value = item.get("routing_source_bytes", item.get("original_source_bytes", item.get("source_bytes")))
    return value if type(value) is int and value > 0 else None


def lane_for(config, item):
    extension = str(item.get("source_extension") or Path(str(item.get("path") or "")).suffix).lower().lstrip(".")
    if extension != "pdf":
        return config["converted_lane"]["name"]
    size = routing_bytes(item)
    if size is None:
        # Unknown native sizes stay in one conservative lane until inspected.
        return config["native_lanes"][-1]["name"]
    for lane in config["native_lanes"]:
        if size >= lane["min_bytes"] and (lane["max_bytes"] is None or size < lane["max_bytes"]):
            return lane["name"]
    raise ValueError("native PDF has no worker lane")


def select_records(config, records, lane, owner):
    if lane_for_owner(config, owner) != lane:
        raise ValueError("account cannot claim another PDF worker lane")
    return [{**item, "worker_lane": lane, "routing_source_bytes": routing_bytes(item)}
            for item in records if lane_for(config, item) == lane]


def validate_queue(config, queue, owner, stage):
    routing = config_for_queue(config, queue)
    lane = lane_for_owner(routing, owner)
    expected_kind = {"render": "pdf-render-queue", "ocr": "pdf-image-ocr-queue"}[stage]
    if (queue.get("version") != 1 or queue.get("kind") != expected_kind
            or queue.get("worker_owner", "").lower() != owner.lower()
            or queue.get("worker_lane") != lane
            or queue.get("worker_config") != config_identity(routing)):
        raise ValueError("PDF worker queue identity mismatch")
    records = [*queue.get("books", []), *queue.get("failed", [])]
    if stage == "render":
        books = {book["key"]: book for book in queue.get("books", [])}
        records.extend({**books.get(item["key"], {}), **item}
                       for shard in queue["shards"] for item in shard["records"])
    for item in records:
        # A ``.djvu`` whose bytes are a PDF is moved onto its derived render
        # during planning, after lane selection. It keeps the native routing
        # only for new queues; older artifacts may lack the field, and its
        # lane is still unambiguous from the source extension.
        routed = item.get("worker_lane")
        if routed is None and item.get("djvu_pdf_fallback") == "pdf-magic":
            routed = lane
        if routed != lane or lane_for(routing, item) != lane:
            raise ValueError("PDF worker queue contains a different account's source")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--owner", required=True)
    parser.add_argument("--queue", type=Path)
    parser.add_argument("--stage", choices=("render", "ocr"))
    args = parser.parse_args()
    config = load_config(args.config)
    if args.queue:
        if not args.stage:
            parser.error("--queue requires --stage")
        queue = json.loads(args.queue.read_text(encoding="utf-8"))
        lane = lane_for_owner(config_for_queue(config, queue), args.owner)
        validate_queue(config, queue, args.owner, args.stage)
    else:
        lane = lane_for_owner(config, args.owner)
    print(lane)


if __name__ == "__main__":
    main()

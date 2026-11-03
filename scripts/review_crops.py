#!/usr/bin/env python3
"""Build durable parse-review crop montages from the public source mirror."""

import hashlib
import io
import json
import os
import urllib.parse


HF_REPOSITORY = os.environ.get("HF_SOURCE_REPOSITORY", "vomebook/BHA-Source-Files")
MAX_INPUT_PIXELS = 40_000_000
MAX_OUTPUT_BYTES = 2 * 1024 * 1024
MAX_TOTAL_BYTES = 50 * 1024 * 1024
MAX_CROP_PAGES = 200
MAX_ARTICLE_CROP_PAGES = 50
MAX_MONTAGE_TILE_PIXELS = 20_000_000


def effective_crop(article: dict, page: int) -> list[float] | None:
    options = dict(article.get("ocr") or {})
    options.update((article.get("ocr_exceptions") or {}).get(str(page)) or {})
    thresholds = options.get("content_thresholds")
    if not isinstance(thresholds, list) or len(thresholds) != 4 or not any(value > 0 for value in thresholds):
        return None
    return [float(value) for value in thresholds]


def requested_crops(request: dict) -> list[tuple[int, dict, list[tuple[int, list[float]]]]]:
    result = []
    body = request.get("body") if isinstance(request.get("body"), dict) else request
    for index, article in enumerate(body.get("articles") or []):
        pages = []
        for page in range(int(article["page_start"]), int(article["page_end"]) + 1):
            thresholds = effective_crop(article, page)
            if thresholds:
                pages.append((page, thresholds))
        if pages:
            if len(pages) > MAX_ARTICLE_CROP_PAGES:
                raise RuntimeError("one parse article has too many cropped review pages")
            result.append((index, article, pages))
    if sum(len(pages) for _index, _article, pages in result) > MAX_CROP_PAGES:
        raise RuntimeError("parse submission has too many cropped review pages")
    return result


def source_path(source_files: list[str], files: dict, page: int) -> str:
    if len(source_files) == 1:
        meta = files.get(source_files[0]) or {}
        previews = (meta.get("page_previews") or {}).get("paths") or []
        if previews:
            if page > len(previews):
                raise RuntimeError(f"source mirror has no preview for page {page}")
            return str(previews[page - 1])
        if page != 1:
            raise RuntimeError(f"single source image cannot provide page {page}")
        path = meta.get("path")
        if path:
            return str(path)
    elif 1 <= page <= len(source_files):
        meta = files.get(source_files[page - 1]) or {}
        previews = (meta.get("page_previews") or {}).get("paths") or []
        if previews:
            return str(previews[0])
        path = meta.get("path")
        if path:
            return str(path)
    raise RuntimeError(f"source mirror cannot resolve parse page {page}")


def crop_tile(image, thresholds: list[float], page: int):
    from PIL import Image, ImageDraw, ImageOps

    image = ImageOps.exif_transpose(image).convert("RGB")
    width, height = image.size
    if width * height > MAX_INPUT_PIXELS:
        raise RuntimeError(f"source page {page} exceeds the review pixel limit")
    top, bottom, left, right = thresholds
    box = (
        int(left * width),
        int(top * height),
        max(int(left * width) + 1, int((1 - right) * width + 0.9999)),
        max(int(top * height) + 1, int((1 - bottom) * height + 0.9999)),
    )
    tile = image.crop(box)
    tile.thumbnail((1600, 1400), Image.Resampling.LANCZOS)
    result = Image.new("RGB", (tile.width, tile.height + 34), "white")
    result.paste(tile, (0, 34))
    ImageDraw.Draw(result).text((12, 10), f"Page {page}", fill="black")
    return result


def montage_bytes(api, revision: str, source_files: list[str], files: dict, pages: list[tuple[int, list[float]]]) -> bytes:
    from PIL import Image

    tiles = []
    tile_pixels = 0
    for page, thresholds in pages:
        path = source_path(source_files, files, page)
        local = api.hf_hub_download(
            repo_id=HF_REPOSITORY, repo_type="dataset", filename=path, revision=revision,
        )
        with Image.open(local) as image:
            tile = crop_tile(image, thresholds, page)
            tile_pixels += tile.width * tile.height
            if tile_pixels > MAX_MONTAGE_TILE_PIXELS:
                raise RuntimeError("parse review montage exceeds the decoded pixel limit")
            tiles.append(tile)
    width = max(tile.width for tile in tiles)
    height = sum(tile.height for tile in tiles) + 12 * (len(tiles) - 1)
    if width > 4096 or height > 4096:
        scale = min(4096 / width, 4096 / height)
        tiles = [tile.resize((max(1, int(tile.width * scale)), max(1, int(tile.height * scale)))) for tile in tiles]
        width = max(tile.width for tile in tiles)
        height = sum(tile.height for tile in tiles) + 12 * (len(tiles) - 1)
    montage = Image.new("RGB", (width, height), "#ececec")
    y = 0
    for tile in tiles:
        montage.paste(tile, ((width - tile.width) // 2, y))
        y += tile.height + 12
    output = io.BytesIO()
    montage.save(output, format="WEBP", quality=86, method=6)
    value = output.getvalue()
    if len(value) > MAX_OUTPUT_BYTES:
        raise RuntimeError("parse review montage exceeds the output size limit")
    return value


def build_review_crops(request: dict) -> list[dict]:
    crops = requested_crops(request)
    if not crops:
        return []
    token = os.environ.get("HF_TOKEN", "")
    if not token:
        raise RuntimeError("HF_TOKEN is required for cropped parse review images")
    body = request.get("body") if isinstance(request.get("body"), dict) else request
    source_files = body.get("source_files")
    if not isinstance(source_files, list) or not source_files or any(not isinstance(value, str) for value in source_files):
        raise RuntimeError("parse review source files are invalid")

    from huggingface_hub import CommitOperationAdd, HfApi

    api = HfApi(token=token)
    revision = api.repo_info(HF_REPOSITORY, repo_type="dataset").sha
    manifest_path = api.hf_hub_download(
        repo_id=HF_REPOSITORY, repo_type="dataset", filename="manifest.json", revision=revision,
    )
    with open(manifest_path, encoding="utf-8") as stream:
        files = (json.load(stream) or {}).get("files") or {}

    assets = []
    data_by_path = {}
    total = 0
    for index, article, pages in crops:
        value = montage_bytes(api, revision, source_files, files, pages)
        total += len(value)
        if total > MAX_TOTAL_BYTES:
            raise RuntimeError("parse review images exceed the total size limit")
        digest = hashlib.sha256(value).hexdigest()
        path = f"review-crops/{digest[:2]}/{digest}.webp"
        data_by_path[path] = value
        assets.append({
            "article_index": index,
            "title": article["title"],
            "pages": [page for page, _thresholds in pages],
            "path": path,
        })
    commit = api.create_commit(
        repo_id=HF_REPOSITORY,
        repo_type="dataset",
        operations=[CommitOperationAdd(path_in_repo=path, path_or_fileobj=value) for path, value in sorted(data_by_path.items())],
        commit_message="Add BHA parse review crops",
        parent_commit=revision,
    )
    output_revision = commit.oid
    base = f"https://huggingface.co/datasets/{HF_REPOSITORY}/resolve/{output_revision}"
    for asset in assets:
        asset["url"] = f"{base}/{urllib.parse.quote(asset['path'], safe='/')}"
    return assets

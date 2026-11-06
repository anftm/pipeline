import re
from collections.abc import Mapping, Set
from typing import Literal, NotRequired, TypedDict, cast
from urllib.parse import unquote_to_bytes


class InsertedPart(TypedDict):
    text: str
    type: str


class InsertedComment(TypedDict):
    text: str
    id: NotRequired[object]


class PartChange(TypedDict, total=False):
    insertBefore: list[InsertedPart]
    insertAfter: list[InsertedPart]
    delete: bool
    diff: str
    type: str


class CommentChange(TypedDict, total=False):
    insertBefore: list[InsertedComment]
    insertAfter: list[InsertedComment]
    delete: bool
    diff: str


class ProofreadPatch(TypedDict):
    version: Literal[2]
    parts: dict[str, PartChange]
    comments: dict[str, CommentChange]
    description: str
    newComments: NotRequired[list[str]]


def _object_dict(value: object) -> dict[object, object] | None:
    return cast(dict[object, object], value) if isinstance(value, dict) else None


def _object_list(value: object) -> list[object]:
    return cast(list[object], value) if isinstance(value, list) else []


def _integer_value(value: object) -> int:
    # Match int() for JSON scalars (including legacy numeric offsets).
    if isinstance(value, (str, bytes, bytearray, int, float)):
        return int(value)
    raise TypeError("comment pivot offset is not numeric")


def utf16_length(value: str) -> int:
    return len(value.encode("utf-16-le")) // 2


def delta_edit_cost(delta: object, original: str) -> int | None:
    if not isinstance(delta, str):
        return None
    consumed = 0
    cost = 0
    for token in delta.split("\t") if delta else []:
        if not token:
            return None
        operation = token[0]
        value = token[1:]
        if operation in {"=", "-"}:
            if not value.isdigit():
                return None
            size = int(value)
            consumed += size
            if operation == "-":
                cost += size
        elif operation == "+":
            try:
                inserted = unquote_to_bytes(value.replace("+", "%2B")).decode("utf-8")
            except (UnicodeDecodeError, ValueError):
                return None
            cost += utf16_length(inserted)
        else:
            return None
    return cost if consumed == utf16_length(original) else None


def diff_cost(delta: object) -> int | None:
    if not isinstance(delta, str):
        return None
    total = 0
    for token in delta.split("\t") if delta else []:
        if not token or token[0] not in {"=", "-", "+"}:
            return None
        if token[0] in {"=", "-"}:
            if not token[1:].isdigit():
                return None
            if token[0] == "-":
                total += int(token[1:])
        else:
            try:
                total += utf16_length(unquote_to_bytes(token[1:].replace("+", "%2B")).decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                return None
    return total


def _patch_index(raw: object, minimum: int, maximum: int) -> int:
    if not isinstance(raw, str) or not re.fullmatch(r"0|[1-9][0-9]*", raw):
        raise ValueError("patch index is invalid")
    value = int(raw)
    if value < minimum or value > maximum:
        raise ValueError("patch index is out of range")
    return value


def _content_part(value: object, part_types: Set[str]) -> InsertedPart:
    part = _object_dict(value)
    if part is None or set(part) != {"text", "type"}:
        raise ValueError("inserted part is invalid")
    text, part_type = part["text"], part["type"]
    if not isinstance(text, str) or not isinstance(part_type, str) or part_type not in part_types:
        raise ValueError("inserted part is invalid")
    if len(text) > 20000:
        raise ValueError("inserted part is too long")
    return {"text": text, "type": part_type}


def validate_proofread_patch(
    patch: object,
    article: Mapping[str, object],
    part_types: Set[str],
) -> ProofreadPatch:
    patch = _object_dict(patch)
    if patch is None or patch.get("version") != 2:
        raise ValueError("invalid OCR patch")
    if set(patch) - {"version", "parts", "comments", "description", "newComments"}:
        raise ValueError("invalid OCR patch fields")
    parts = _object_dict(patch.get("parts"))
    comments = _object_dict(patch.get("comments"))
    if parts is None or comments is None:
        raise ValueError("patch parts and comments must be objects")
    article_parts = _object_list(article.get("parts"))
    article_comments = _object_list(article.get("comments"))
    description = patch.get("description", "")
    normalized: ProofreadPatch = {"version": 2, "parts": {}, "comments": {}, "description": ""}
    if not isinstance(description, str):
        raise ValueError("patch description must be a string")
    if description:
        original_description = str(article.get("description") or "")
        if delta_edit_cost(description, original_description) is None:
            raise ValueError("description diff is invalid")
        if apply_text_delta(original_description, description) != original_description:
            normalized["description"] = description
    for raw_index, raw_change in parts.items():
        index = _patch_index(raw_index, 0, len(article_parts) - 1)
        change = _object_dict(raw_change)
        if change is None or not set(change) <= {"insertBefore", "insertAfter", "delete", "diff", "type"}:
            raise ValueError("part patch is invalid")
        if not change:
            raise ValueError("part patch is empty")
        part_item: PartChange = {}
        for key in ("insertBefore", "insertAfter"):
            if key in change:
                if not isinstance(change[key], list):
                    raise ValueError("inserted parts must be a list")
                values = [_content_part(value, part_types) for value in _object_list(change[key])]
                if values:
                    if key == "insertBefore":
                        part_item["insertBefore"] = values
                    else:
                        part_item["insertAfter"] = values
        if "delete" in change:
            if not isinstance(change["delete"], bool):
                raise ValueError("part delete must be boolean")
            if change["delete"] and any(key in change for key in ("diff", "type")):
                raise ValueError("deleted part cannot also be changed")
            if change["delete"]:
                part_item["delete"] = True
        if "diff" in change:
            original = article_part_text(article, index)
            diff = change["diff"]
            if not isinstance(diff, str) or delta_edit_cost(diff, original) is None:
                raise ValueError("part diff is invalid")
            if apply_text_delta(original, diff) != original:
                part_item["diff"] = diff
        if "type" in change:
            part_type = change["type"]
            if not isinstance(part_type, str) or part_type not in part_types:
                raise ValueError("part type is invalid")
            original_part = _object_dict(article_parts[index]) or {}
            if part_type != (original_part.get("type") or "paragraph"):
                part_item["type"] = part_type
        if part_item:
            normalized["parts"][str(index)] = part_item
    for raw_index, raw_change in comments.items():
        index = _patch_index(raw_index, 1, len(article_comments))
        change = _object_dict(raw_change)
        if change is None or not set(change) <= {"insertBefore", "insertAfter", "delete", "diff"} or not change:
            raise ValueError("comment patch is invalid")
        comment_item: CommentChange = {}
        for key in ("insertBefore", "insertAfter"):
            if key in change:
                if not isinstance(change[key], list):
                    raise ValueError("inserted comments must be a list")
                comment_values: list[InsertedComment] = []
                for value in _object_list(change[key]):
                    comment = _object_dict(value)
                    if comment is None or set(comment) - {"id", "text"} or not isinstance(comment.get("text"), str):
                        raise ValueError("inserted comment is invalid")
                    text = comment["text"]
                    assert isinstance(text, str)
                    inserted: InsertedComment = {"text": text}
                    if "id" in comment:
                        inserted["id"] = comment["id"]
                    comment_values.append(inserted)
                if comment_values:
                    if key == "insertBefore":
                        comment_item["insertBefore"] = comment_values
                    else:
                        comment_item["insertAfter"] = comment_values
        if "delete" in change:
            if not isinstance(change["delete"], bool):
                raise ValueError("comment delete must be boolean")
            if change["delete"]:
                comment_item["delete"] = True
        if "diff" in change:
            original = str(article_comments[index - 1] or "")
            diff = change["diff"]
            if not isinstance(diff, str) or delta_edit_cost(diff, original) is None:
                raise ValueError("comment diff is invalid")
            if apply_text_delta(original, diff) != original:
                comment_item["diff"] = diff
        if comment_item:
            normalized["comments"][str(index)] = comment_item
    if "newComments" in patch:
        new_comments = patch["newComments"]
        if not isinstance(new_comments, list):
            raise ValueError("new comments are invalid")
        comment_strings: list[str] = []
        for value in cast(list[object], new_comments):
            if not isinstance(value, str):
                raise ValueError("new comments are invalid")
            comment_strings.append(value)
        if not comment_strings and not normalized["parts"] and not normalized["comments"] and not normalized["description"]:
            raise ValueError("patch contains no changes")
        if comment_strings:
            normalized["newComments"] = comment_strings
    if not normalized["parts"] and not normalized["comments"] and not normalized["description"] and "newComments" not in normalized:
        raise ValueError("patch contains no changes")
    return normalized


def article_part_text(article: Mapping[str, object], index: int) -> str:
    parts = _object_list(article.get("parts"))
    if index < 0 or index >= len(parts) or (part := _object_dict(parts[index])) is None:
        raise ValueError("part index is invalid")
    values = list(str(part.get("text") or ""))
    pivots = _object_list(article.get("comment_pivots"))
    selected = [item for value in pivots if (item := _object_dict(value)) is not None and item.get("part_idx") == index]
    for pivot in sorted(selected, key=lambda item: _integer_value(item.get("offset", 0)), reverse=True):
        offset = _integer_value(pivot.get("offset", 0))
        if offset < 0 or offset > len(values):
            raise ValueError("comment pivot offset is invalid")
        values.insert(offset, f"〔{pivot.get('index')}〕")
    return "".join(values)


def apply_text_delta(text: str, delta: str) -> str:
    source = text.encode("utf-16-le")
    cursor = 0
    output = bytearray()
    for token in delta.split("\t") if delta else []:
        if not token:
            continue
        operation = token[0]
        value = token[1:]
        if operation == "=":
            if value.isdigit():
                size = int(value) * 2
                output.extend(source[cursor:cursor + size])
                cursor += size
        elif operation == "-":
            if value.isdigit():
                cursor += int(value) * 2
        elif operation == "+":
            try:
                inserted = unquote_to_bytes(value.replace("+", "%2B")).decode("utf-8")
                output.extend(inserted.encode("utf-16-le"))
            except (UnicodeDecodeError, ValueError):
                continue
    if cursor != len(source):
        raise ValueError("delta length mismatch")
    return output.decode("utf-16-le")


def _clip_text(value: object, limit: int | None = None) -> str:
    text = str(value if value is not None else "")
    if limit is None or len(text) <= limit:
        return text
    return text[:limit] + "…"


def _preview_value(value: object, limit: int | None = None) -> object:
    return value if limit is None else _clip_text(value, limit)


def proofread_change_preview(
    patch: ProofreadPatch | None,
    article: Mapping[str, object],
    metadata: Mapping[str, object] | None,
    source: Mapping[str, object],
    clip: int | None = None,
) -> list[dict[str, object]]:
    article = article or {}
    changes: list[dict[str, object]] = []
    if patch:
        article_parts = _object_list(article.get("parts"))
        article_comments = _object_list(article.get("comments"))
        for raw_index in sorted((patch.get("parts") or {}), key=int):
            index = int(raw_index)
            change = patch["parts"][raw_index]
            original_full = article_part_text(article, index)
            original = _clip_text(original_full, clip)
            if "diff" in change:
                edited_full = apply_text_delta(original_full, change["diff"])
                if edited_full != original_full:
                    changes.append({"kind": "part", "index": index + 1, "original": original, "edited": _clip_text(edited_full, clip)})
            if change.get("delete"):
                changes.append({"kind": "part", "index": index + 1, "delete": True, "original": original})
            for key in ("insertBefore", "insertAfter"):
                inserted_parts = change.get("insertBefore") if key == "insertBefore" else change.get("insertAfter")
                for part in inserted_parts or []:
                    changes.append({
                        "kind": "part", "index": index + 1, "insert": key == "insertAfter",
                        "text": _clip_text(part.get("text"), clip), "part_type": part.get("type"),
                    })
            if "type" in change:
                old_part = _object_dict(article_parts[index]) if 0 <= index < len(article_parts) else None
                old_type = old_part.get("type") if old_part else None
                if old_type != change["type"]:
                    changes.append({"kind": "part_type", "index": index + 1, "old": old_type, "new": change["type"]})
        for raw_index in sorted((patch.get("comments") or {}), key=int):
            index = int(raw_index) - 1
            change = patch["comments"][raw_index]
            base = str(article_comments[index] or "") if 0 <= index < len(article_comments) else ""
            original = _clip_text(base, clip)
            if "diff" in change:
                edited = apply_text_delta(base, change["diff"])
                if edited != base:
                    changes.append({"kind": "comment", "index": index + 1, "original": original, "edited": _clip_text(edited, clip)})
            if change.get("delete"):
                changes.append({"kind": "comment", "index": index + 1, "delete": True, "original": original})
            for key in ("insertBefore", "insertAfter"):
                inserted_comments = change.get("insertBefore") if key == "insertBefore" else change.get("insertAfter")
                for comment in inserted_comments or []:
                    changes.append({
                        "kind": "comment", "index": index + 1, "insert": key == "insertAfter",
                        "text": _clip_text(comment.get("text"), clip), "part_type": "comment",
                    })
        for offset, text in enumerate(patch.get("newComments") or [], start=1):
            changes.append({"kind": "new_comment", "index": len(article_comments) + offset, "text": _clip_text(text, clip)})
        if patch.get("description"):
            original = str(article.get("description") or "")
            edited = apply_text_delta(original, patch["description"])
            if edited != original:
                changes.append({
                    "kind": "description",
                    "original": _clip_text(original, clip),
                    "edited": _clip_text(edited, clip),
                })
    if metadata:
        article_meta = _object_dict(metadata.get("article")) or {}
        for field in ("title", "authors", "dates", "tags"):
            if field in article_meta and article.get(field) != article_meta[field]:
                changes.append({"kind": "metadata", "field": field, "old": _preview_value(article.get(field), clip), "new": _preview_value(article_meta[field], clip)})
        source_meta = _object_dict(metadata.get("source")) or {}
        source_fields = {"name": "publication_name", "author": "publication_author", "type": "publication_type", "files": "source_files"}
        for field, key in source_fields.items():
            if field in source_meta and source.get(key) != source_meta[field]:
                changes.append({"kind": "metadata", "field": field, "old": _preview_value(source.get(key), clip), "new": _preview_value(source_meta[field], clip)})
    return changes


def patch_is_safe_to_merge(
    patch: object,
    _article: Mapping[str, object],
    max_diff_cost: int,
    max_net_paragraph_delta: int,
) -> bool:
    if patch is None:
        return True
    patch = _object_dict(patch)
    if patch is None or patch.get("version") != 2 or patch.get("newComments") or patch.get("description"):
        return False
    parts = _object_dict(patch.get("parts") or {})
    comments = _object_dict(patch.get("comments") or {})
    if parts is None or comments is None:
        return False
    if not parts and not comments:
        return False
    delta = 0
    cost = 0
    for raw_change in parts.values():
        change = _object_dict(raw_change)
        if change is None or not change:
            return False
        if set(change) - {"diff", "type", "insertBefore", "insertAfter", "delete"}:
            return False
        if "delete" in change and change["delete"] is not True:
            return False
        if change.get("delete"):
            delta -= 1
        for key in ("insertBefore", "insertAfter"):
            if key in change:
                if not isinstance(change[key], list):
                    return False
                inserted_parts = _object_list(change[key])
                delta += len(inserted_parts)
                for raw_part in inserted_parts:
                    part = _object_dict(raw_part)
                    if part is None or not isinstance(text := part.get("text"), str):
                        return False
                    cost += utf16_length(text)
        if "diff" in change:
            if not isinstance(change["diff"], str):
                return False
            item_cost = diff_cost(change["diff"])
            if item_cost is None:
                return False
            cost += item_cost
    for raw_change in comments.values():
        change = _object_dict(raw_change)
        if change is None or set(change) != {"diff"} or not isinstance(change.get("diff"), str):
            return False
        item_cost = diff_cost(change["diff"])
        if item_cost is None:
            return False
        cost += item_cost
    return -max_net_paragraph_delta <= delta <= max_net_paragraph_delta and cost <= max_diff_cost

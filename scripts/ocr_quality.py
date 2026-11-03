"""Evidence and publication gates for document OCR, independent of model confidence."""

from __future__ import annotations

from collections import Counter
import math
from pathlib import Path
import re
import subprocess
import tempfile
import unicodedata
from difflib import SequenceMatcher

try:
    from . import audit_ocr_language as languages, ocr_layout, ocr_geometry
except ImportError:
    import audit_ocr_language as languages
    import ocr_layout
    import ocr_geometry

POLICY = "ocr-quality-v1"
ROUTING_PROFILE = 'language-routing-v4-corroborated-cjk'
DEFAULT_LANGUAGES = {"Han": "ch", "Japanese": "japan", "Korean": "korean",
                     "Cyrillic": "ru", "Arabic": "ar", "Latin": "en",
                     "Greek": "el", "Thai": "th", "Devanagari": "hi"}


def issue(code, severity, **evidence):
    return {"code": code, "severity": severity, "evidence": evidence}


def status(issues):
    return "failed" if any(i["severity"] == "error" for i in issues) else (
        "review" if any(i["severity"] == "review" for i in issues) else "passed")


def compare_recognizers(rows, models):
    issues = []
    punctuation = lambda text: sum(unicodedata.category(c).startswith('P') for c in text)
    first = ''.join(r['primary'] for r in rows)
    second = ''.join(r['secondary'] for r in rows)
    a, b = punctuation(first), punctuation(second)
    if abs(a - b) >= 4 and min(a, b) < max(a, b) * .6:
        issues.append(issue('recognizers-disagree-on-punctuation', 'review', primary=a, secondary=b,
                            regions=[r['region'] for r in rows if punctuation(r['primary']) != punctuation(r['secondary'])]))
    different = [r['region'] for r in rows if SequenceMatcher(None, r['primary'], r['secondary'], autojunk=False).ratio() < .85]
    if len(different) >= max(2, len(rows) * .2):
        issues.append(issue('recognizers-disagree-on-characters', 'review', regions=different))
    return {'status': status(issues), 'issues': issues, 'models': models, 'regions': rows}


def probe_image(path):
    """Retain regional script votes; an inconclusive OSD result is not a language."""
    import numpy as np
    from PIL import Image

    with Image.open(path) as source:
        image = source.convert("L")
    pixels = np.asarray(image)
    # Only a nearly uniform light page can be positively called blank. A photo,
    # faint writing or detector failure must not acquire blank-page status.
    dark = float(np.count_nonzero(pixels < 220) / pixels.size)
    contrast = int(pixels.max()) - int(pixels.min())
    blank = bool(int(pixels.min()) >= 220 and contrast <= 12)
    result = {"blank": blank, "dark_pixel_share": round(dark, 6), "contrast": contrast,
              "width": image.width, "height": image.height, "regions": [], "issues": []}
    if blank or min(image.size) < 8:
        image.close()
        return result
    with tempfile.TemporaryDirectory(prefix="ocr-script-", dir=Path(path).parent) as temp:
        w, h = image.size
        for label, box in (("whole", (0, 0, w, h)), ("upper", (0, 0, w, round(h * .55))),
                           ("lower", (0, round(h * .45), w, h))):
            target = Path(temp) / (label + ".png")
            with image.crop(box) as crop:
                crop.thumbnail((2400, 2400))
                crop.save(target, compress_level=1)
            try:
                run = subprocess.run(["tesseract", str(target), "stdout", "--psm", "0", "-l", "osd"],
                                     capture_output=True, text=True, timeout=30)
            except (OSError, subprocess.TimeoutExpired) as exc:
                result["issues"].append(issue("language-probe-unavailable", "review", error=str(exc)[:300]))
                break
            row = {"region": label, "returncode": run.returncode}
            if run.returncode:
                row["error"] = run.stderr[-500:]
            else:
                values = dict(line.split(": ", 1) for line in run.stdout.splitlines() if ": " in line)
                row.update(script=values.get("Script"), confidence=float(values.get("Script confidence", 0)),
                           rotation=int(values.get("Rotate", 0)),
                           orientation_confidence=float(values.get("Orientation confidence", 0)))
            result["regions"].append(row)
            if label == "whole" and row.get("confidence", 0) >= 5:
                break
    image.close()
    return result


def select_language(planned, source, probe):
    votes = [r for r in probe.get("regions", []) if r.get("script") in DEFAULT_LANGUAGES]
    counts = Counter(r["script"] for r in votes if r.get("confidence", 0) >= 1)
    strong = {r["script"] for r in votes if r.get("confidence", 0) >= 2}
    strong.update(s for s, n in counts.items() if n >= 2)
    regional = Counter(r["script"] for r in votes if r.get("confidence", 0) >= .5)
    strong.update(s for s, n in regional.items() if n >= 3)
    # Cyrillic models also read Latin. Preserve a regional Cyrillic vote on a
    # bilingual page even if a French passage wins the whole-page OSD vote.
    if "Latin" in strong and any(r["script"] == "Cyrillic" and r.get("confidence", 0) >= .3 for r in votes):
        strong.add("Cyrillic")
    specific = strong - {"Latin", "Han"}
    script = next(iter(specific)) if len(specific) == 1 else (
        "Han" if "Han" in strong and not specific else "Latin" if strong == {"Latin"} else None)
    proposed = DEFAULT_LANGUAGES.get(script, planned)
    if languages.LANGUAGE_SCRIPTS.get(planned) == script or (script == "Han" and planned == "japan"):
        proposed = planned
    explicit = source in {"override", "global"}
    issues = list(probe.get("issues", []))
    # The Cyrillic recognizer includes Latin letters, needed for French passages
    # in the Russian archive. Script coverage is distinct from document language.
    compatible_latin = script == 'Latin' and languages.LANGUAGE_SCRIPTS.get(planned) in {'Cyrillic', 'Greek'}
    if explicit and proposed != planned and not compatible_latin:
        issues.append(issue("explicit-language-conflicts-with-image", "error", planned=planned, observed=proposed))
    elif len(specific) > 1:
        issues.append(issue("multiple-script-models-required", "review", scripts=sorted(strong)))
    return {"language": planned if explicit else proposed,
            "source": source if explicit or script is None else "page-image",
            "planned_language": planned, "scripts": sorted(strong), "issues": issues,
            "probe": probe}


def _valid_box(box):
    return (isinstance(box, (list, tuple)) and len(box) == 4
            and all(isinstance(x, (int, float)) and math.isfinite(x) for x in box)
            and 0 <= box[0] < box[2] <= 1 and 0 <= box[1] < box[3] <= 1)


def scientific_symbol_coverage(payload,recognition):
    proof=recognition.get('language_evidence',{}).get('scientific_symbol_coverage',{})
    model=recognition.get('models',{}).get('models',{}).get('rec',{})
    if (proof.get('classification')!='scientific-symbols' or proof.get('page')!=payload.get('page')
            or proof.get('source_sha256')!=recognition.get('source_sha256')
            or proof.get('input_sha256')!=recognition.get('input_sha256')
            or proof.get('language')!=recognition.get('language')
            or proof.get('script_coverage')!=['Han','Latin'] or not proof.get('review',{}).get('basis')
            or recognition.get('language') not in {'ch','chinese_cht'}
            or model.get('language') not in {'ch','chinese_cht'}
            or not re.fullmatch('[0-9a-f]{64}',str(model.get('sha256','')))):
        return False
    content=payload.get('text','')
    # This structural check is deliberately not a chemical dictionary. Long
    # Latin words/prose cannot acquire scientific coverage from filename,
    # document language or a cached review badge.
    latin=re.findall('[A-Za-z]+',content)
    if not latin or any(not re.fullmatch(r'(?:[A-Z][a-z]?)+',word) for word in latin):return False
    operator_count=sum(content.count(char) for char in '=→⇌↑↓+-⁺⁻')
    return operator_count>=4 and sum(c.isdigit() or c in '₀₁₂₃₄₅₆₇₈₉⁰¹²³⁴⁵⁶⁷⁸⁹' for c in content)>=8


def check_page(payload, recognition=None, *, source_sha256=None, input_sha256=None, profile=None):
    """Recompute checks from stored text/geometry; never trust a cached green flag."""
    recognition = recognition or payload.get("recognition") or {}
    issues = []
    raw = payload.get("raw_blocks", payload.get("blocks", []))
    text = payload.get("text", "")
    spans = payload.get("text_spans", [])
    layout = payload.get("layout", {})
    ocr = payload.get("source") == "ocr"
    if ocr:
        for name, expected in (("source_sha256", source_sha256), ("input_sha256", input_sha256), ("profile", profile)):
            if expected is not None and recognition.get(name) != expected:
                issues.append(issue("recognition-identity-mismatch", "error", field=name))
        if recognition.get("policy") != POLICY:
            issues.append(issue("quality-evidence-missing", "review"))
        evidence = recognition.get("language_evidence", {})
        scientific=scientific_symbol_coverage(payload,recognition)
        issues.extend(row for row in evidence.get('issues',[]) if not(scientific
            and row.get('code')=='explicit-language-conflicts-with-image'
            and row.get('evidence',{}).get('observed')=='en'))
        models = recognition.get("models", {}).get("models", {})
        diagnostics = recognition.get("models", {}).get("diagnostics", {})
        secondary = diagnostics.get("secondary_check", {})
        issues.extend(secondary.get("issues", []))
        issues.extend(diagnostics.get('mixed_script', {}).get('issues', []))
        issues.extend(diagnostics.get('formula_scripts',{}).get('issues',[]))
        issues.extend(diagnostics.get('formula_sums',{}).get('issues',[]))
        if diagnostics.get("empty_regions"):
            issues.append(issue("detected-regions-without-text", "review", regions=diagnostics["empty_regions"]))
        if diagnostics.get('unresolved_regions'):
            issues.append(issue('unresolved-recognition-regions', 'review',
                                regions=[{'box': row['box'], 'reason': row['reason'],
                                          'crop_sha256': row['crop_sha256']}
                                         for row in diagnostics['unresolved_regions']]))
        if not evidence.get("probe", {}).get("blank") and not all(
                re.fullmatch(r"[0-9a-f]{64}", str(models.get(name, {}).get("sha256", ""))) for name in ("det", "rec")):
            issues.append(issue("model-identity-unverified", "review"))
        if not raw and not text.strip() and not evidence.get("probe", {}).get("blank"):
            issues.append(issue("empty-recognition-on-unverified-page", "review"))
        if recognition.get("degenerate"):
            issues.append(issue("degenerate-page-image", "review", width=payload.get("width"), height=payload.get("height")))
        if evidence.get("probe", {}).get("blank") and text.strip():
            issues.append(issue("text-on-verified-blank-image", "error"))
        if not evidence.get("scripts") and raw and evidence.get("source") not in {"override", "global", "native-text"}:
            issues.append(issue("language-image-evidence-inconclusive", "review"))
        for row in evidence.get("probe", {}).get("regions", []):
            if row.get("rotation") and row.get("orientation_confidence", 0) >= 10:
                issues.append(issue("page-orientation-unresolved", "review", region=row["region"], rotation=row["rotation"]))
        language = recognition.get("language", "")
        counts = languages.script_counts(text)
        total = sum(counts.values())
        expected = languages.LANGUAGE_SCRIPTS.get(language)
        if total >= 40 and expected:
            own = counts.get(expected, 0) / total
            latin_covered = (expected in {'Cyrillic', 'Greek'} and 'Latin' in evidence.get('scripts', [])
                             and models.get('rec', {}).get('language') in {'cyrillic', 'el'}
                             and (counts.get('Latin', 0) + counts.get(expected, 0)) / total >= .95)
            if own < languages.MIN_SCRIPT_SHARE.get(language, .05) and not latin_covered and not scientific:
                issues.append(issue("recognition-language-script-mismatch", "error", language=language, counts=counts))
            if language in {"ch", "chinese_cht"} and counts.get("Japanese", 0) >= max(4, total * .015):
                issues.append(issue("japanese-text-under-chinese-language", "error", counts=counts))
        chars = sum(len(b.get("t", "")) for b in raw)
        low = sum(len(b.get("t", "")) for b in raw if b.get("c", 0) < .75)
        if chars >= 40 and low / chars >= .15:
            issues.append(issue("low-recognition-confidence", "review", character_share=round(low / chars, 4)))
        uncertain = [i for i, b in enumerate(raw) if b.get('t') and b.get('c', 0) < .6]
        if uncertain:
            issues.append(issue('uncertain-recognized-regions', 'review', blocks=uncertain))
    valid = True
    for index, block in enumerate(raw):
        if not _valid_box(block.get("b")):
            valid = False
            issues.append(issue("invalid-text-geometry", "error", block=index))
    if valid:
        ordered_boxes = sorted(enumerate(raw), key=lambda pair: pair[1]['b'][0])
        for at, (index, block) in enumerate(ordered_boxes):
            a = block['b']
            for other_index, other in ordered_boxes[at + 1:]:
                b = other['b']
                if b[0] >= a[2]:
                    break
                fraction_overlap=False
                for operator,operator_index,parent_index in [(block,index,other_index),(other,other_index,index)]:
                    proof=operator.get('fraction_bar_evidence',{})
                    parents=operator.get('structural_parent_blocks',[])
                    if (operator.get('t')=='/' and operator.get('confidence_source')=='original-fraction-bar-geometry'
                            and type(proof.get('numerator')) is int and type(proof.get('denominator')) is int
                            and parents==[proof['numerator'],proof['denominator']] and parent_index in parents
                            and operator.get('b')==proof.get('bar_box')
                            and re.fullmatch(r'[0-9a-f]{64}',str(proof.get('bar_pixels_sha256','')))
                            and {r.get('threshold') for r in proof.get('threshold_evidence',[])}=={128,180,220}):
                        fraction_overlap=True
                if fraction_overlap:continue
                if ocr_geometry.overlap(block,other)>=.85:
                    issues.append(issue('overlapping-recognized-regions', 'review', blocks=[index, other_index]))
    if raw:
        seen = []
        cursor = 0
        for span in spans:
            index, start, end = span.get("block"), span.get("start"), span.get("end")
            if (type(index) is not int or not 0 <= index < len(raw) or type(start) is not int
                    or type(end) is not int or not cursor <= start < end <= len(text)
                    or text[start:end] != raw[index].get("t") or span.get("box") != raw[index].get("b")
                    or text[cursor:start].strip()):
                issues.append(issue("text-span-or-coordinate-mismatch", "error", block=index))
                continue
            seen.append(index)
            cursor = end
        if sorted(seen) != [i for i, b in enumerate(raw) if b.get("t")]:
            issues.append(issue("text-blocks-dropped-or-duplicated", "error"))
        if text[cursor:].strip():
            issues.append(issue("text-outside-source-blocks", "error"))
    elif text.strip():
        issues.append(issue("text-without-positioned-blocks", "review"))
    if valid and raw and not layout.get("rotation_clockwise"):
        mode = layout.get("writing_mode", "auto")
        tall = [b for b in raw if len(b.get("t", "")) >= 3
                and ocr_layout.physical_ratio(b, payload["width"], payload["height"]) < .6]
        if len(tall) >= 3 and len(tall) / len(raw) >= .75 and not mode.startswith("vertical"):
            issues.append(issue("vertical-text-in-horizontal-order", "error"))
        if mode.startswith("vertical"):
            working = [{**b, "id": i} for i, b in enumerate(raw) if b.get('detector_class') != 9]
            sections = {b.get('section', 0) for b in working}
            for section in sections:
                selected = [b for b in working if b.get('section', 0) == section]
                cut = ocr_layout.gap_split(selected, 1, .012)
                if cut and all(len(part) >= 2 for part in cut):
                    tier = {b["id"]: index for index, part in enumerate(cut) for b in part}
                    sequence = [tier[s["block"]] for s in spans if s.get("block") in tier]
                    if sequence != sorted(sequence):
                        issues.append(issue("vertical-tiers-interleaved", "error", section=section))
        elif mode.startswith("horizontal") and len(raw) >= 6:
            # Check an independently visible column gutter against published
            # order. A row-wise sequence must not alternate between columns.
            working = [{**b, "id": i} for i, b in enumerate(raw)]
            cut = ocr_layout.gap_split(working, 0, .03)
            if cut and all(len(part) >= 3 for part in cut):
                rows = [max(b["b"][1] for b in part) - min(b["b"][1] for b in part) for part in cut]
                if min(rows) >= .08:
                    column = {b["id"]: index for index, part in enumerate(cut) for b in part}
                    sequence = [column[s["block"]] for s in spans if s.get("block") in column]
                    if sequence != sorted(sequence, reverse=mode == "horizontal-rtl"):
                        issues.append(issue("text-columns-interleaved", "error"))
    for flag in layout.get("review", []):
        if flag in {"blocks-outside-explicit-regions", "within-block-character-order-unverified"}:
            issues.append(issue(flag, "review"))
    for table in layout.get('tables', []):
        for flag in table.get('review', []):
            issues.append(issue(flag['code'], 'review', **{k:v for k,v in flag.items() if k!='code'}))
    for diagram in layout.get('flow_diagrams',[]):
        for flag in diagram.get('review',[]):
            issues.append(issue(flag['code'],'review',**{k:v for k,v in flag.items() if k!='code'}))
    return {"policy": POLICY, "page": payload.get("page"), "status": status(issues), "issues": issues,
            "scope": "automated-consistency-checks", "character_accuracy": "unreviewed"}


def book_report(reports, page_count):
    reports = list(reports)
    issues = [{**i, "page": row["page"]} for row in reports for i in row["issues"]]
    if [r["page"] for r in reports] != list(range(1, page_count + 1)):
        issues.append(issue("quality-page-coverage-incomplete", "error"))
    return {"policy": POLICY, "status": status(issues), "checked_pages": len(reports),
            "page_count": page_count, "affected_pages": sorted({i["page"] for i in issues if i.get("page")}),
            "counts": dict(Counter(i["code"] for i in issues)), "issues": issues,
            "scope": "automated-consistency-checks", "character_accuracy": "unreviewed"}

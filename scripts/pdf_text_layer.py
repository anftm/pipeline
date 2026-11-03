"""Independent, versioned Reader text layers and explicit correction acceptance."""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
import unicodedata

VERSION = 1


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def sha(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ValueError("invalid SHA-256")
    return value


def number(value, positive=False):
    if (type(value) not in (int, float) or not math.isfinite(value)
            or (value <= 0 if positive else value < 0)):
        raise ValueError("invalid geometry number")
    return value


def box(value):
    if (not isinstance(value, list) or len(value) != 4
            or any(number(x) > 1 for x in value)
            or value[0] >= value[2] or value[1] >= value[3]):
        raise ValueError("invalid normalized text box")
    return value


def quad(value):
    if (not isinstance(value, list) or len(value) != 4
            or any(not isinstance(point, list) or len(point) != 2
                   or any(type(n) not in (int, float) or not math.isfinite(n) or n < 0 or n > 1
                          for n in point) for point in value)):
        raise ValueError("invalid normalized text quadrilateral")
    area = sum(value[i][0] * value[(i + 1) % 4][1]
               - value[(i + 1) % 4][0] * value[i][1] for i in range(4)) / 2
    if abs(area) <= 1e-9:
        raise ValueError("degenerate normalized text quadrilateral")
    return value


def validate_formula_relations(relations, regions):
    """Positioned source relations refer to stable regions and exact local text."""
    if not isinstance(relations,list) or len(relations)>1000:
        raise ValueError('invalid formula relations')
    known={r['id']:r for r in regions}
    identities=set()
    def anchor(item):
        if not isinstance(item,dict) or item.get('region_id') not in known:
            raise ValueError('unknown formula relation anchor')
        region=known[item['region_id']];start,end=item.get('start'),item.get('end')
        if (type(start) is not int or type(end) is not int or not 0<=start<end<=len(region['text'])
                or region['text'][start:end]!=item.get('text')):
            raise ValueError('stale formula relation anchor text')
    for relation in relations:
        if not isinstance(relation,dict):raise ValueError('invalid formula relation')
        identity=relation.get('id')
        if (not isinstance(identity,str) or not re.fullmatch(r'f[0-9a-f]{16}',identity)
                or identity in identities or relation.get('kind')!='printed-equilibrium'
                or relation.get('axis') not in {'horizontal','vertical'}):
            raise ValueError('invalid formula relation identity')
        operators=({'⇌':['right','left'],'⇋':['left','right']} if relation['axis']=='horizontal'
                   else {'⇅':['up','down'],'⇵':['down','up']})
        operator=relation.get('operator',{}).get('text')
        if relation.get('directions')!=operators.get(operator) or operator not in operators:
            raise ValueError('formula relation arrow order differs from its printed operator')
        box(relation.get('box'));quad(relation.get('quad'))
        for key in ('operator','from','to'):anchor(relation.get(key))
        proof=relation.get('evidence',{})
        if not isinstance(proof,dict) or not isinstance(proof.get('reason'),str) or not proof['reason'].strip():
            raise ValueError('formula relation lacks image evidence')
        sha(proof.get('crop_sha256'))
        identities.add(identity)


def validate_formula_continuations(continuations, regions, content):
    """Keep one printed formula's wrapped line distinct from a reaction branch.

    These links join existing exact text spans, never insert an arrow, change
    '=' to equilibrium, drop a repeated term, or infer a missing coefficient.
    """
    if not isinstance(continuations,list) or len(continuations)>1000:
        raise ValueError('invalid formula continuations')
    known={r['id']:r for r in regions};identities=set();pairs=set()
    def anchor(item):
        if not isinstance(item,dict) or item.get('region_id') not in known:
            raise ValueError('unknown formula continuation anchor')
        region=known[item['region_id']];start,end=item.get('start'),item.get('end')
        if (type(start) is not int or type(end) is not int or not 0<=start<end<=len(region['text'])
                or region['text'][start:end]!=item.get('text')):
            raise ValueError('stale formula continuation anchor')
        return region
    for link in continuations:
        if not isinstance(link,dict):raise ValueError('invalid formula continuation')
        identity=link.get('id')
        if (not isinstance(identity,str) or not re.fullmatch(r'c[0-9a-f]{16}',identity)
                or identity in identities or link.get('kind')!='printed-formula-continuation'
                or link.get('join_separator') not in {'',' '}):
            raise ValueError('invalid formula continuation identity')
        first=anchor(link.get('from'));last=anchor(link.get('to'));operator=anchor(link.get('operator'))
        pair=(first['id'],last['id'])
        if (pair in pairs or first['id']==last['id'] or operator['id']!=first['id']
                or link['operator']['text'] not in {'+','-','−','=','⇌','⇋'}
                or link['operator']['end']!=len(first['text']) or link['from']['end']!=len(first['text'])
                or link['operator']['start']<link['from']['start'] or link['to']['start']!=0
                or first['order']+1!=last['order']):
            raise ValueError('formula continuation is not an adjacent wrapped source line')
        gap=content[first['end']:last['start']]
        if not gap or '\n' not in gap or any(c not in ' \t\r\n' for c in gap):
            raise ValueError('formula continuation lost its printed line break')
        box(link.get('box'));quad(link.get('quad'))
        proof=link.get('evidence',{})
        if not isinstance(proof,dict) or not isinstance(proof.get('reason'),str) or not proof['reason'].strip():
            raise ValueError('formula continuation lacks source evidence')
        sha(proof.get('crop_sha256'));identities.add(identity);pairs.add(pair)


def validate_errata_relations(relations, regions):
    """Link printed wrong text, its arrow and correction without replacing either.

    Anchors are exact codepoint spans in original positioned regions. The
    relation never supplies a second transcription or invented character boxes.
    """
    if not isinstance(relations,list) or len(relations)>1000:
        raise ValueError('invalid printed errata relations')
    known={r['id']:r for r in regions};identities=set();operators=set()
    def anchor(item):
        if (not isinstance(item,dict) or not isinstance(item.get('region_id'),str)
                or item['region_id'] not in known):
            raise ValueError('unknown printed erratum anchor')
        region=known[item['region_id']];start,end=item.get('start'),item.get('end')
        if (type(start) is not int or type(end) is not int or not 0<=start<end<=len(region['text'])
                or region['text'][start:end]!=item.get('text')):
            raise ValueError('stale printed erratum anchor')
        return region['start']+start,region['start']+end
    arrows={'↓':('vertical','down'),'↑':('vertical','up'),
            '→':('horizontal','right'),'←':('horizontal','left')}
    for relation in relations:
        if not isinstance(relation,dict):raise ValueError('invalid printed erratum')
        identity=relation.get('id')
        if (not isinstance(identity,str) or not re.fullmatch(r'e[0-9a-f]{16}',identity)
                or identity in identities or relation.get('kind')!='printed-erratum'):
            raise ValueError('invalid printed erratum identity')
        wrong=anchor(relation.get('wrong'));operator=anchor(relation.get('operator'))
        correct=anchor(relation.get('correct'))
        mark=relation['operator']['text']
        if mark not in arrows or (relation.get('axis'),relation.get('direction'))!=arrows[mark]:
            raise ValueError('printed erratum arrow direction differs from source')
        if (not wrong[1]<=operator[0]<operator[1]<=correct[0]
                or relation['wrong']['text']==relation['correct']['text']
                or not relation['wrong']['text'].strip() or not relation['correct']['text'].strip()
                or operator in operators):
            raise ValueError('printed erratum must preserve distinct wrong-arrow-correct spans')
        bounds=box(relation.get('box'));polygon=quad(relation.get('quad'))
        if any(not bounds[0]-1e-8<=x<=bounds[2]+1e-8 or not bounds[1]-1e-8<=y<=bounds[3]+1e-8
               for x,y in polygon):
            raise ValueError('printed erratum quadrilateral is outside source box')
        proof=relation.get('evidence',{})
        if not isinstance(proof,dict) or not isinstance(proof.get('reason'),str) or not proof['reason'].strip():
            raise ValueError('printed erratum lacks original-source evidence')
        sha(proof.get('crop_sha256'));identities.add(identity);operators.add(operator)


def validate_flow_diagrams(diagrams,regions):
    """Check stable source memberships and directed routes, without copying text."""
    if not isinstance(diagrams,list) or len(diagrams)>100:raise ValueError('invalid flow diagrams')
    by_id={r['id']:r for r in regions};known=set(by_id);owned=set()
    for diagram in diagrams:
        if not isinstance(diagram,dict):raise ValueError('invalid flow diagram')
        box(diagram.get('box'));sha(diagram.get('source_pixels_sha256'))
        nodes=diagram.get('nodes');edges=diagram.get('edges')
        if not isinstance(nodes,list) or not nodes or not isinstance(edges,list):raise ValueError('invalid flow diagram structure')
        identities=set();node_boxes={}
        for node in nodes:
            if not isinstance(node,dict):raise ValueError('invalid flow node')
            identity=node.get('id');refs=node.get('region_ids')
            box(node.get('box'))
            if (not isinstance(identity,str) or identity in identities or not isinstance(refs,list)
                    or not refs or any(r not in known for r in refs) or len(set(refs))!=len(refs)
                    or owned.intersection(refs) or node.get('label_region_id') not in refs):
                raise ValueError('invalid flow node region ownership')
            identities.add(identity);owned.update(refs)
            node_boxes[identity]=node['box']
            for mark in node.get('marks',[]):
                if mark.get('kind')!='vertical-dots' or len(mark.get('boxes',[]))!=3:
                    raise ValueError('invalid source diagram marks')
                for bounds in mark['boxes']:box(bounds)
                sha(mark.get('source_pixels_sha256'))
                if mark.get('operator_region_id') is not None and (
                        mark['operator_region_id'] not in refs or by_id[mark['operator_region_id']]['text']!='⋮'):
                    raise ValueError('flow dots token differs from its source mark')
        routes=set()
        for edge in edges:
            if not isinstance(edge,dict):raise ValueError('invalid flow edge')
            first,last=edge.get('from'),edge.get('to');points=edge.get('path')
            if first not in identities or last not in identities or first==last or (first,last) in routes:
                raise ValueError('invalid directed flow connection')
            if (not isinstance(points,list) or not 2<=len(points)<=100 or any(
                    not isinstance(point,list) or len(point)!=2 or any(number(v)>1 for v in point) for point in points)):
                raise ValueError('invalid positioned flow path')
            for (x,y),(u,v) in zip(points,points[1:]):
                if (x==u and y==v) or min(abs(u-x),abs(v-y))>1e-8:
                    raise ValueError('flow path is not orthogonal')
            for point,node_id in ((points[0],first),(points[-1],last)):
                x,y=point;a,b,c,d=node_boxes[node_id]
                if (not a-1e-8<=x<=c+1e-8 or not b-1e-8<=y<=d+1e-8
                        or min(abs(x-a),abs(x-c),abs(y-b),abs(y-d))>1e-8):
                    raise ValueError('flow connection misses its original source node')
            (x,y),(u,v)=points[-2:]
            direction=('right' if u>x else 'left') if abs(v-y)<1e-8 else ('down' if v>y else 'up')
            evidence=edge.get('evidence',{})
            if edge.get('arrow_direction')!=direction or evidence.get('arrowhead',{}).get('direction')!=direction:
                raise ValueError('flow arrow direction differs from source route')
            sha(evidence.get('source_path_sha256'));routes.add((first,last))


def direction(text):
    strong = {unicodedata.bidirectional(c) for c in text} & {"L", "R", "AL"}
    if "L" in strong and strong & {"R", "AL"}:
        return "mixed"
    if strong & {"R", "AL"}:
        return "rtl"
    return "ltr" if "L" in strong else "neutral"


def scripts(text):
    result = set()
    for char in text:
        name = unicodedata.name(char, "")
        for prefix, script in (("CJK", "Hani"), ("HIRAGANA", "Hira"), ("KATAKANA", "Kana"),
                               ("HANGUL", "Hang"), ("ARABIC", "Arab"), ("HEBREW", "Hebr"),
                               ("CYRILLIC", "Cyrl"), ("GREEK", "Grek"), ("LATIN", "Latn"),
                               ("DEVANAGARI", "Deva"), ("THAI", "Thai")):
            if name.startswith(prefix):
                result.add(script)
                break
    return sorted(result)


def from_page(payload, source_sha256, language="und", raw_resource=None, *, layout_options=None):
    """Retain engine text verbatim; expose only offsets actually matching it."""
    sha(source_sha256)
    if payload.get("kind") != "pdf-ocr-page" or payload.get("source") not in {"native", "ocr"}:
        raise ValueError("invalid raw page")
    page = payload.get("page")
    if type(page) is not int or page < 1:
        raise ValueError("invalid page number")
    width, height = number(payload["width"], True), number(payload["height"], True)
    text = payload.get("text")
    if not isinstance(text, str):
        raise ValueError("invalid page text")
    layout = payload.get("layout", {})
    mode = layout.get("writing_mode", "auto")
    flags = set(layout.get("review", []))
    options = dict(layout_options or {})
    if options:
        try:
            from . import ocr_layout
        except ImportError:
            import ocr_layout
        options = ocr_layout.validate_options(options)
    regions = []
    blocks = {b.get("id", index): b for index, b in enumerate(payload.get("blocks", []))}
    spans = payload.get("text_spans", [])
    if text and not spans:
        flags.add("text-without-positioned-spans")
    cursor = 0
    for index, span in enumerate(spans):
        start, end = span.get("start"), span.get("end")
        block = blocks.get(span.get("block"))
        if (type(start) is not int or type(end) is not int or not cursor <= start < end <= len(text)
                or block is None or text[start:end] != block.get("t")):
            flags.add("text-span-mismatch")
            continue
        try:
            bounds = box(list(span["box"]))
            polygon = block.get("q") or [[bounds[0], bounds[1]], [bounds[2], bounds[1]],
                                         [bounds[2], bounds[3]], [bounds[0], bounds[3]]]
            if (len(polygon) != 4 or any(len(p) != 2 for p in polygon)
                    or any(number(n) > 1 for p in polygon for n in p)):
                raise ValueError("invalid text quadrilateral")
            confidence = number(block.get("c", 0))
            if confidence > 1:
                raise ValueError("invalid confidence")
        except (ValueError, KeyError, TypeError):
            flags.add("invalid-text-geometry")
            continue
        content = text[start:end]
        bidi = direction(content)
        detected_scripts = scripts(content)
        if bidi in {"rtl", "mixed"}:
            flags.add("bidi-order-needs-review")
        block_source = block.get('s', payload['source'])
        if block_source not in {'native', 'ocr'}:
            block_source = payload['source']
        if block_source == "ocr" and confidence < .90:
            flags.add("low-recognition-confidence")
        if block_source == "ocr":
            supported = ({"Hani", "Latn"} if language in {"ch", "chinese_cht"} else
                         {"Latn"} if language == "en" else
                         {"Hani", "Hira", "Kana", "Latn"} if language == "japan" else None)
            if supported is not None and set(detected_scripts) - supported:
                flags.add("recognition-language-script-mismatch")
        region_mode = span.get("writing_mode", mode)
        if "writing_mode" not in span and options:
            region_mode = options.get("writing_mode", mode)
            region_mode = mode if region_mode == "auto" else region_mode
            center_x, center_y = (bounds[0] + bounds[2]) / 2, (bounds[1] + bounds[3]) / 2
            for override in options.get("regions", []):
                original_box = ocr_layout.map_box_back(override["box"], options.get("rotation", 0))
                if original_box[0] <= center_x <= original_box[2] and original_box[1] <= center_y <= original_box[3]:
                    region_mode = override.get("writing_mode", region_mode)
                    if region_mode == "auto":
                        region_mode = mode
                        flags.add("region-writing-mode-unverified")
                    break
        if region_mode.startswith("vertical"):
            flags.add("vertical-recognition-needs-sample-validation")
        regions.append({"id": f"b{span['block']}", "order": len(regions), "start": start, "end": end,
                        "text": content, "box": bounds, "quad": copy.deepcopy(polygon),
                         "mapping_precision": "block", "source": block_source,
                        "confidence": confidence, "language_hint": language,
                        "scripts": detected_scripts, "direction": bidi,
                        "writing_mode": region_mode, "layout_region": span.get("region", index)})
        cursor = end
    if payload["source"] == "ocr" and not text.strip():
        # An empty detector output cannot establish that the source page is blank.
        flags.add("empty-recognition-unverified")
    geometry = {"width": width, "height": height, "coordinate_space": "normalized-page",
                "ocr_rotation_clockwise": layout.get("rotation_clockwise", 0)}
    result = {"version": VERSION, "kind": "pdf-text-layer", "source_sha256": source_sha256,
              "page": page, "page_identity": digest({"source": source_sha256, "page": page,
                                                     "geometry": geometry}),
              "geometry": geometry, "offset_unit": "unicode-codepoint", "text": text,
              "regions": regions, "raw_sha256": digest(payload), "revision": "raw",
              "processing": "processed" if text.strip() else "processed-empty",
              "quality": "unreviewed", "review_flags": sorted(flags),
              "provenance": {"source": payload["source"], "language_hint": language}}
    if raw_resource is not None:
        result["provenance"]["raw_resource"] = copy.deepcopy(raw_resource)
    if layout.get('tables'):
        # Cells refer to stable positioned regions, so accepted corrections can
        # change their text/offsets without leaving a stale second text copy.
        result['tables'] = copy.deepcopy(layout['tables'])
        for table in result['tables']:
            for cell in table['cells']:
                cell['region_ids'] = [f'b{index}' for index in cell.pop('blocks')]
                cell.pop('text', None)
    if layout.get('fractions'):
        result['fractions']=copy.deepcopy(layout['fractions'])
        for fraction in result['fractions']:
            for role in ('numerator','denominator','operator'):
                fraction[role+'_region_id']='b'+str(fraction.pop(role+'_block'))
    if layout.get('flow_diagrams'):
        result['flow_diagrams']=copy.deepcopy(layout['flow_diagrams'])
        for diagram in result['flow_diagrams']:
            for node in diagram['nodes']:
                node['region_ids']=['b'+str(i) for i in node.pop('blocks')]
                node['label_region_id']='b'+str(node.pop('label_block'))
                for mark in node.get('marks',[]):
                    if 'operator_block' in mark:mark['operator_region_id']='b'+str(mark.pop('operator_block'))
    result["generation"] = digest(result)
    validate(result)
    return result


def validate(layer):
    if layer.get("version") != VERSION or layer.get("kind") != "pdf-text-layer":
        raise ValueError("invalid text layer schema")
    for field in ("source_sha256", "page_identity", "raw_sha256", "generation"):
        sha(layer.get(field))
    if type(layer.get("page")) is not int or layer["page"] < 1:
        raise ValueError("invalid text layer page")
    if layer.get("offset_unit") != "unicode-codepoint" or not isinstance(layer.get("text"), str):
        raise ValueError("invalid text offset contract")
    if (layer.get("revision") not in {"raw", "accepted"}
            or layer.get("quality") not in {"unreviewed", "partially-reviewed", "accepted"}
            or layer.get("processing") not in {"processed", "processed-empty"}):
        raise ValueError("invalid text layer state")
    if layer["revision"] == "raw" and layer["quality"] != "unreviewed":
        raise ValueError("raw text is not reviewed text")
    if layer["revision"] == "accepted":
        sha(layer.get("parent_generation"))
        if layer["quality"] != "partially-reviewed" or not layer.get("acceptances"):
            raise ValueError("accepted text lacks explicit review audit")
        for audit in layer["acceptances"]:
            sha(audit.get("proposal_sha256"))
            if not isinstance(audit.get("actor"), str) or not audit["actor"].strip():
                raise ValueError("invalid correction audit actor")
    geometry = layer["geometry"]
    number(geometry["width"], True)
    number(geometry["height"], True)
    if geometry.get("coordinate_space") != "normalized-page":
        raise ValueError("unsupported coordinate space")
    if geometry.get("ocr_rotation_clockwise") not in (0, 90, 180, 270):
        raise ValueError("invalid OCR rotation")
    if layer["page_identity"] != digest({"source": layer["source_sha256"], "page": layer["page"],
                                         "geometry": geometry}):
        raise ValueError("page identity mismatch")
    previous, ids = 0, set()
    for index, region in enumerate(layer["regions"]):
        start, end = region["start"], region["end"]
        if (type(start) is not int or type(end) is not int
                or not previous <= start < end <= len(layer["text"])
                or layer["text"][start:end] != region["text"]
                or region["id"] in ids or region["order"] != index):
            raise ValueError("invalid text region offsets or identity")
        box(region["box"])
        quad(region["quad"])
        if (region["direction"] not in {"ltr", "rtl", "mixed", "neutral"}
                or region["writing_mode"] not in {"auto", "horizontal-ltr", "horizontal-rtl",
                                                   "vertical-rl", "vertical-lr"}
                or region["mapping_precision"] not in {"block", "corrected-block"}):
            raise ValueError("invalid region writing contract")
        if (number(region["confidence"]) > 1 or region["source"] not in {"native", "ocr"}
                or not isinstance(region.get("language_hint"), str)):
            raise ValueError("invalid region recognition provenance")
        previous = end
        ids.add(region["id"])
    for table in layer.get('tables', []):
        box(table['box'])
        sha(table['source_pixels_sha256'])
        for axis in ('rows', 'columns'):
            positions = table.get(axis)
            if (not isinstance(positions, list) or len(positions) < 2
                    or any(number(p) > 1 for p in positions)
                    or any(a >= b for a, b in zip(positions, positions[1:]))):
                raise ValueError('invalid table grid')
        occupied, members = set(), set()
        for cell in table['cells']:
            box(cell['box'])
            row, column, rowspan, colspan = (cell.get(k) for k in ('row', 'column', 'rowspan', 'colspan'))
            if (any(type(n) is not int for n in (row, column, rowspan, colspan))
                    or min(row, column) < 0 or min(rowspan, colspan) < 1
                    or row+rowspan >= len(table['rows']) or column+colspan >= len(table['columns'])):
                raise ValueError('invalid table cell span')
            slots = {(r, c) for r in range(row, row+rowspan) for c in range(column, column+colspan)}
            refs = cell.get('region_ids')
            if (occupied & slots or not isinstance(refs, list) or any(i not in ids for i in refs)
                    or len(set(refs)) != len(refs) or members.intersection(refs)):
                raise ValueError('invalid table cell region ownership')
            occupied.update(slots)
            members.update(refs)
        if len(occupied) != (len(table['rows'])-1)*(len(table['columns'])-1):
            raise ValueError('incomplete table cell coverage')
    validate_formula_relations(layer.get('formula_relations',[]),layer['regions'])
    validate_formula_continuations(layer.get('formula_continuations',[]),layer['regions'],layer['text'])
    validate_errata_relations(layer.get('errata_relations',[]),layer['regions'])
    validate_flow_diagrams(layer.get('flow_diagrams',[]),layer['regions'])
    for fraction in layer.get('fractions',[]):
        box(fraction.get('box'));box(fraction.get('bar_box'))
        sha(fraction.get('bar_pixels_sha256'))
        members=[fraction.get(role+'_region_id') for role in ('numerator','denominator','operator')]
        if len(set(members))!=3 or any(identity not in ids for identity in members):
            raise ValueError('invalid positioned fraction ownership')
        found={r['id']:r for r in layer['regions']}
        if (found[members[2]]['text']!='/' or any(not re.fullmatch('[0-9]+',found[identity]['text']) for identity in members[:2])):
            raise ValueError('positioned numeric fraction text changed')
    annotations=layer.get('annotations',[])
    if not isinstance(annotations,list) or len(annotations)>1000:raise ValueError('invalid source annotations')
    annotation_ids=set()
    for annotation in annotations:
        if not isinstance(annotation,dict):raise ValueError('invalid source annotation')
        identity=annotation.get('id');original=annotation.get('original_region')
        if (not isinstance(identity,str) or identity in ids or identity in annotation_ids
                or annotation.get('classification') not in {'handwritten-annotation','marginal-mark'}
                or annotation.get('status')!='unresolved' or annotation.get('literal_text') is not None):
            raise ValueError('source annotation loses original recognition evidence')
        if original is not None:
            if (not isinstance(original,dict) or original.get('id')!=identity or not isinstance(original.get('text'),str)
                    or annotation.get('box')!=original.get('box') or annotation.get('quad')!=original.get('quad')):
                raise ValueError('source annotation loses original recognition evidence')
        elif (not re.fullmatch(r'a[0-9a-f]{16}',identity) or annotation.get('confidence')!=0.
                or annotation.get('recognition_status')!='source-reviewed-not-model-recognized'):
            raise ValueError('unrecognized source annotation lacks explicit review identity')
        box(annotation['box']);quad(annotation['quad']);proof=annotation.get('evidence',{})
        if not isinstance(proof,dict):raise ValueError('annotation lacks original-source evidence')
        sha(proof.get('crop_sha256'))
        if not isinstance(proof.get('reason'),str) or not proof['reason'].strip():raise ValueError('annotation lacks original-source review')
        annotation_ids.add(identity)
    content = {k: v for k, v in layer.items() if k != "generation"}
    if digest(content) != layer["generation"]:
        raise ValueError("text generation mismatch")
    return layer


def review_task(layer, text_resource, evidence=None):
    validate(layer)
    if not layer["review_flags"]:
        return None
    body = {"source_sha256": layer["source_sha256"], "page": layer["page"],
            "page_identity": layer["page_identity"], "raw_sha256": layer["raw_sha256"],
            "base_generation": layer["generation"], "issues": layer["review_flags"],
            "region_ids": [r["id"] for r in layer["regions"]], "text_layer": text_resource,
            "evidence": evidence or [], "status": "pending"}
    return {"id": digest(body), **body}


def accept_proposal(layer, proposal, *, actor):
    """Explicit trusted acceptance; proposals never mutate their raw baseline."""
    validate(layer)
    if not isinstance(actor, str) or not actor.strip():
        raise ValueError("acceptance actor required")
    if (proposal.get("kind") != "pdf-text-correction" or proposal.get("version") != 1
            or proposal.get("base_generation") != layer["generation"]
            or proposal.get("raw_sha256") != layer["raw_sha256"]
            or proposal.get("page_identity") != layer["page_identity"]):
        raise ValueError("stale or mismatched correction")
    replacements = proposal.get("replacements")
    if not isinstance(replacements,list) or any(not isinstance(row,dict) for row in replacements):
        raise ValueError('invalid correction replacements')
    additions = proposal.get('additions', [])
    exclusions = proposal.get('exclusions', [])
    annotations = proposal.get('annotations', [])
    annotation_additions = proposal.get('annotation_additions', [])
    separators = proposal.get('separators', [])
    relations = proposal.get('formula_relations', [])
    continuations = proposal.get('formula_continuations', [])
    errata = proposal.get('errata_relations', [])
    if not isinstance(relations,list):raise ValueError('invalid formula relations')
    if not isinstance(continuations,list):raise ValueError('invalid formula continuations')
    if not isinstance(errata,list):raise ValueError('invalid printed errata relations')
    if (not isinstance(additions,list) or not isinstance(exclusions,list) or not isinstance(annotations,list)
            or not isinstance(separators,list)
            or not isinstance(annotation_additions,list)
            or any(not isinstance(row,dict) for row in additions+exclusions+annotations+separators+annotation_additions)):
        raise ValueError('invalid positioned correction scope')
    positioned = bool(additions or exclusions or annotations or annotation_additions or separators or relations or continuations or errata or any('geometry' in row for row in replacements or []))
    visual_review = proposal.get('visual_review', {})
    if positioned:
        if (not isinstance(visual_review, dict) or visual_review.get('kind') != 'source-image-review'
                or visual_review.get('source_sha256') != layer['source_sha256']
                or visual_review.get('page') != layer['page']
                or not isinstance(visual_review.get('basis'), str) or not visual_review['basis'].strip()):
            raise ValueError('positioned correction requires exact-source visual review')
        sha(visual_review.get('input_png_sha256'))
    if len(additions)+len(exclusions)+len(annotations)+len(annotation_additions)+len(separators)>1000:
        raise ValueError('invalid positioned correction scope')
    requested_order = proposal.get("region_order", [])
    original_order = [r["id"] for r in layer["regions"]]
    known = {r['id']: r for r in layer['regions']}
    excluded = {}
    separated = {}
    def evidence(item):
        proof = item.get('evidence')
        if (not isinstance(proof, dict) or not isinstance(proof.get('reason'), str)
                or not proof['reason'].strip()):
            raise ValueError('positioned change lacks source evidence')
        sha(proof.get('crop_sha256'))
        return copy.deepcopy(proof)
    def reviewed_geometry(bounds,polygon):
        bounds=box(copy.deepcopy(bounds));polygon=quad(copy.deepcopy(polygon))
        if any(not bounds[0]-1e-8<=x<=bounds[2]+1e-8 or not bounds[1]-1e-8<=y<=bounds[3]+1e-8
               for x,y in polygon):
            raise ValueError('reviewed quadrilateral is outside its source box')
        return bounds,polygon
    for item in exclusions:
        target = item.get('region_id')
        if (target not in known or target in excluded or item.get('before') != known[target]['text']
                or item.get('classification') not in {'map-geometry', 'illustration-geometry', 'scan-artifact'}):
            raise ValueError('invalid reviewed nontext exclusion')
        excluded[target] = {'region':copy.deepcopy(known[target]), 'classification':item['classification'],
                            'evidence':evidence(item)}
    for item in annotations:
        target=item.get('region_id')
        if (target not in known or target in excluded or target in separated
                or item.get('before')!=known[target]['text']
                or item.get('classification') not in {'handwritten-annotation','marginal-mark'}
                or item.get('status')!='unresolved' or item.get('literal_text') is not None):
            raise ValueError('invalid reviewed source annotation')
        region=known[target]
        separated[target]={'id':target,'classification':item['classification'],'status':'unresolved','literal_text':None,
            'box':copy.deepcopy(region['box']),'quad':copy.deepcopy(region['quad']),
            'original_region':copy.deepcopy(region),'evidence':evidence(item)}
    new_annotations = {}
    existing_annotations = {a['id'] for a in layer.get('annotations',[])}
    for item in annotation_additions:
        identity=item.get('id')
        if (not isinstance(identity,str) or not re.fullmatch(r'a[0-9a-f]{16}',identity)
                or identity in known or identity in new_annotations or identity in existing_annotations
                or item.get('classification') not in {'handwritten-annotation','marginal-mark'}
                or item.get('status')!='unresolved' or item.get('literal_text') is not None
                or set(item)-{'id','classification','status','literal_text','box','quad','evidence'}):
            raise ValueError('invalid source-positioned annotation addition')
        bounds,polygon=reviewed_geometry(item.get('box'),item.get('quad'))
        new_annotations[identity]={'id':identity,'classification':item['classification'],
            'status':'unresolved','literal_text':None,'box':bounds,'quad':polygon,'confidence':0.,
            'recognition_status':'source-reviewed-not-model-recognized','evidence':evidence(item)}
    added = {}
    for item in additions:
        identity = item.get('region_id')
        if (not isinstance(identity, str) or not re.fullmatch(r'v[0-9a-f]{16}', identity)
                or identity in known or identity in added or not isinstance(item.get('text'), str)
                or not 1<=len(item['text'])<=10000 or item.get('writing_mode', 'horizontal-ltr') not in
                    {'horizontal-ltr','horizontal-rtl','vertical-lr','vertical-rl'}):
            raise ValueError('invalid source-positioned addition')
        bounds,polygon=reviewed_geometry(item.get('box'),item.get('quad'))
        proof=evidence(item)
        added[identity]={'id':identity,'text':item['text'],'box':bounds,'quad':polygon,
            'mapping_precision':'corrected-block','source':'ocr','confidence':0.,
            'language_hint':item.get('language_hint', layer['provenance']['language_hint']),
            'scripts':scripts(item['text']),'direction':direction(item['text']),
            'writing_mode':item.get('writing_mode','horizontal-ltr'),'layout_region':item.get('layout_region',0),
            'visual_review_evidence':proof,'recognition_status':'source-reviewed-not-model-recognized'}
    table_members={identity for table in layer.get('tables',[]) for cell in table['cells'] for identity in cell['region_ids']}
    structural_members=set(table_members)
    structural_members.update(identity for graph in layer.get('flow_diagrams',[]) for node in graph['nodes'] for identity in node['region_ids'])
    structural_members.update(fraction[role+'_region_id'] for fraction in layer.get('fractions',[]) for role in ('numerator','denominator','operator'))
    if structural_members.intersection(set(excluded)|set(separated)):
        raise ValueError('reviewed exclusion cannot silently remove a table member')
    removed=set(excluded)|set(separated)
    expected_order=(set(original_order)-removed)|set(added)
    if (not isinstance(requested_order, list) or any(not isinstance(i, str) for i in requested_order)
            or requested_order and (len(requested_order) != len(expected_order) or set(requested_order) != expected_order)
            or (added or removed) and not requested_order):
        raise ValueError("correction order must name every region exactly once")
    reorder = bool(requested_order and requested_order != original_order)
    if not isinstance(replacements, list) or not replacements and not reorder and not relations and not continuations and not errata and not separators and not new_annotations:
        raise ValueError("empty correction proposal")
    # Source-reviewed line/space corrections are independent from glyph text.
    # Require the exact original adjacent pair and gap so no hidden text can
    # be removed, invented or moved by an apparently harmless separator edit.
    final_order = requested_order or original_order
    separator_changes = {}
    for item in separators:
        left,right = item.get('after_region_id'),item.get('before_region_id')
        before,after = item.get('before'),item.get('after')
        if (not isinstance(left,str) or not isinstance(right,str)
                or left not in known or right not in known or right in separator_changes
                or left in removed or right in removed
                or original_order.index(right) != original_order.index(left)+1
                or final_order.index(right) != final_order.index(left)+1
                or not isinstance(before,str) or not isinstance(after,str) or before == after
                or len(before)>64 or len(after)>64
                or any(c not in ' \t\r\n' for c in before+after)
                or layer['text'][known[left]['end']:known[right]['start']] != before):
            raise ValueError('stale or invalid source-reviewed separator')
        separator_changes[right] = {'after_region_id':left,'before_region_id':right,
            'before':before,'after':after,'evidence':evidence(item)}
    changes = {}
    geometry = {}
    for item in replacements:
        target = item.get("region_id")
        if (target not in known or target in removed or target in changes or item.get("before") != known[target]["text"]
                or not isinstance(item.get("after"), str) or not item["after"]
                or len(item["after"]) > 10000):
            raise ValueError("invalid correction region or text")
        changes[target] = item["after"]
        if 'geometry' in item:
            update=item['geometry']
            if (not isinstance(update,dict) or update.get('before_box')!=known[target]['box']
                    or update.get('before_quad')!=known[target]['quad'] or target in table_members):
                raise ValueError('stale or unsupported reviewed geometry')
            bounds,polygon=reviewed_geometry(update.get('box'),update.get('quad'))
            geometry[target]={'box':bounds,'quad':polygon,
                              'visual_review_evidence':evidence(item)}
    result = copy.deepcopy(layer)
    if reorder:
        cursor = 0
        for region in layer["regions"]:
            if layer["text"][cursor:region["start"]].strip():
                raise ValueError("cannot reorder text with unpositioned content")
            cursor = region["end"]
        if layer["text"][cursor:].strip():
            raise ValueError("cannot reorder text with unpositioned content")
        by_id = {r["id"]: r for r in result["regions"] if r['id'] not in removed}
        by_id.update(added)
        result["regions"] = [by_id[identity] for identity in requested_order]
    parts, position, length = [], 0, 0
    original_predecessors={identity:original_order[at-1] if at else None
                           for at,identity in enumerate(original_order)}
    for index, region in enumerate(result["regions"]):
        old_start, old_end = region.get('start',0), region.get('end',0)
        gap = ("\n" if index else "") if reorder else layer["text"][position:old_start]
        if positioned and reorder and region['id'] in known:
            predecessor=original_predecessors[region['id']]
            previous=result['regions'][index-1]['id'] if index else None
            if predecessor==previous:
                previous_end=known[predecessor]['end'] if predecessor is not None else 0
                gap=layer['text'][previous_end:old_start]
        if region['id'] in separator_changes:
            gap=separator_changes[region['id']]['after']
        parts.append(gap)
        length += len(gap)
        if region["id"] in changes:
            region["text"] = changes[region["id"]]
            region["mapping_precision"] = "corrected-block"
            region["scripts"] = scripts(region["text"])
            region["direction"] = direction(region["text"])
        if region['id'] in geometry:
            region.update(geometry[region['id']])
        region["start"] = length
        region["order"] = index
        length += len(region["text"])
        region["end"] = length
        parts.append(region["text"])
        position = old_end
    if not reorder:
        parts.append(layer["text"][position:])
    elif positioned and result['regions'] and result['regions'][-1]['id']==original_order[-1]:
        parts.append(layer['text'][known[original_order[-1]]['end']:])
    result["text"] = "".join(parts)
    if separated or new_annotations:
        result.setdefault('annotations',[]).extend(copy.deepcopy([*separated.values(),*new_annotations.values()]))
        result['review_flags']=sorted(set(result['review_flags'])|{'unresolved-source-annotation'})
    if relations:
        combined=[*result.get('formula_relations',[]),*copy.deepcopy(relations)]
        validate_formula_relations(combined,result['regions'])
        result['formula_relations']=combined
    if continuations:
        combined=[*result.get('formula_continuations',[]),*copy.deepcopy(continuations)]
        validate_formula_continuations(combined,result['regions'],result['text'])
        result['formula_continuations']=combined
    if errata:
        combined=[*result.get('errata_relations',[]),*copy.deepcopy(errata)]
        validate_errata_relations(combined,result['regions'])
        result['errata_relations']=combined
    result.update(revision="accepted", quality="partially-reviewed", parent_generation=layer["generation"])
    result.setdefault("acceptances", []).append({"proposal_sha256": digest(proposal), "actor": actor,
                                                 "region_ids": sorted(changes),
                                                 **({'positioned_additions':copy.deepcopy(list(added.values())),
                                                      'excluded_regions':copy.deepcopy(list(excluded.values())),
                                                       **({'separated_annotations':copy.deepcopy(list(separated.values()))} if separated else {}),
                                                       **({'source_positioned_annotations':copy.deepcopy(list(new_annotations.values()))} if new_annotations else {}),
                                                      'geometry_regions':sorted(geometry),
                                                      'geometry_changes':[{'region_id':identity,
                                                          'original_region':copy.deepcopy(known[identity]),
                                                          'updated_geometry':copy.deepcopy(geometry[identity])}
                                                          for identity in sorted(geometry)],
                                                       'formula_relation_ids':[r['id'] for r in relations],
                                                       **({'formula_continuation_ids':[r['id'] for r in continuations]} if continuations else {}),
                                                       **({'errata_relation_ids':[r['id'] for r in errata]} if errata else {}),
                                                       **({'separator_changes':copy.deepcopy(list(separator_changes.values()))} if separator_changes else {}),
                                                     'visual_review':copy.deepcopy(visual_review)} if positioned else {}),
                                                **({"region_order": requested_order} if reorder else {})})
    result["generation"] = digest({k: v for k, v in result.items() if k != "generation"})
    return validate(result)

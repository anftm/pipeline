"""Repair misplaced volume entries with two independent navigation signals."""

from collections import Counter, defaultdict
from urllib.parse import unquote, urlsplit
import xml.etree.ElementTree as ET


COVER_LABELS = {'cover', '封面', '封皮', '封面页'}
FRONT_MATTER_LIMIT = 8


def _tag(node):
    return node.tag.rsplit('}', 1)[-1].lower() if isinstance(node.tag, str) else ''


def _label(text):
    return ' '.join(text.split()).casefold()


def repair_volume_targets(entries, documents, spine_order, resolve):
    """Return exact path/fragment repairs, leaving unrelated entries untouched.

    ``documents`` maps spine paths to sanitized XHTML; ``resolve`` resolves an
    href to a path relative to the package root. An inline contents page near
    the start must name multiple distinct top-level entries. Its unique links
    and the first independent child of a group must corroborate the new target.
    An existing target within the group is not overridden, except to refine an
    unanchored link to the same page.
    """
    if not entries or entries[0]['depth'] != 0:
        return entries
    titles = Counter(_label(entry['title']) for entry in entries if entry['depth'] == 0)
    if len(titles) < 2:
        return entries
    roots, anchors, link_cache = {}, {}, {}

    def parsed(path):
        if path not in roots:
            try:
                roots[path] = ET.fromstring(documents.get(path, ''))
            except ET.ParseError:
                roots[path] = None
        return roots[path]

    def local_links(path):
        if path in link_cache:
            return link_cache[path]
        result = defaultdict(set)
        root = parsed(path)
        if root is not None:
            for node in root.iter():
                link = node.get('href')
                if _tag(node) != 'a' or not link:
                    continue
                try:
                    parts = urlsplit(link)
                    if parts.scheme or parts.netloc or parts.query or not (parts.path or parts.fragment):
                        continue
                    target = resolve(path, link) if parts.path else path
                    fragment = unquote(parts.fragment)
                except ValueError:
                    continue
                result[_label(''.join(node.itertext()))].add((target, fragment))
        link_cache[path] = result
        return result

    def usable(path, fragment):
        if path not in spine_order:
            return False
        if not fragment:
            return True
        if path not in anchors:
            root = parsed(path)
            anchors[path] = Counter(value for node in (() if root is None else root.iter())
                                    for value in {node.get('id'), node.get('name')} if value)
        return anchors[path][fragment] == 1

    opening = max(1, min(FRONT_MATTER_LIMIT, spine_order.get(entries[0]['source_path'], 1)))
    links = defaultdict(set)
    best_score = (0, 0)
    for path, position in spine_order.items():
        if position > opening:
            continue
        matches = {label: targets for label, targets in local_links(path).items()
                   if titles[label] == 1 and any(usable(*target) for target in targets)}
        if len(matches) < 2:
            continue
        # Prefer the page with the most explicit, distinct top-level links.
        # An old conversion may also have generated a plain-text list linked
        # to its own broken sidebar, which is not independent evidence.
        score = (sum(len(values) == 1 and bool(next(iter(values))[1]) for values in matches.values()),
                 len(matches))
        if score < best_score:
            continue
        if score > best_score:
            links.clear()
            best_score = score
        for label, targets in matches.items():
            links[label].update(targets)

    repaired = list(entries)
    for i in range(1, len(entries) - 1):
        entry = entries[i]
        title = _label(entry['title'])
        targets = links.get(title, ())
        if entry['depth'] != 0 or titles[title] != 1 or len(targets) != 1:
            continue
        target, fragment = next(iter(targets))
        previous = spine_order.get(repaired[i - 1]['source_path'])
        if previous is None or not usable(target, fragment) or spine_order[target] <= previous:
            continue
        original = entry['source_path'], entry['fragment']
        refine_anchor = original == (target, '') and bool(fragment)
        if (not refine_anchor and usable(*original)
                and previous <= spine_order[original[0]] <= spine_order[target]):
            continue

        # A generated, empty cover may alias the broken group target. It isn't
        # independent evidence. Repoint it only if the independently located
        # next sibling actually confirms the same physical title/start page.
        child_index = i + 1
        child = entries[child_index]
        alias = (child['depth'] == 1 and _label(child['title']) in COVER_LABELS
                 and (child['source_path'], child['fragment']) == original)
        if alias:
            child_index += 1
            if child_index >= len(entries):
                continue
            child = entries[child_index]
        child_path, child_fragment = child['source_path'], child['fragment']
        if child['depth'] != 1 or not usable(child_path, child_fragment):
            continue
        if child_path != target:
            # Some collections link first to a dedicated volume contents page;
            # its exact link to the independent child corroborates that page.
            if (spine_order[child_path] < spine_order[target]
                    or local_links(target).get(_label(child['title'])) != {(child_path, child_fragment)}):
                continue
        repaired[i] = dict(entry, source_path=target, fragment=fragment)
        if alias:
            repaired[i + 1] = dict(entries[i + 1], source_path=target, fragment=fragment)
    return repaired

"""Pinned empty targets for normal BHA index delivery, including reconciliation."""
try:
    from . import bha_empty_archive_proof as proof
except ImportError:
    import bha_empty_archive_proof as proof


def source(archive, commit, tree, github_token, *, fetch=proof.read_json):
    if (type(archive) is not int or not 0 <= archive <= 31
            or not isinstance(commit, str) or not proof.SHA.fullmatch(commit)
            or not isinstance(tree, str) or not proof.SHA.fullmatch(tree)):
        raise ValueError('invalid empty target identity')
    base = f'https://api.github.com/repos/{proof.revisions.OWNER}/banned-historical-archives{archive}/git'
    value = fetch(base + '/commits/' + commit, github_token, proof.MAX_PROOF)
    if value.get('sha') != commit or value.get('tree', {}).get('sha') != tree:
        raise ValueError('empty target commit/tree binding differs')
    inventory = proof.empty_tree(fetch(base + '/trees/' + tree + '?recursive=1', github_token, proof.MAX_TREE), tree)
    return {'archive': archive, 'commit': commit, **inventory}


def validate_sources(rows, commits, trees):
    if not isinstance(rows, list) or len(rows) > 32:
        raise ValueError('invalid empty source proofs')
    archives = set()
    for row in rows:
        if not isinstance(row, dict):raise ValueError('invalid empty source proof')
        archive = row.get('archive');key = str(archive)
        if (type(archive) is not int or key not in commits or archive in archives
                or row.get('commit') != commits[key] or row.get('tree') != trees[key]
                or type(row.get('article_json_files')) is not int or row['article_json_files'] != 0
                or type(row.get('tag_files')) is not int or row['tag_files'] != 0
                or type(row.get('entries')) is not int or row['entries'] < 0
                or type(row.get('metadata_files')) is not int or not 0 <= row['metadata_files'] <= row['entries']
                or not isinstance(row.get('inventory_sha256'), str)
                or not proof.SHA256.fullmatch(row['inventory_sha256'])):
            raise ValueError('empty source proof does not bind exact target')
        archives.add(archive)
    return archives


def validate_serving(rows, commits, trees, fingerprint):
    archives = validate_sources(rows, commits, trees)
    for row in rows:
        serving = row.get('serving')
        if (not isinstance(serving, dict) or serving.get('kind') != 'bha-archive-cardinality'
                or type(serving.get('archive')) is not int or serving['archive'] != row['archive']
                or serving.get('archive_revisions') != trees or serving.get('build_fingerprint') != fingerprint
                or serving.get('all_counts_equal') is not True
                or type(serving.get('remote_writes')) is not int or serving['remote_writes'] != 0
                or not isinstance(serving.get('counts'), dict)
                or set(serving['counts']) != {'elasticsearch','docs','facets'}
                or any(type(v) is not int or v != 0 for v in serving['counts'].values())):
            raise ValueError('empty serving proof differs from exact ready target')
    return archives


def observe(record, archives, github_token, admin_token, *, fetch=proof.read_json, status=proof.access.read_status):
    before = proof.status_identity(status(admin_token))
    if before['archive_revisions'] != record['archive_revisions']:
        raise ValueError('empty archive target is not serving')
    rows = []
    for archive in archives:
        key = str(archive)
        row = source(archive, record['archive_commits'][key], record['archive_revisions'][key], github_token, fetch=fetch)
        row['serving'] = fetch(proof.BASE + '/api/reindex/archive/' + key, admin_token, proof.MAX_PROOF)
        rows.append(row)
    validate_serving(rows, record['archive_commits'], record['archive_revisions'], before['build_fingerprint'])
    if proof.status_identity(status(admin_token)) != before:
        raise ValueError('empty archive serving state changed')
    return rows

"""Run an exact audit-only producer migration publication from a locked workflow.

The workflow must arrange concurrency with maintenance and Reader writers. This
entrypoint cannot enable a scope, modify any live root, delete an object, or
revoke a token. It re-reads every pinned input and creates only immutable audit
objects. External HF/S3 writers are outside the Actions concurrency boundary.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re

try:
    from . import reader_producer_audit_publish as publication
    from . import reader_producer_audit_store as storage
    from . import reader_producer_transition_state as model
except ImportError:
    import reader_producer_audit_publish as publication
    import reader_producer_audit_store as storage
    import reader_producer_transition_state as model


ROOT = Path(__file__).resolve().parents[1]
INPUTS = ROOT / 'config/producer-migration-audit-20261103'
WORKFLOW_REF = 'anftm/pipeline/.github/workflows/producer-migration-audit.yml@refs/heads/main'
CANDIDATE_SHA = 'ae99a54aa55df5865ce1d9389cb78bf9921e42758cc403179035304a4d154c97'
WRITER_SHA = '2b3d64397bf0068c258b9a57e77129a58eb100d52f0870dcefaff604bf6d3d62'
INTENT_SHA = '1785db6b5d9322a6e3d2259c3abd0ff1e9ccc0e3abeced40a360a8cc3f2b1b09'
EXPECTED_RECEIPTS = {
    '283dad71c9cd4d28c1d1ead8bc1ce5164a9eb2fd6ed367cb96da3adc01bb682a',
    '5d4ae8c93c97e7a02f8f040298b81a0ef5d0e120fbaaf53290b1878cfc7c5381',
    '600726404e3f0fee2182af76ad8cecdb7117d291d468eb6223632d1102a3b1cf',
    '61051315aefe28e4cb489efdf16d7e86a0b4c0e303f50694fdded4dee52c0877',
    '7fa3585dcfee8e70b9061e1fd3435838bbb6a3bfeb32a4727d70c02588df08df',
    '97f2b7a2a5ab52066795b2c421f14bbf2e58d219ece7c50cb1e7ccd6a37cd05c',
    'bd9fc58b106d3e2755bfb2856427f9d4e2c8af5d995e049a070329667e070a56',
    'c7f231c8cfbd9f54d95df15c0a2ea8da5fe47988703bf22ef32158527e37db96',
    'cd8643e0e92bafbc6c6787d414e0945a301e42f13fe05704877eccc93e468a37',
}


def require_context():
    if (os.environ.get('GITHUB_ACTIONS') != 'true' or os.environ.get('GITHUB_REPOSITORY') != 'anftm/pipeline'
            or os.environ.get('GITHUB_WORKFLOW_REF') != WORKFLOW_REF
            or not re.fullmatch(r'[1-9][0-9]*', os.environ.get('GITHUB_RUN_ID', ''))):
        raise RuntimeError('producer migration audit requires its locked central workflow')


def load_inputs(root=INPUTS):
    root = Path(root)
    expected = {'candidate.json', 'writer-inventory.json', 'intent.json', 'receipts'}
    if not root.is_dir() or {p.name for p in root.iterdir()} != expected:
        raise ValueError('producer audit input directory differs')
    def fixed(name, sha):
        path = root / name
        if not path.is_file() or path.is_symlink():
            raise ValueError('producer audit input file invalid')
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != sha:
            raise ValueError('producer audit input digest differs: ' + name)
        return raw
    candidate_raw = fixed('candidate.json', CANDIDATE_SHA)
    writer_raw = fixed('writer-inventory.json', WRITER_SHA)
    intent_raw = fixed('intent.json', INTENT_SHA)
    directory = root / 'receipts'
    if not directory.is_dir() or {x.name for x in directory.iterdir()} != {x + '.json' for x in EXPECTED_RECEIPTS}:
        raise ValueError('producer audit receipt inventory differs')
    raws = {}
    for sha in EXPECTED_RECEIPTS:
        path = directory / (sha + '.json')
        if not path.is_file() or path.is_symlink():
            raise ValueError('producer audit receipt file invalid')
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != sha:
            raise ValueError('producer audit receipt digest differs')
        value = json.loads(raw, object_pairs_hook=model.scope.unique_object)
        repo = value.get('repository') if isinstance(value, dict) else None
        if not isinstance(repo, str) or repo in raws:
            raise ValueError('producer audit receipt identity differs')
        raws[repo] = raw
    candidate = json.loads(candidate_raw, object_pairs_hook=model.scope.unique_object)
    planned = publication.intent.plan(candidate, raws, writer_raw)
    if planned['candidate_sha256'] != CANDIDATE_SHA or planned['intent_sha256'] != INTENT_SHA:
        raise ValueError('producer audit candidate or intent differs')
    intent_path = publication.intent.PREFIX + 'intents/' + INTENT_SHA + '.json'
    if planned['objects'][intent_path] != intent_raw:
        raise ValueError('producer audit intent bytes differ')
    return candidate, raws, writer_raw


def run(store, root=INPUTS):
    require_context()
    candidate, raws, writer = load_inputs(root)
    result = publication.publish_candidate(candidate, raws, writer, read=store.read, create=store.create)
    if (result['candidate_sha256'] != CANDIDATE_SHA or result['intent_sha256'] != INTENT_SHA
            or result['scope_activated'] is not False or result['gc_authorized'] is not False):
        raise RuntimeError('producer audit publication outcome differs')
    return result


def main():
    require_context()
    # Validate inputs before constructing the remote client or making a request.
    load_inputs()
    result = run(storage.AuditStore())
    path = Path('output/producer-migration-audit/result.json')
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('x', encoding='utf-8') as stream:
        json.dump(result, stream, ensure_ascii=False, sort_keys=True, indent=2)
        stream.write('\n')
    print(json.dumps(result, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()

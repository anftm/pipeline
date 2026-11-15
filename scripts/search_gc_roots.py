"""Read current site build pins, configured defaults and actual loaded roots.

Only fixed public source paths and the existing deployment configuration APIs
are read. Historical Git commits are not retention roots. Failure is fatal to
GC; an unknown consumer must never be treated as an empty reference set.
"""
from __future__ import annotations

import json
import os
import re
import urllib.request

SHA = re.compile(r'[0-9a-f]{40}\Z')
GEN = re.compile(r'[0-9a-f]{64}\Z')
PAGES_API = 'https://api.github.com/repos/vomebook/search'
SPACE = 'VoiceOfML/Search'


def generation(value):
    if not isinstance(value, str) or not GEN.fullmatch(value):
        raise ValueError('invalid live GC consumer generation')
    return value


def public_get(url):
    request = urllib.request.Request(url, headers={'Cache-Control': 'no-cache', 'User-Agent': 'search-gc-roots/1'})
    with urllib.request.urlopen(request, timeout=25) as response:
        raw = response.read(1024 * 1024 + 1)
    if len(raw) > 1024 * 1024:
        raise ValueError('oversized GC consumer metadata')
    return raw


def collect(*, session, api, get=public_get):
    def config():
        response = session.get(PAGES_API + '/git/ref/heads/main', timeout=20)
        response.raise_for_status()
        pages_sha = response.json()['object']['sha']
        space_sha = api.space_info(SPACE, timeout=20).sha
        if not all(isinstance(s, str) and SHA.fullmatch(s) for s in (pages_sha, space_sha)):
            raise ValueError('invalid exact consumer revision')
        response = session.get(PAGES_API + '/actions/variables/SEARCH_DATA_GENERATION', timeout=20)
        response.raise_for_status()
        value = response.json()
        if value.get('name') != 'SEARCH_DATA_GENERATION':
            raise ValueError('invalid Pages data default')
        variables = api.get_space_variables(SPACE)
        space_default = getattr(variables.get('SEARCH_DATA_GENERATION'), 'value', None)
        return {'pages_sha': pages_sha, 'space_sha': space_sha,
                'pages_default': generation(value.get('value')),
                'space_default': generation(space_default)}

    def loaded():
        pages = json.loads(get('https://vomebook.github.io/search/data/generation-manifest.json'))
        space = json.loads(get('https://voiceofml-search.hf.space/api/search-data-generation'))
        if (not isinstance(pages, dict) or not isinstance(space, dict)
                or pages.get('kind') != 'search-snapshot-local' or type(pages.get('version')) is not int
                or pages['version'] != 1 or type(pages.get('record_count')) is not int or pages['record_count'] < 1
                or not isinstance(pages.get('files'), dict) or set(pages['files']) != {'pages', 'space'}
                or not isinstance(pages.get('objects'), dict) or set(pages['objects']) != {'pages', 'space'}
                or space.get('verified') is not True or space.get('surface') != 'space'
                or type(space.get('records')) is not int or space['records'] < 1
                or type(space.get('files')) is not int or space['files'] < 1
                or not isinstance(space.get('api_generation'), str) or not space['api_generation'].startswith('api:')):
            raise ValueError('consumer generation is not verified')
        for group in ('files', 'objects'):
            for surface in ('pages', 'space'):
                members = pages[group][surface]
                if not isinstance(members, dict) or not members or not isinstance(members.get('search_data.json.gz'), dict):
                    raise ValueError('invalid loaded Pages surface members')
        return {'pages_loaded': generation(pages.get('generation')),
                'space_loaded': generation(space.get('generation'))}

    before = config()
    actual = loaded()
    pages_config = json.loads(get('https://raw.githubusercontent.com/vomebook/search/' +
                                 before['pages_sha'] + '/config/search-data.json'))
    pins = {'pages-test-default': generation(pages_config.get('generation'))}
    prefix = 'https://huggingface.co/spaces/' + SPACE + '/resolve/' + before['space_sha'] + '/'
    docker = get(prefix + 'Dockerfile').decode('utf8')
    prepare = get(prefix + 'scripts/data/prepare_test_data.mjs').decode('utf8')
    logical_docker = re.sub(r'\\\r?\n', ' ', docker)
    relevant = [line for line in logical_docker.splitlines()
                if re.match(r'^\s*(?:ARG|ENV)\b', line, re.I) and 'SEARCH_DATA_GENERATION' in line]
    declarations = re.findall(r'^ARG SEARCH_DATA_GENERATION=["\']?([0-9a-f]{64})["\']?\s*$', docker, re.M)
    defaults = re.findall(r'^export const DEFAULT_GENERATION = ["\']([0-9a-f]{64})["\'];\s*$', prepare, re.M)
    if len(relevant) != 1 or len(declarations) != 1 or len(defaults) != 1:
        raise ValueError('unknown or ambiguous Space build/test generation declarations')
    pins['space-build-default'] = generation(declarations[0])
    pins['space-test-default'] = generation(defaults[0])
    if config() != before or loaded() != actual:
        raise ValueError('consumer configuration or loaded roots changed during GC observation')
    return {'revisions': {k: before[k] for k in ('pages_sha', 'space_sha')},
            'generations': {**pins, **{k: before[k] for k in ('pages_default', 'space_default')}, **actual}}


def live_roots():
    import requests
    from huggingface_hub import HfApi
    token = os.environ.get('PAGES_TOKEN')
    if not token:
        raise ValueError('PAGES_TOKEN required to verify configured GC roots')
    session = requests.Session()
    session.headers.update({'Authorization': 'Bearer ' + token, 'Accept': 'application/vnd.github+json'})
    try:
        return collect(session=session, api=HfApi(token=os.environ.get('HF_TOKEN')))
    finally:
        session.close()

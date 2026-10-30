"""Historical repositories to fence during Reader maintenance, not OCR routing."""
import json
import re
from pathlib import Path

SCOPE = Path(__file__).resolve().parents[1] / 'config/producer-isolation-scope.json'
REPOSITORY = re.compile(r'[A-Za-z0-9-]+/[A-Za-z0-9._-]+')


def load_config(path=SCOPE):
    value = json.loads(Path(path).read_text(encoding='utf-8'))
    repos = value.get('repositories')
    central = value.get('publisher_repository')
    if (value.get('version') != 1 or value.get('kind') != 'reader-producer-isolation-scope'
            or value.get('status') != 'isolation-only' or not isinstance(repos, list) or not repos
            or any(not isinstance(repo, str) or not REPOSITORY.fullmatch(repo) for repo in repos)
            or len({repo.lower() for repo in repos}) != len(repos)
            or not isinstance(central, str) or central.lower() not in {repo.lower() for repo in repos}):
        raise ValueError('invalid producer isolation scope')
    return value

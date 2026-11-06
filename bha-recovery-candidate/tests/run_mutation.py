"""Run a reproducible, scoped mutmut study without mutating the checkout.

Only app/parse_core.py is mutated. The strict score counts killed/total,
including mutants without tests in the denominator; survivors are reported.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from importlib.metadata import version
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INPUTS = ["pyproject.toml", "requirements-mutation.txt", "tests/test_parse_core.py", "tests/run_mutation.py",
          *(f"app/{path.name}" for path in sorted((ROOT / "app").glob("*.py")))]


def evaluate(stats: dict[str, int], minimum: float) -> float:
    total = stats["total"]
    if not total or any(stats[key] for key in ("suspicious", "timeout", "skipped", "check_was_interrupted_by_user", "segfault")):
        raise SystemExit(f"incomplete or invalid mutation run: {stats}")
    score = 100 * stats["killed"] / total
    if score < minimum:
        raise SystemExit(f"mutation score {score:.2f}% is below {minimum:.2f}%")
    return score


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, help="write JSON evidence outside the checkout")
    parser.add_argument("--min-score", type=float, default=0.0, help="minimum killed/total percentage (default: report only)")
    parser.add_argument("--max-children", type=int, default=2)
    args = parser.parse_args()
    if not 0 <= args.min_score <= 100 or args.max_children < 1:
        parser.error("min-score must be 0..100 and max-children must be positive")
    installed = {name: version(name) for name in ("mutmut", "pytest")}
    if installed != {"mutmut": "3.8.1", "pytest": "9.1.1"}:
        raise SystemExit(f"unexpected mutation tool versions: {installed}")
    hashes = {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in INPUTS}
    with tempfile.TemporaryDirectory(prefix="bha-mutmut-") as directory:
        stage = Path(directory)
        for name in INPUTS:
            target = stage / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / name, target)
        (stage / "tests/__init__.py").touch()
        env = {**os.environ, "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"}
        # No app main import or production data is needed for the chosen suite.
        subprocess.run([sys.executable, "-m", "mutmut", "run", "--max-children", str(args.max_children)], cwd=stage, env=env, check=True)
        subprocess.run([sys.executable, "-m", "mutmut", "export-cicd-stats"], cwd=stage, env=env, check=True)
        stats = json.loads((stage / "mutants/mutmut-cicd-stats.json").read_text())
        findings = subprocess.run([sys.executable, "-m", "mutmut", "results"], cwd=stage, env=env,
                                  text=True, capture_output=True, check=True).stdout
    for name, digest in hashes.items():
        if hashlib.sha256((ROOT / name).read_bytes()).hexdigest() != digest:
            raise SystemExit(f"input changed during mutation run: {name}")
    score = evaluate(stats, 0)
    report = {"scope": "app/parse_core.py", "test_suite": "tests/test_parse_core.py", "versions": installed,
              "source_sha256": hashes["app/parse_core.py"], "tests_sha256": hashes["tests/test_parse_core.py"],
              "mutmut": stats, "strict_score_percent": round(score, 2), "survivor_details": findings.splitlines()}
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if args.report:
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    evaluate(stats, args.min_score)


if __name__ == "__main__":
    main()

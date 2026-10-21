"""SC-01 release-metric synchronization contract tests.

Asserts the SC-01 decision (``recovery/aaron-decisions.md``, 2026-07-17) is
consistently reflected across the per-file checker, ``pyproject.toml`` coverage
config, the CI workflow, and the spec/acceptance/interface documents.

SC-01 chose option (B): ``coverage.json.totals.percent_covered`` (combined
statement + branch, since ``branch = true`` in ``pyproject.toml``) is the single
release metric, with a strict ``> 95.0`` threshold and no ``--force`` override.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
CHECKER = ROOT / "tests" / "unit" / "_checkers" / "per_file_coverage.py"
PYPROJECT = ROOT / "pyproject.toml"
WORKFLOW = ROOT / ".github" / "workflows" / "unit-coverage.yml"
ACCEPTANCE = (
    ROOT
    / ".louke/project/specs/v0.2-004-coverage-recovery/acceptance.md"
)
INTERFACES = (
    ROOT
    / ".louke/project/specs/v0.2-004-coverage-recovery/interfaces.md"
)
SPEC_ID = "v0.2-004-coverage-recovery"


def _ac_block(text: str, ac_id: str) -> str:
    """Return the markdown slice for a single acceptance-criterion id.

    Args:
        text: Full acceptance.md source text.
        ac_id: The unique AC token, e.g. ``AC-NFR1001-01``.

    Returns:
        The text from the token up to (but excluding) the next ``### AC-``
        heading, or end of file if none follows.

    Raises:
        ValueError: If ``ac_id`` is not found in ``text``.
    """
    start = text.index(ac_id)
    next_heading = text.find("### AC-", start + len(ac_id))
    end = next_heading if next_heading != -1 else len(text)
    return text[start:end]


def _section(text: str, number: int) -> str:
    """Return the markdown slice for a numbered ``## N.`` section.

    Args:
        text: Full interfaces.md source text.
        number: The section number to extract.

    Returns:
        The text from the section heading up to (but excluding) the next
        ``## `` top-level heading, or end of file if none follows.

    Raises:
        ValueError: If the section heading is not found.
    """
    marker = f"## {number}."
    start = text.index(marker)
    next_heading = text.find("\n## ", start + len(marker))
    end = next_heading if next_heading != -1 else len(text)
    return text[start:end]


def _inventory(records: list[tuple[str, str]]) -> dict:
    return {
        "schema": "production-inventory-index/v2",
        "spec_id": SPEC_ID,
        "record_count": len(records),
        "records": [
            {
                "record_id": f"prod-{index:04d}",
                "path": path,
                "origin": origin,
                "classification": (
                    "active v0.2 product"
                    if origin == "v0.2-added"
                    else "active retained legacy"
                ),
            }
            for index, (path, origin) in enumerate(records)
        ],
    }


def _coverage(percent: float, files: dict[str, tuple[float, int]]) -> dict:
    return {
        "totals": {"percent_covered": percent},
        "files": {
            path: {
                "summary": {
                    "percent_covered": file_percent,
                    "num_statements": statements,
                    "covered_lines": round(statements * file_percent / 100),
                }
            }
            for path, (file_percent, statements) in files.items()
        },
    }


def _run_checker(
    tmp_path: Path,
    coverage: dict,
    inventory: dict,
) -> tuple[subprocess.CompletedProcess[str], dict]:
    """Run the per-file checker on synthetic artifacts and return result + report.

    Args:
        tmp_path: Temporary directory for input/output artifacts.
        coverage: Synthetic coverage.json payload.
        inventory: Synthetic production inventory payload.

    Returns:
        A tuple of the completed subprocess and the parsed report JSON.
    """
    coverage_path = tmp_path / "coverage.json"
    inventory_path = tmp_path / "inventory.json"
    waiver_path = tmp_path / "coverage-waivers.json"
    report_path = tmp_path / "report.json"
    coverage_path.write_text(json.dumps(coverage), encoding="utf-8")
    inventory_path.write_text(json.dumps(inventory), encoding="utf-8")
    waiver_path.write_text(
        json.dumps(
            {"schema": "coverage-waivers/v1", "spec_id": SPEC_ID, "waivers": []}
        ),
        encoding="utf-8",
    )
    result = subprocess.run(
        [
            sys.executable,
            str(CHECKER),
            str(coverage_path),
            str(waiver_path),
            "--inventory",
            str(inventory_path),
            "--report",
            str(report_path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    return result, json.loads(report_path.read_text(encoding="utf-8"))


def test_per_file_checker_reads_totals_percent_covered_and_rejects_at_threshold(
    tmp_path: Path,
) -> None:
    """SC-01: checker reads totals.percent_covered and rejects percent_covered <= 95.0."""
    inventory = _inventory([("quantide/a.py", "v0.2-added")])
    result, report = _run_checker(
        tmp_path,
        _coverage(95.0, {"quantide/a.py": (100.0, 1)}),
        inventory,
    )

    assert result.returncode == 1
    assert "must be greater than 95.0%" in result.stderr
    assert report["overall"]["metric"] == "totals.percent_covered"
    assert report["overall"]["status"] == "fail"


def test_pyproject_coverage_run_has_branch_true_and_no_conflicting_fail_under() -> None:
    """SC-01: pyproject [tool.coverage.run] has branch=true and no fail_under conflict."""
    with PYPROJECT.open("rb") as config_file:
        config = tomllib.load(config_file)

    run = config["tool"]["coverage"]["run"]
    assert run["branch"] is True
    assert "fail_under" not in run


def test_workflow_uses_cov_fail_under_95() -> None:
    """SC-01: the CI workflow enforces --cov-fail-under=95."""
    assert "--cov-fail-under=95" in WORKFLOW.read_text()


def test_acceptance_ac_nfr1001_01_names_combined_metric_and_strict_threshold() -> None:
    """SC-01: acceptance.md AC-NFR1001-01 names totals.percent_covered, branch=true, strict >95.0, no --force."""
    block = _ac_block(ACCEPTANCE.read_text(), "AC-NFR1001-01")

    assert "totals.percent_covered" in block
    assert "> 95.0" in block
    assert "branch = true" in block
    assert "--force" in block


def test_interfaces_section_2_names_totals_percent_covered() -> None:
    """SC-01: interfaces.md section 2 overall gate references totals.percent_covered."""
    section = _section(INTERFACES.read_text(), 2)

    assert "totals.percent_covered" in section

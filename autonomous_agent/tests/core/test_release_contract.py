from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
RELEASE_SCRIPT = REPOSITORY_ROOT / "tools" / "release_check.sh"
RELEASE_WORKFLOW = REPOSITORY_ROOT / ".github" / "workflows" / "release-gate.yml"
DEVELOPMENT_RULES = REPOSITORY_ROOT / "docs" / "DEVELOPMENT_RULES.md"
V50_AGENT = REPOSITORY_ROOT / "autonomous_agent" / "safe_agent_v50.py"

REQUIRED_GATE_FRAGMENTS = {
    RELEASE_SCRIPT: (
        "set -Eeuo pipefail",
        "./autonomous_agent/tools/test_all_v50.sh",
        "autonomous_agent.tests.test_runtime_paths",
        "autonomous_agent.tests.test_recovery_storage",
        "autonomous_agent.tests.test_recovery_schema",
        "uv run --frozen pytest autonomous_agent/tests/core",
        "uv run --frozen ruff check autonomous_agent/core autonomous_agent/cli.py autonomous_agent/ui.py autonomous_agent/tests/core",
        "uv run --frozen mypy autonomous_agent/core autonomous_agent/cli.py autonomous_agent/ui.py",
        "uv run --frozen bandit -q -r autonomous_agent/core autonomous_agent/cli.py autonomous_agent/ui.py",
        "shellcheck tools/release_check.sh autonomous_agent/tools/test_all_v50.sh",
        "shfmt -d tools/release_check.sh autonomous_agent/tools/test_all_v50.sh",
        "python3 -m compileall",
        "uv build --offline --no-build-isolation",
        "test -f autonomous_agent/BUILD_INFO_V50.txt",
        "test -f autonomous_agent/release/V50_COMMIT.txt",
        "test -f autonomous_agent/release/V50_BUILD_TIME.txt",
        "git diff --check",
    ),
    RELEASE_WORKFLOW: (
        '"v50-development-*"',
        'python-version: "3.12"',
        "uv==0.12.5",
        "uv sync --frozen --group dev",
        "sudo apt-get update",
        "sudo apt-get install -y apparmor-profiles apparmor-utils bubblewrap shellcheck shfmt",
        "sudo install -m 0644 /usr/share/apparmor/extra-profiles/bwrap-userns-restrict /etc/apparmor.d/bwrap-userns-restrict",
        "sudo apparmor_parser -r /etc/apparmor.d/bwrap-userns-restrict",
        "./tools/release_check.sh",
    ),
}

FORBIDDEN_OFFLINE_GATE_FRAGMENTS = (
    "test_all_v49.sh",
    "test_end_to_end_v49.sh",
    "test_end_to_end_v50.sh",
    "safe_agent_v49",
)

ERROR_CORRECTION_STEPS = (
    "reproduce the exact failure",
    "systematic debugging",
    "GitHub plugin or `gh` fallback",
    "matching installed review/security plugin when callable",
    "regression test before production changes",
    "correct the root cause",
    "independent review",
    "reviewed SHA matches local `HEAD`",
    "report unavailable plugin endpoints",
)


def _assert_fragments_in_order(document: str, fragments: tuple[str, ...]) -> None:
    position = -1
    for fragment in fragments:
        next_position = document.find(fragment, position + 1)
        assert next_position >= 0, f"missing release-contract fragment: {fragment!r}"
        assert next_position > position
        position = next_position


def test_canonical_release_script_has_exact_ordered_offline_gate() -> None:
    script = RELEASE_SCRIPT.read_text(encoding="utf-8")

    _assert_fragments_in_order(script, REQUIRED_GATE_FRAGMENTS[RELEASE_SCRIPT])
    assert "git status --short" in script
    assert script.index("git status --short") < script.index(
        "./autonomous_agent/tools/test_all_v50.sh"
    )
    assert script.count("check_clean_worktree") == 3
    for forbidden in FORBIDDEN_OFFLINE_GATE_FRAGMENTS:
        assert forbidden not in script


def test_github_workflow_runs_only_the_canonical_gate() -> None:
    workflow = RELEASE_WORKFLOW.read_text(encoding="utf-8")

    _assert_fragments_in_order(workflow, REQUIRED_GATE_FRAGMENTS[RELEASE_WORKFLOW])
    assert 'branches:\n      - main\n      - "v50-development-*"' in workflow
    assert "permissions:\n  contents: read" in workflow
    assert "uses: actions/checkout@v4" in workflow
    assert "uses: actions/setup-python@v5" in workflow
    assert workflow.count("./tools/release_check.sh") == 1
    for forbidden in FORBIDDEN_OFFLINE_GATE_FRAGMENTS:
        assert forbidden not in workflow


def test_error_correction_workflow_is_complete_and_ordered() -> None:
    rules = DEVELOPMENT_RULES.read_text(encoding="utf-8")
    section = rules.split("## Plugin and GitHub Error Correction", 1)[1]
    section = section.split("\n## ", 1)[0]

    _assert_fragments_in_order(
        section.lower(), tuple(fragment.lower() for fragment in ERROR_CORRECTION_STEPS)
    )
    numbered_steps = [line for line in section.splitlines() if line[:3].endswith(". ")]
    assert [line.split(".", 1)[0] for line in numbered_steps] == [
        str(number) for number in range(1, 10)
    ]


def test_v50_sandbox_uses_python_from_its_mounted_system_tree() -> None:
    document = V50_AGENT.read_text(encoding="utf-8")
    run_python = document.split("def run_python", 1)[1].split("TOOL_NAMES", 1)[0]

    assert '"/usr/bin/python3"' in run_python
    assert "sys.executable" not in run_python

#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIRECTORY="$(
	cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
	pwd -P
)"
readonly SCRIPT_DIRECTORY
REPOSITORY_ROOT="$(
	cd -- "${SCRIPT_DIRECTORY}/.."
	pwd -P
)"
readonly REPOSITORY_ROOT

cd -- "${REPOSITORY_ROOT}"

echo "=== LOCAL CODING AGENT RELEASE CHECK ==="

check_clean_worktree() {
	# Ignore rules cover documented generated output such as dist/. Every tracked
	# change and every non-ignored untracked path remains release-fatal.
	local worktree_status
	worktree_status="$(git status --short --untracked-files=all)"
	if [[ -n "${worktree_status}" ]]; then
		printf '%s\n' "${worktree_status}"
		echo "ERROR: release checks require a clean worktree" >&2
		exit 1
	fi
}

echo "[1/11] Clean worktree"
check_clean_worktree

echo "[2/11] Legacy V50 gate"
./autonomous_agent/tools/test_all_v50.sh

echo "[3/11] Runtime and recovery unittests"
python3 -m unittest \
	autonomous_agent.tests.test_runtime_paths \
	autonomous_agent.tests.test_recovery_storage \
	autonomous_agent.tests.test_recovery_schema

echo "[4/11] Core tests"
uv run --frozen pytest autonomous_agent/tests/core

echo "[5/11] Ruff"
uv run --frozen ruff check autonomous_agent/core autonomous_agent/cli.py autonomous_agent/ui.py autonomous_agent/tests/core

echo "[6/11] MyPy"
uv run --frozen mypy autonomous_agent/core autonomous_agent/cli.py autonomous_agent/ui.py

echo "[7/11] Bandit"
uv run --frozen bandit -q -r autonomous_agent/core autonomous_agent/cli.py autonomous_agent/ui.py

echo "[8/11] Shell quality"
shellcheck tools/release_check.sh autonomous_agent/tools/test_all_v50.sh
shfmt -d tools/release_check.sh autonomous_agent/tools/test_all_v50.sh

echo "[9/11] Python compilation"
python3 -m compileall \
	autonomous_agent/safe_agent_v50.py \
	autonomous_agent/runtime_paths.py \
	autonomous_agent/state_manager.py \
	autonomous_agent/recovery_schema.py \
	autonomous_agent/recovery_authority.py \
	autonomous_agent/recovery_gate.py \
	autonomous_agent/recovery_governance.py \
	autonomous_agent/core \
	autonomous_agent/cli.py \
	autonomous_agent/ui.py

echo "[10/11] Build package"
uv build --offline --no-build-isolation

echo "[11/11] Release files and diff"
test -f autonomous_agent/BUILD_INFO_V50.txt
test -f autonomous_agent/runtime_paths.py
test -f autonomous_agent/release/V50_COMMIT.txt
test -f autonomous_agent/release/V50_BUILD_TIME.txt

if grep -nF 'shell=True' autonomous_agent/safe_agent_v50.py; then
	echo "ERROR: unsafe shell execution found in safe_agent_v50.py" >&2
	exit 1
else
	readonly GREP_STATUS=$?
	if ((GREP_STATUS != 1)); then
		echo "ERROR: unsafe-shell inspection failed" >&2
		exit "${GREP_STATUS}"
	fi
fi

git diff --check
check_clean_worktree

echo
echo "=== RELEASE CHECK PASSED ==="

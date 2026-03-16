#!/usr/bin/env bash
# Run all tests for the A2A Octane Wrapper.
#
# Usage:
#   ./run_tests.sh              # all tests (unit + E2E)
#   ./run_tests.sh unit         # unit tests only (free, no API key)
#   ./run_tests.sh e2e          # E2E tests only (needs GEMINI_API_KEY in .env)
#   ./run_tests.sh e2e-free     # E2E tool-routing tests only (free)

set -e

cd "$(dirname "$0")"

# Prefer local venv if present
if [ -x "./.venv-1/bin/python" ]; then
  PYTHON="./.venv-1/bin/python"
else
  PYTHON="python"
fi

case "${1:-all}" in
  unit)
    echo "=== Running unit tests ==="
    "$PYTHON" -m pytest tests -v
    ;;
  e2e)
    echo "=== Running E2E tests (requires GEMINI_API_KEY) ==="
    "$PYTHON" -m pytest tests/e2e/ -v
    ;;
  e2e-free)
    echo "=== Running E2E tool-routing tests (free, no API key) ==="
    "$PYTHON" -m pytest tests/e2e/ -k "TestToolCorrectness and not DeepEval" -v
    ;;
  all)
    echo "=== Running unit tests ==="
    "$PYTHON" -m pytest tests -v
    echo ""
    echo "=== Running E2E tests ==="
    "$PYTHON" -m pytest tests/e2e/ -v
    ;;
  *)
    echo "Usage: $0 {unit|e2e|e2e-free|all}"
    exit 1
    ;;
esac

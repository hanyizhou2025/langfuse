#!/usr/bin/env bash

set -euo pipefail

remote="${1:-origin}"

echo "Checking Git remote: ${remote}"
git ls-remote --exit-code "${remote}" HEAD
echo "Git remote is reachable."

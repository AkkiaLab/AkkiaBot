#!/usr/bin/env bash
# Credential scan with gitleaks (.gitleaks.toml + .gitleaksignore). See docs/secret-scanning.md.
#
#   secret_scan.sh history   every commit reachable from HEAD (CI `secret-scan`, `just secret-scan`)
#   secret_scan.sh unpushed  commits being pushed that no remote-tracking ref has yet (pre-push hook)
#
# Runs through pre-commit, which provides the pinned gitleaks binary on PATH.
# Findings are always redacted; only rule, file, line and commit are printed.
set -euo pipefail

mode="${1:-history}"
cd "$(git rev-parse --show-toplevel)"
scan() { gitleaks git --redact=100 --no-banner --verbose --exit-code 1 --log-opts="$1" .; }

case "$mode" in
  history)
    scan "HEAD"
    ;;
  unpushed)
    # pre-commit sets PRE_COMMIT_TO_REF for pre-push; all zeros means a branch deletion.
    to="${PRE_COMMIT_TO_REF:-HEAD}"
    if [[ "$to" =~ ^0+$ ]]; then exit 0; fi
    if [[ -z "$(git rev-list -n 1 "$to" --not --remotes)" ]]; then
      echo "secret-scan: no unpushed commits"
      exit 0
    fi
    # Covers the first push of a new branch too: everything not on any remote yet.
    scan "$to --not --remotes"
    ;;
  *)
    echo "usage: $0 [history|unpushed]" >&2
    exit 2
    ;;
esac

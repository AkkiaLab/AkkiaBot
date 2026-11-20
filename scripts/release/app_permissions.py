"""Token permissions of the release purpose of the workspace agent App ``akkialab-agent``.

The release pipeline requests its installation token with exactly this set, never
"whatever the installation has" (AKKIA-25). It is declared in two more places that
cannot import Python: ``.github/actions/release-setup`` (``permission-*`` inputs) and
``scripts/release/as-app.sh`` (request body); ``tests/release/test_app_permissions.py``
keeps the three identical. The Reviewer tool ``reviewer-gh`` lives in the workspace's
private tools repository (AKKIA-34) and cross-checks its own release set against this
repository's master in its CI.
"""

from __future__ import annotations

from collections.abc import Mapping

RELEASE_PERMISSIONS: Mapping[str, str] = {
    "actions": "write",
    "administration": "write",
    "checks": "read",
    "contents": "write",
    "issues": "write",
    "metadata": "read",
    "pull_requests": "write",
    "statuses": "read",
}

"""Token permissions of the release purpose of the GitHub App ``akkialab-agent``.

The release pipeline requests its installation token with exactly this set, never
"whatever the installation has". It is declared in two more places that
cannot import Python: ``.github/actions/release-setup`` (``permission-*`` inputs) and
``scripts/release/as-app.sh`` (request body); ``tests/release/test_app_permissions.py``
keeps the three identical. Other uses of the same App (review, merge) are managed
outside this repository and must not widen this set.
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

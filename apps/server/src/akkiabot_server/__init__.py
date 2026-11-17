"""AkkiaBot backend service: Telegram bot runtime and HTTP API."""

import os
from collections.abc import Mapping
from importlib.metadata import version

__version__ = version("akkiabot-server")

GIT_SHA_ENV = "AKKIABOT_GIT_SHA"


def release_name(environ: Mapping[str, str] | None = None) -> str:
    """Deployment release shared by Sentry and Langfuse.

    The image build bakes the full commit sha into ``AKKIABOT_GIT_SHA``
    (``deploy/Dockerfile``, ``.github/workflows/release.yml``), so every
    deployed image reports ``akkiabot@<sha>``. Local runs and images built
    without the build arg fall back to ``akkiabot@<package version>``.
    """
    env = os.environ if environ is None else environ
    sha = env.get(GIT_SHA_ENV, "").strip()
    return f"akkiabot@{sha or __version__}"

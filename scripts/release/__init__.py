"""Release flow: version branches, cascade forward merges, RC and formal tags.

Runbook: docs/release.md. Entry point: ``python -m scripts.release`` (cli.py).
Everything here only depends on the Python standard library, ``git`` and
``gh`` so that GitHub Actions and the runtime machine run the same code.
"""

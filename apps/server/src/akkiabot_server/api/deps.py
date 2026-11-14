"""FastAPI dependencies."""

from __future__ import annotations

from typing import Annotated, cast

from fastapi import Depends, Request

from akkiabot_server.runtime import Runtime


def get_runtime(request: Request) -> Runtime:
    """The process :class:`Runtime`, attached by :func:`create_app`."""
    return cast(Runtime, request.app.state.runtime)


RuntimeDep = Annotated[Runtime, Depends(get_runtime)]

"""HTTP API (FastAPI)."""

from akkiabot_server.api.app import create_app, openapi_document

__all__ = ["create_app", "openapi_document"]

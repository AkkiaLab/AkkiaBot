"""The committed OpenAPI document (input of the frontend types) matches the backend."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from akkiabot_server.api import openapi_document
from akkiabot_server.main import main

COMMITTED = Path(__file__).resolve().parents[3] / "apps" / "webapp" / "openapi.json"


def test_committed_openapi_is_up_to_date() -> None:
    committed = json.loads(COMMITTED.read_text(encoding="utf-8"))
    assert committed == openapi_document(), "后端接口已变更，请运行 `just gen-api` 并提交生成结果"


def test_captcha_contract() -> None:
    document = openapi_document()
    operation = document["paths"]["/api/v1/captcha/verify"]["post"]
    assert operation["operationId"] == "verifyCaptcha"
    assert {"200", "400", "401", "403", "404", "409", "410", "502", "503"} <= set(
        operation["responses"]
    )
    request = document["components"]["schemas"]["CaptchaVerifyRequest"]
    assert set(request["required"]) == {"challenge_id", "init_data", "turnstile_token"}
    codes = set(document["components"]["schemas"]["RejectReason"]["enum"])
    assert {"turnstile_failed", "init_data_expired", "challenge_used", "wrong_user"} <= codes
    # The webhook is never part of the public contract.
    assert "/telegram/webhook" not in document["paths"]


def test_openapi_command(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["openapi"]) == 0
    assert json.loads(capsys.readouterr().out) == openapi_document()
    out = tmp_path / "openapi.json"
    assert main(["openapi", "--output", str(out)]) == 0
    assert out.read_text(encoding="utf-8") == COMMITTED.read_text(encoding="utf-8")

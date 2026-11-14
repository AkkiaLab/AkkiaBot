import pytest

import akkiabot_antispam
import akkiabot_core
import akkiabot_server
import akkiabot_whois
from akkiabot_server.main import main


def test_server_depends_on_all_packages() -> None:
    versions = {
        m.__version__ for m in (akkiabot_server, akkiabot_core, akkiabot_antispam, akkiabot_whois)
    }
    assert versions == {"0.1.0"}


def test_main_prints_version(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == 0
    assert capsys.readouterr().out == "akkiabot 0.1.0\n"


def test_version_flag(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert capsys.readouterr().out == "akkiabot 0.1.0\n"

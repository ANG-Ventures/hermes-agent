from __future__ import annotations

import logging


def test_dashboard_startup_state_db_log_is_absolute(tmp_path, monkeypatch, caplog):
    import hermes_cli.web_server as web_server

    monkeypatch.setattr(web_server, "get_hermes_home", lambda: tmp_path)

    with caplog.at_level(logging.INFO, logger="hermes_cli.web_server"):
        path = web_server._log_dashboard_state_db_path()

    assert path.is_absolute()
    assert path.name == "state.db"
    assert any(
        "Dashboard state.db path:" in record.getMessage()
        and str(path) in record.getMessage()
        for record in caplog.records
    )

import json
import warnings

import pytest

from duckduck import DuckAPI
from duckduck.semantic.admin import validate_config

SECRET = "s3cr3t-value-never-shown"


def _config(tmp_path, **extra):
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    (data / "hosts.csv").write_text("hostname,risk\nweb-1,70\ndb-1,40\n")
    config = {
        "on_error": "warn",
        "services": {
            "files": {"connector": "files", "path": "data", "table_prefix": ""},
            "nvd": {"connector": "nvd", "authentication": {"type": "local", "api_key": SECRET}},
            "world": {"connector": "restcountries"},  # no authentication block: fails to start
            "sn": {"connector": "servicenow", "instance": "x", "timezone": "America/Sao_Paulo",
                   "authentication": {"type": "aws", "secret_id": "prod/sn", "password": "$secret.pw"}},
        },
        "ai_providers": {"claude": {"provider": "anthropic", "api_key": SECRET}},
        "views": {"risky": {"sql": "SELECT * FROM hosts WHERE risk > 50", "description": "risky hosts"},
                  "broken": {"table": "nowhere_table", "args": {"x": 1}}},
        "lake": {"state": "state"},
        **extra,
    }
    path = tmp_path / "duckduck.json"
    path.write_text(json.dumps(config))
    return path


def _duck(path):
    duck = DuckAPI()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        duck.auto_register(config_path=str(path), secrets={"aws": _NoSecrets()})
    return duck


class _NoSecrets:
    def get_secret(self, secret_id):
        raise RuntimeError(f"no secret {secret_id} here")


def test_services_say_which_started_and_why_not(tmp_path):
    duck = _duck(_config(tmp_path))
    df = duck.sql("SELECT * FROM duckduck.services").df().set_index("name")
    assert list(df.index) == ["files", "nvd", "world", "sn"]
    assert df.loc["files", "started"] and df.loc["files", "tables"] >= 1
    assert not df.loc["world", "started"] and "authentication" in df.loc["world", "error"]
    assert not df.loc["sn", "started"] and df.loc["sn", "auth_type"] == "aws" and df.loc["sn", "secret_id"] == "prod/sn"
    assert json.loads(df.loc["sn", "options"]) == {"instance": "x", "timezone": "America/Sao_Paulo"}
    failed = duck.sql("SELECT name FROM duckduck.services WHERE started = false ORDER BY name").df()
    assert list(failed["name"]) == ["sn", "world"]


def test_no_system_table_ever_shows_a_secret(tmp_path):
    duck = _duck(_config(tmp_path))
    for table in ("services", "tables", "saved_tables", "settings", "pipeline_runs"):
        text = duck.sql(f"SELECT * FROM duckduck.{table}").df().to_csv()
        assert SECRET not in text, table
    settings = duck.sql("SELECT key, value, secret FROM duckduck.settings").df().set_index("key")
    assert settings.loc["services.nvd.authentication.api_key", "value"] == "***"
    assert settings.loc["services.nvd.authentication.api_key", "secret"]
    assert settings.loc["ai_providers.claude.api_key", "value"] == "***"
    assert settings.loc["services.sn.authentication.password", "value"] == "$secret.pw"  # a reference, not a secret


def test_settings_are_one_row_per_key_and_read_the_file_as_it_is_now(tmp_path):
    path = _config(tmp_path)
    duck = _duck(path)
    tz = duck.sql("SELECT value FROM duckduck.settings WHERE key = 'services.sn.timezone'").df()
    assert tz["value"].tolist() == ["America/Sao_Paulo"]
    lake = duck.sql("SELECT key FROM duckduck.settings WHERE section = 'lake'").df()
    assert lake["key"].tolist() == ["lake.state"]
    data = json.loads(path.read_text())
    data["services"]["sn"]["timezone"] = "UTC"
    path.write_text(json.dumps(data))
    again = duck.sql("SELECT value FROM duckduck.settings WHERE key ILIKE '%sn.timezone'").df()
    assert again["value"].tolist() == ["UTC"]


def test_saved_tables_list_failures_with_the_reason(tmp_path):
    duck = _duck(_config(tmp_path))
    df = duck.sql("SELECT * FROM duckduck.saved_tables ORDER BY name").df().set_index("name")
    assert df.loc["risky", "kind"] == "query" and df.loc["risky", "registered"]
    assert df.loc["risky", "description"] == "risky hosts"
    assert df.loc["broken", "kind"] == "bound" and not df.loc["broken", "registered"]
    assert df.loc["broken", "error"]


def test_tables_lists_every_registered_table_with_its_service_and_address(tmp_path):
    duck = _duck(_config(tmp_path))
    df = duck.sql("SELECT name, service, address, kind FROM duckduck.tables").df().set_index("name")
    assert df.loc["hosts", "service"] == "files" and df.loc["hosts", "address"] == "files.hosts"
    assert df.loc["duckduck_services", "kind"] == "catalog"  # listings, never drafted as data
    assert df.loc["duckduck_services", "address"] == "duckduck.services"


def test_pipeline_runs_read_the_state_folder(tmp_path):
    path = _config(tmp_path)
    state = tmp_path / "state"
    state.mkdir()
    (state / "assets_silver.json").write_text(json.dumps({
        "last_run_id": "r1", "last_success_at": "2026-10-01T12:00:00+00:00",
        "load": {"type": "incremental", "columns": ["_loaded_at"]},
        "watermarks": {"_loaded_at": "2026-10-01 11:59:00"}}))
    duck = _duck(path)
    df = duck.sql("SELECT * FROM duckduck.pipeline_runs").df()
    assert df["pipeline"].tolist() == ["assets_silver"] and df["load"].tolist() == ["incremental"]
    assert json.loads(df["watermarks"][0]) == {"_loaded_at": "2026-10-01 11:59:00"}


def test_system_tables_can_be_turned_off(tmp_path):
    duck = _duck(_config(tmp_path, system_tables=False))
    assert not any(n.startswith("duckduck_") for n in duck.functions)
    assert "system_tables is true or false" in " ".join(validate_config({"system_tables": "yes"})["errors"])
    assert not validate_config({"system_tables": False, "services": {}})["warnings"]


def test_a_service_named_duckduck_keeps_its_name(tmp_path):
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "x.csv").write_text("a\n1\n")
    duck = DuckAPI()
    with pytest.warns(RuntimeWarning, match="named 'duckduck'"):
        duck.auto_register({"duckduck": {"connector": "files", "path": str(tmp_path / "data")}})
    assert "duckduck_x" in duck.functions and "duckduck_services" not in duck.functions

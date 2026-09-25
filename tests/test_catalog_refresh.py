"""Incremental catalog generation: max_age, force, hand-written sources, notes, auto_refresh."""

import datetime as dt
import json
import os
import warnings

import pandas as pd
import pytest

pytest.importorskip("pydantic")
pytest.importorskip("yaml")

from duckduck import DuckAPI  # noqa: E402
from duckduck.semantic import Catalog, CatalogGenerator, SemanticConfig, SemanticSearch, generate_catalog  # noqa: E402
from duckduck.semantic.generation import (  # noqa: E402
    GenEntity,
    GenField,
    GenRelationship,
    GenSource,
    GenVocabulary,
    parse_age,
)

NOW = dt.datetime(2026, 9, 25, 12, 0, tzinfo=dt.timezone.utc)


class CountingLLM:
    """Describes every column; the vocabulary joins hosts.ip to alerts.ip."""

    def __init__(self):
        self.drafted, self.prompts, self.link_prompts = [], [], []

    def generate(self, system, prompt, output_model):
        if output_model is GenVocabulary:
            self.link_prompts.append(prompt)
            return GenVocabulary(
                entities=[GenEntity(name="host", description="LLM's host", keywords=["hosts"])],
                relationships=[GenRelationship(from_field="hosts.ip", to_field="alerts.ip",
                                               type="same_entity", confidence=0.9)],
            )
        profile = json.loads(prompt.split("Table profile:\n", 1)[1])
        self.drafted.append(profile["source_name"])
        self.prompts.append(profile)
        return GenSource(
            description=f"drafted {profile['source_name']}",
            entities=["host"],
            fields=[GenField(name=c, semantic_type="ip_address" if c == "ip" else None) for c in profile["columns"]],
        )


def _duck():
    duck = DuckAPI()
    duck.register_api_function("hosts", lambda limit=None: pd.DataFrame({"ip": ["10.0.0.1"], "name": ["web"]}))
    duck.register_api_function("alerts", lambda limit=None: pd.DataFrame({"ip": ["10.0.0.1"], "sev": ["high"]}))
    return duck


def _gen(llm, **kw):
    return CatalogGenerator(llm, _duck(), clock=lambda: NOW, llm_label="claude (anthropic claude-opus-5)", **kw)


def _aged(catalog, name, days):
    data = catalog.model_dump(by_alias=True)
    data["sources"][name]["generated_at"] = NOW - dt.timedelta(days=days)
    return Catalog.model_validate(data)


# ---------------------------------------------------------------------------


@pytest.mark.parametrize("text, expected", [
    ("7d", dt.timedelta(days=7)), ("12h", dt.timedelta(hours=12)), ("30m", dt.timedelta(minutes=30)),
    ("2w", dt.timedelta(weeks=2)), (90, dt.timedelta(seconds=90)), (None, None),
])
def test_parse_age(text, expected):
    assert parse_age(text) == expected


def test_parse_age_rejects_garbage():
    with pytest.raises(ValueError, match="max_age"):
        parse_age("a week")


def test_first_run_stamps_every_source():
    result = _gen(CountingLLM()).generate()
    assert result.drafted == {"hosts": "new", "alerts": "new"}
    for src in result.catalog.sources.values():
        assert src.generated_at == NOW and src.generated_by == "claude (anthropic claude-opus-5)"


def test_up_to_date_catalog_makes_no_llm_call():
    first = _gen(CountingLLM()).generate()
    llm = CountingLLM()
    again = _gen(llm, max_age="7d").generate(existing=first.catalog)
    assert llm.drafted == [] and llm.link_prompts == [] and not again.changed
    assert again.catalog is first.catalog and again.summary().startswith("up to date")


def test_only_expired_sources_are_redrafted_and_the_rest_is_kept():
    existing = _aged(_gen(CountingLLM()).generate().catalog, "alerts", days=10)
    llm = CountingLLM()
    result = _gen(llm, max_age="7d").generate(existing=existing)
    assert llm.drafted == ["alerts"] and result.drafted == {"alerts": "10d old, max_age 7d"}
    assert result.kept == ["hosts"] and result.catalog.sources["alerts"].generated_at == NOW
    # the link pass still saw the kept source, so joins to it are found
    assert '"hosts"' in llm.link_prompts[0]
    assert [(r.from_, r.to) for r in result.catalog.relationships] == [("hosts.ip", "alerts.ip")]


def test_force_true_redrafts_every_generated_source():
    existing = _gen(CountingLLM()).generate().catalog
    llm = CountingLLM()
    result = _gen(llm).generate(existing=existing, force=True)
    assert sorted(llm.drafted) == ["alerts", "hosts"] and set(result.drafted.values()) == {"forced"}


def test_force_by_name_or_pattern():
    existing = _gen(CountingLLM()).generate().catalog
    llm = CountingLLM()
    result = _gen(llm).generate(existing=existing, force=["al*", "nope"])
    assert llm.drafted == ["alerts"]
    assert any("force: 'nope' matched no table" in w for w in result.warnings)


def _hand_written(existing):
    data = existing.model_dump(by_alias=True)
    hosts = data["sources"]["hosts"]
    hosts.update(generated_at=None, generated_by=None, description="written by hand")
    return Catalog.model_validate(data)


def test_hand_written_sources_are_never_redrafted_unless_forced_by_name():
    existing = _hand_written(_aged(_gen(CountingLLM()).generate().catalog, "hosts", days=100))
    llm = CountingLLM()
    _gen(llm, max_age="1d").generate(existing=existing, force=True)
    assert "hosts" not in llm.drafted  # neither age nor force=True touch it
    llm = CountingLLM()
    result = _gen(llm).generate(existing=existing, force="hosts")
    assert llm.drafted == ["hosts"] and result.catalog.sources["hosts"].description == "drafted hosts"


def test_a_table_already_cataloged_under_another_name_keeps_that_name():
    data = _gen(CountingLLM()).generate().catalog.model_dump(by_alias=True)
    data["sources"]["my_hosts"] = data["sources"].pop("hosts")
    data["relationships"] = []
    existing = Catalog.model_validate(data)
    llm = CountingLLM()
    result = _gen(llm, max_age="7d").generate(existing=existing)
    assert llm.drafted == [] and set(result.catalog.sources) == {"my_hosts", "alerts"}


def test_existing_vocabulary_wins_and_is_offered_to_the_llm():
    data = _gen(CountingLLM()).generate().catalog.model_dump(by_alias=True)
    data["entities"]["host"]["description"] = "edited by a person"
    existing = _aged(Catalog.model_validate(data), "alerts", days=30)
    llm = CountingLLM()
    result = _gen(llm, max_age="7d").generate(existing=existing)
    assert result.catalog.entities["host"].description == "edited by a person"
    assert "Already defined" in llm.link_prompts[0] and '"host"' in llm.link_prompts[0]


def test_max_tables_defers_the_rest_to_the_next_run():
    result = _gen(CountingLLM(), max_tables=1).generate()
    assert list(result.drafted) == ["hosts"] and result.deferred == ["alerts"]
    nxt = _gen(CountingLLM(), max_tables=1).generate(existing=result.catalog)
    assert list(nxt.drafted) == ["alerts"] and not nxt.deferred


# ---------------------------------------------------------------------------
# notes
# ---------------------------------------------------------------------------


def _noted(existing):
    data = existing.model_dump(by_alias=True)
    data["sources"]["alerts"]["notes"] = "sev is only filled since 2025"
    data["sources"]["alerts"]["fields"]["sev"]["notes"] = "values are lowercase"
    data["sources"]["alerts"]["critical"] = True
    return Catalog.model_validate(data)


def test_notes_survive_a_redraft_and_reach_the_llm():
    existing = _noted(_gen(CountingLLM()).generate().catalog)
    llm = CountingLLM()
    result = _gen(llm).generate(existing=existing, force="alerts")
    alerts = result.catalog.sources["alerts"]
    assert alerts.notes == "sev is only filled since 2025" and alerts.fields["sev"].notes == "values are lowercase"
    assert alerts.critical is True
    profile = llm.prompts[0]
    assert "sev is only filled since 2025" in profile["owner_notes"]
    assert profile["field_notes"] == {"sev": "values are lowercase"}


def test_notes_on_a_field_that_disappears_are_reported():
    existing = _noted(_gen(CountingLLM()).generate().catalog)

    class DropsSev(CountingLLM):
        def generate(self, system, prompt, output_model):
            out = super().generate(system, prompt, output_model)
            if output_model is GenSource:
                out.fields = [f for f in out.fields if f.name != "sev"]
            return out

    result = _gen(DropsSev()).generate(existing=existing, force="alerts")
    assert any("field 'sev' is gone" in w and "values are lowercase" in w for w in result.warnings)


def test_notes_round_trip_through_the_yaml(tmp_path):
    result = _gen(CountingLLM()).generate()
    result.catalog = _noted(result.catalog)
    path = tmp_path / "catalog.yaml"
    result.write(str(path))
    loaded = Catalog.load(str(path))
    assert loaded.sources["alerts"].notes == "sev is only filled since 2025"
    assert loaded.sources["alerts"].generated_at == NOW


# ---------------------------------------------------------------------------
# generate_catalog / CLI / auto_refresh, against a config file
# ---------------------------------------------------------------------------


@pytest.fixture
def project(tmp_path, monkeypatch):
    (tmp_path / "src.py").write_text(
        "TABLES = {'hosts': lambda limit=None: [{'ip': '10.0.0.1', 'name': 'web'}],\n"
        "          'alerts': lambda limit=None: [{'ip': '10.0.0.1', 'sev': 'high'}]}\n"
    )

    def write(**catalog_generation):
        (tmp_path / "duckduck.json").write_text(json.dumps({
            "services": {"src": {"connector": "python", "module": "src.py", "table_prefix": ""}},
            "ai_providers": {"claude": {"provider": "anthropic", "authentication": {"type": "local", "api_key": "k"}}},
            "semantic": {"catalog_path": "catalog.yaml", "default_llm": "claude",
                         "catalog_generation": catalog_generation},
        }))
        return str(tmp_path / "duckduck.json")

    llm = CountingLLM()
    monkeypatch.setattr(SemanticConfig, "build_llm", lambda self, duck, stage=None: llm)
    return tmp_path, write, llm


def test_generation_maintains_catalog_path_itself(project):
    folder, write, llm = project
    cfg = write()
    first = generate_catalog(config_path=cfg)
    assert first.path == str(folder / "catalog.yaml") and os.path.isfile(first.path)
    before = os.path.getmtime(first.path)
    again = generate_catalog(config_path=cfg)
    assert not again.changed and again.path is None and os.path.getmtime(first.path) == before


def test_cli_force(project, capsys):
    from duckduck.semantic.__main__ import main

    folder, write, llm = project
    cfg = write()
    generate_catalog(config_path=cfg)
    llm.drafted.clear()
    assert main(["--config", cfg, "generate-catalog", "--force", "alerts"]) == 0
    assert llm.drafted == ["alerts"] and "drafted alerts (forced)" in capsys.readouterr().out
    llm.drafted.clear()
    assert main(["--config", cfg, "generate-catalog", "--force"]) == 0
    assert sorted(llm.drafted) == ["alerts", "hosts"]


def test_auto_refresh_drafts_before_searching(project):
    folder, write, llm = project
    cfg = write(auto_refresh=True)
    duck = DuckAPI()
    duck.auto_register(config_path=cfg)
    search = SemanticSearch.from_config(duck, config_path=cfg)
    assert set(search.catalog.sources) == {"hosts", "alerts"} and len(llm.drafted) == 2
    SemanticSearch.from_config(duck, config_path=cfg)
    assert len(llm.drafted) == 2  # nothing expired: no LLM call


def test_auto_refresh_failure_keeps_the_existing_catalog(project, monkeypatch):
    folder, write, llm = project
    cfg = write(auto_refresh=True, max_age="1s")
    generate_catalog(config_path=cfg)

    def broken(self, duck, stage=None):
        raise RuntimeError("LLM down")

    monkeypatch.setattr(SemanticConfig, "build_llm", broken)
    duck = DuckAPI()
    duck.auto_register(config_path=cfg)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        search = SemanticSearch.from_config(duck, config_path=cfg)
    assert set(search.catalog.sources) == {"hosts", "alerts"}
    assert any("auto_refresh failed" in str(w.message) for w in caught)


def test_auto_refresh_needs_generation_to_maintain_catalog_path(project):
    folder, write, llm = project
    with pytest.raises(ValueError, match="auto_refresh only applies"):
        SemanticConfig.load(DuckAPI(), write(auto_refresh=True, output_path="draft.yaml"))


def test_an_invalid_catalog_file_is_never_overwritten(project):
    folder, write, llm = project
    (folder / "catalog.yaml").write_text("sources: {broken: {fields: {}}}\n")
    with pytest.raises(ValueError, match="isn't a valid catalog"):
        generate_catalog(config_path=write())
    assert (folder / "catalog.yaml").read_text() == "sources: {broken: {fields: {}}}\n"


# ---------------------------------------------------------------------------
# the YAML shows where notes go; verbose progress
# ---------------------------------------------------------------------------


def test_yaml_has_a_notes_slot_on_every_source_and_field(tmp_path):
    import yaml

    path = tmp_path / "catalog.yaml"
    _gen(CountingLLM()).generate().write(str(path))
    text = path.read_text()
    assert "Write your observations in `notes`" in text
    assert "&id" not in text and "*id" not in text  # one shared generated_at, no YAML aliases
    data = yaml.safe_load(text)
    for source in data["sources"].values():
        keys = list(source)
        assert keys[keys.index("description") + 1] == "notes"
        assert all("notes" in f for f in source["fields"].values())
    # a person fills a slot in; it loads, and survives the next forced redraft
    text = text.replace("notes: ''", "notes: hosts behind the proxy", 1)
    path.write_text(text)
    existing = Catalog.load(str(path))
    assert existing.sources["hosts"].notes == "hosts behind the proxy"
    redrafted = _gen(CountingLLM()).generate(existing=existing, force=True)
    assert redrafted.catalog.sources["hosts"].notes == "hosts behind the proxy"


def test_verbose_progress(caplog):
    import logging

    caplog.set_level(logging.INFO, logger="duckduck")
    _gen(CountingLLM()).generate()
    text = caplog.text
    assert "catalog: 2 tables — drafting 2 (2 new), keeping 0" in text
    assert "[1/2] hosts (new) — profiling hosts()" in text and "[2/2] alerts (new)" in text
    assert "asking claude (anthropic claude-opus-5)" in text and "elapsed · ~" in text
    assert "linking 2 sources (2 drafted, 0 kept)" in text and "merged: 2 sources" in text


def test_verbose_up_to_date(caplog):
    import logging

    first = _gen(CountingLLM()).generate()
    caplog.set_level(logging.INFO, logger="duckduck")
    _gen(CountingLLM()).generate(existing=first.catalog)
    assert "all up to date — nothing to draft" in caplog.text


def test_llm_calls_log_time_and_tokens(caplog):
    import logging
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from duckduck.semantic.llm import OpenRouterLLM

    client = MagicMock()
    message = SimpleNamespace(parsed="OK", refusal=None)
    client.chat.completions.parse.return_value = SimpleNamespace(
        choices=[SimpleNamespace(message=message, finish_reason="stop")],
        usage=SimpleNamespace(prompt_tokens=1234, completion_tokens=56),
    )
    caplog.set_level(logging.INFO, logger="duckduck")
    OpenRouterLLM("vendor/model", client=client).generate("s", "p", GenSource)
    assert "llm vendor/model → GenSource" in caplog.text and "1,234 in / 56 out tokens" in caplog.text


# ---------------------------------------------------------------------------
# selectors: service, service:table, args; --only; verbose levels
# ---------------------------------------------------------------------------


class Lake:
    """A connector whose tables sit behind a catalog, like Glue."""

    def __init__(self, tables):
        self._tables = tables  # {(database, table): rows}

    def listing(self):
        return pd.DataFrame([{"database": d, "table_name": t} for d, t in self._tables])

    def table(self, database: str, table_name: str, limit=None):
        return pd.DataFrame(self._tables[(database, table_name)])


def _two_service_duck():
    from duckduck.kinds import catalog

    duck = DuckAPI()
    lake = Lake({("security", "proxy_logs"): [{"ip": "1"}], ("security", "dns_logs"): [{"q": "a"}],
                 ("sales", "orders"): [{"id": 1}]})
    lake.tables = catalog(lists="table")(lake.listing.__func__).__get__(lake)
    for name, fn in (("glue_tables", lake.tables), ("glue_table", lake.table)):
        duck.register_api_function(name, fn)
        duck.service_of[name] = "glue"
    duck.register_api_function("hosts", lambda limit=None: pd.DataFrame({"ip": ["1"]}))
    duck.service_of["hosts"] = "inventory"
    return duck


def _gen2(llm, **kw):
    return CatalogGenerator(llm, _two_service_duck(), clock=lambda: NOW, **kw)


def test_the_catalog_is_discovered_per_service():
    result = _gen2(CountingLLM()).generate()
    assert set(result.catalog.sources) == {"security_proxy_logs", "security_dns_logs", "sales_orders", "hosts"}


@pytest.mark.parametrize("selector, expected", [
    ("glue", {"security_proxy_logs", "security_dns_logs", "sales_orders"}),        # a whole service
    ("glue:security.*", {"security_proxy_logs", "security_dns_logs"}),              # service + database
    ("glue:security.proxy_logs", {"security_proxy_logs"}),                          # service + table
    ("glue:proxy_logs", {"security_proxy_logs"}),                                   # service + table name alone
    ("security", {"security_proxy_logs", "security_dns_logs"}),                     # any single arg
    ("inventory:hosts", {"hosts"}),
    ("sales_orders", {"sales_orders"}),                                             # catalog source name
    ("*", {"security_proxy_logs", "security_dns_logs", "sales_orders", "hosts"}),  # everything
])
def test_force_selectors(selector, expected):
    existing = _gen2(CountingLLM()).generate().catalog
    llm = CountingLLM()
    result = _gen2(llm).generate(existing=existing, force=selector)
    assert set(llm.drafted) == expected and set(result.drafted) == expected


def test_force_by_name_drafts_nothing_else_even_new_tables():
    existing = _gen2(CountingLLM()).generate().catalog
    data = existing.model_dump(by_alias=True)
    del data["sources"]["hosts"]  # hosts is "new" now
    llm = CountingLLM()
    _gen2(llm).generate(existing=Catalog.model_validate(data), force="glue:sales.orders")
    assert llm.drafted == ["sales_orders"]


def test_service_selector_does_not_match_another_service():
    existing = _gen2(CountingLLM()).generate().catalog
    llm = CountingLLM()
    result = _gen2(llm).generate(existing=existing, force="inventory:proxy_logs")
    assert llm.drafted == [] and any("matched no table" in w for w in result.warnings)


def test_only_limits_the_run_to_a_service():
    llm = CountingLLM()
    result = _gen2(llm).generate(only="glue:security.*")
    assert set(llm.drafted) == {"security_proxy_logs", "security_dns_logs"}
    # later: only the other service; what's already there is kept
    llm = CountingLLM()
    result = _gen2(llm).generate(existing=result.catalog, only="inventory")
    assert llm.drafted == ["hosts"] and set(result.catalog.sources) >= {"security_proxy_logs", "hosts"}


def test_only_and_force_true_together():
    existing = _gen2(CountingLLM()).generate().catalog
    llm = CountingLLM()
    _gen2(llm).generate(existing=existing, only="glue:sales.*", force=True)
    assert llm.drafted == ["sales_orders"]


def test_auto_register_records_each_table_s_service(project):
    folder, write, llm = project
    duck = DuckAPI()
    duck.auto_register(config_path=write())
    assert duck.service_of == {"hosts": "src", "alerts": "src"}


def test_cli_only(project, capsys):
    from duckduck.semantic.__main__ import main

    folder, write, llm = project
    assert main(["--config", write(), "generate-catalog", "--only", "src:alerts"]) == 0
    assert llm.drafted == ["alerts"]


def test_verbose_level_applies_even_with_your_own_duck(project, caplog):
    import logging

    from duckduck.logs import set_verbose

    folder, write, llm = project
    cfg = write()
    duck = DuckAPI()
    duck.auto_register(config_path=cfg)
    try:
        generate_catalog(config_path=cfg, duck=duck, verbose="debug")
        assert logging.getLogger("duckduck").level == logging.DEBUG
    finally:
        set_verbose(False)


def test_debug_shows_what_goes_to_the_llm_and_what_comes_back(caplog):
    import logging

    caplog.set_level(logging.DEBUG, logger="duckduck")
    _gen(CountingLLM()).generate()
    assert "sent to the LLM:" in caplog.text and '"sample_rows"' in caplog.text
    assert "draft:" in caplog.text and "vocabulary:" in caplog.text


def test_redrafting_over_a_catalog_with_an_activity_without_resource():
    """Regression: kept activities come from a dump without defaults — no 'resource' key when it was None."""
    first = _gen(CountingLLM()).generate().catalog
    data = first.model_dump(by_alias=True)
    data["activities"]["alerting"] = {"description": "Alerts raised", "keywords": ["alerts"]}  # no resource
    data["sources"]["alerts"]["activities"] = ["alerting"]
    existing = Catalog.model_validate(data)
    result = _gen(CountingLLM()).generate(existing=existing, force=True)
    assert result.catalog.activities["alerting"].resource is None and set(result.drafted) == {"hosts", "alerts"}

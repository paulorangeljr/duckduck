"""The shipped examples and reference config stay runnable and consistent."""

import contextlib
import io
import json
import os
import re
import runpy
from unittest.mock import MagicMock

import pytest

pytest.importorskip("pydantic")

from duckduck import DuckAPI  # noqa: E402
from duckduck.registry import SERVICE_REGISTRY  # noqa: E402
from duckduck.semantic import SemanticConfig, generate_catalog, jev_check  # noqa: E402
from duckduck.semantic.generation import GenEntity, GenField, GenSource, GenVocabulary  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EX = os.path.join(REPO, "examples")
CONFIGS = {
    "reference": os.path.join(REPO, "duckduck.example.json"),
    "local": os.path.join(EX, "local", "duckduck.local.json"),
    "semantic-local": os.path.join(EX, "semantic", "duckduck.local.json"),
    "semantic-online": os.path.join(EX, "semantic", "duckduck.online.json"),
}


@pytest.mark.parametrize("name", CONFIGS)
def test_every_config_is_valid_json_with_known_connectors(name):
    config = json.load(open(CONFIGS[name]))
    for service, spec in config["services"].items():
        assert spec.get("connector", service) in SERVICE_REGISTRY, (name, service)
    if "semantic" in config:
        SemanticConfig.from_file_data(config, CONFIGS[name])


def test_reference_catalog_generation_tables_exist():
    """`table:` must be a name auto_register really registers: {service}_{table}."""
    config = json.load(open(CONFIGS["reference"]))
    registered = {
        f"{service}_{table}"
        for service, spec in config["services"].items()
        for table in SERVICE_REGISTRY[spec.get("connector", service)].tables
    }
    for spec in config["semantic"]["catalog_generation"]["tables"]:
        assert spec["table"] in registered, spec


@pytest.mark.parametrize("name", ["reference", "semantic-online"])
def test_prompt_files_referenced_by_configs_exist(name):
    path = CONFIGS[name]
    semantic = json.load(open(path))["semantic"]
    files = [semantic.get("extractor", {}).get("system_prompt_file")]
    gen = semantic.get("catalog_generation", {})
    files += [gen.get("source_prompt_file"), gen.get("link_prompt_file")]
    for f in filter(None, files):
        assert os.path.isfile(os.path.join(os.path.dirname(path), f)), f


def test_offline_semantic_config_explains_the_missing_llm():
    with pytest.raises(ValueError) as info:
        generate_catalog(config_path=CONFIGS["semantic-local"], write=False)
    message = str(info.value)
    assert CONFIGS["semantic-local"] in message and '"ai_providers": {"claude": {"provider": "anthropic", "model": "claude-opus-5"}}' in message
    assert "duckduck.online.json" in message


# ---------------------------------------------------------------------------
# online config, with the LLM / Jev faked
# ---------------------------------------------------------------------------


class ScriptedLLM:
    def generate(self, system, prompt, output_model):
        if output_model is GenVocabulary:
            return GenVocabulary(entities=[GenEntity(name="user", keywords=["users"])])
        # describe the table's real first column (the prompt carries its profile)
        profile = json.loads(prompt.split("Table profile:\n", 1)[1])
        first = next(iter(profile["columns"]))
        return GenSource(description="Sample source.", fields=[GenField(name=first)])


def test_online_generate_catalog_uses_prompt_files_and_never_overwrites_catalog(monkeypatch, tmp_path):
    seen = []

    def build_llm(self, duck, stage=None):
        llm = ScriptedLLM()
        original = llm.generate
        llm.generate = lambda s, p, m: seen.append(s) or original(s, p, m)
        return llm

    monkeypatch.setattr(SemanticConfig, "build_llm", build_llm)
    result = generate_catalog(config_path=CONFIGS["semantic-online"], write=False)
    assert set(result.catalog.sources) == {"proxy_logs", "dns_logs", "firewall_logs", "auth_logs", "asset_inventory"}
    prompts = os.path.join(EX, "semantic", "prompts")
    assert open(os.path.join(prompts, "catalog_source.md")).read() in seen
    assert open(os.path.join(prompts, "catalog_link.md")).read() in seen
    cfg = SemanticConfig.load(DuckAPI(), CONFIGS["semantic-online"])
    assert os.path.basename(cfg.path(cfg.catalog_generation.output_path)) == "generated_catalog.yaml"
    assert cfg.catalog_generation.output_path != cfg.catalog_path


def test_online_jev_check_reads_the_key_from_the_environment(monkeypatch):
    monkeypatch.setenv("JEV_API_KEY", "from-env")
    sent = {}

    def fake_post(self, url, data, timeout):
        sent["auth"] = self.headers["Authorization"]
        options = json.loads(data)["questions"]["answer"]["criteria"]
        r = MagicMock(status_code=200, ok=True)
        r.json.return_value = {"code": 0, "data": {"answers": {"answer": {"probabilities": {o: 1 for o in options}}}}}
        return r

    monkeypatch.setattr("requests.Session.post", fake_post)
    jev_check(config_path=CONFIGS["semantic-online"])
    assert sent["auth"] == "Bearer from-env"


def test_online_jev_check_without_a_key_says_how_to_set_it(monkeypatch):
    monkeypatch.delenv("JEV_API_KEY", raising=False)
    with pytest.raises(ValueError, match="JEV_API_KEY"):
        jev_check(config_path=CONFIGS["semantic-online"])


# ---------------------------------------------------------------------------
# scripts and README snippets run as written
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("script", [os.path.join(EX, "semantic", "demo.py"), os.path.join(EX, "local", "run.py")])
def test_example_scripts_run(script, monkeypatch, tmp_path):
    from duckduck.logs import set_verbose

    monkeypatch.chdir(tmp_path)  # from anywhere
    out = io.StringIO()
    try:
        with contextlib.redirect_stdout(out):
            runpy.run_path(script, run_name="__main__")
    finally:
        set_verbose(False)  # run.py turns verbose on process-wide
    if script.endswith("demo.py"):
        metrics = json.loads(out.getvalue()[out.getvalue().rindex("{"):])
        assert metrics["answer_accuracy"] == 1.0


def _offline_snippets():
    readme = open(os.path.join(EX, "README.md")).read()
    blocks = re.findall(r"```python\n(.*?)```", readme, re.S)
    return [b for b in blocks if "online" not in b]


@pytest.mark.parametrize("snippet", _offline_snippets())
def test_offline_readme_snippets_run_verbatim(snippet, monkeypatch):
    monkeypatch.chdir(REPO)  # the README's paths are from the repo root
    with contextlib.redirect_stdout(io.StringIO()):
        exec(snippet, {})

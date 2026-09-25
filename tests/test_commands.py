"""CLI ↔ Python parity for `python -m duckduck.semantic`, and the docs that promise it."""

import json
import os
import re
from unittest.mock import MagicMock

import pytest

pytest.importorskip("pydantic")

import duckduck.semantic as semantic  # noqa: E402
from duckduck.semantic import ask, connect, generate_catalog, jev_check  # noqa: E402
from duckduck.semantic.__main__ import build_parser, main  # noqa: E402
from duckduck.semantic.generation import GenSource, GenVocabulary, GenField, GenEntity  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCAL_CONFIG = os.path.join(REPO, "examples", "semantic", "duckduck.local.json")
QUESTION = "Which machines communicated with 203.0.113.9?"


def _subcommands():
    parser = build_parser()
    action = next(a for a in parser._actions if a.dest == "command")
    return action.choices  # {name: subparser}


def test_every_subcommand_has_an_exported_python_function():
    for name, sub in _subcommands().items():
        fn = sub.get_default("python")
        assert fn is not None, f"subcommand {name!r} has no Python equivalent"
        assert getattr(semantic, fn.__name__) is fn, f"{fn.__name__} isn't exported from duckduck.semantic"


def test_readme_documents_the_python_equivalent_of_every_cli_command():
    readme = open(os.path.join(REPO, "README.md")).read()
    documented = set(re.findall(r"python -m duckduck\.semantic(?: --config \S+)?(?: -v(?: [a-z]+)?)? ([a-z][a-z-]*)", readme))
    assert documented, "README shows no CLI commands?"
    for name in documented:
        fn = _subcommands()[name].get_default("python")
        assert f"{fn.__name__}(" in readme, f"README shows `{name}` but not `{fn.__name__}(...)`"


# ---------------------------------------------------------------------------
# ask
# ---------------------------------------------------------------------------


def test_ask_python_and_cli_print_the_same(capsys):
    result = ask(QUESTION, config_path=LOCAL_CONFIG)
    assert result.status == "ok" and sorted(result.results["hostname"]) == ["srv-build", "ws-dave"]
    assert main(["--config", LOCAL_CONFIG, "ask", QUESTION]) == 0
    cli = capsys.readouterr().out.strip()
    # identical except the timing on the first line
    strip_ms = lambda text: re.sub(r"\(\d+ ms\)", "(_ ms)", text)  # noqa: E731
    assert strip_ms(cli) == strip_ms(result.report())


def test_ask_plan_only_and_json(capsys):
    planned = ask(QUESTION, config_path=LOCAL_CONFIG, execute=False)
    assert planned.status == "planned" and planned.results is None and planned.sql
    assert main(["--config", LOCAL_CONFIG, "ask", QUESTION, "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "ok" and payload.keys() == ask(QUESTION, config_path=LOCAL_CONFIG).to_dict().keys()


def test_ask_reuses_a_given_duck():
    duck = connect(LOCAL_CONFIG)
    assert ask(QUESTION, config_path=LOCAL_CONFIG, duck=duck).status == "ok"


def test_a_direct_reply_exits_zero_and_prints_it(capsys):
    assert main(["--config", LOCAL_CONFIG, "ask", "How is the weather?"]) == 0
    assert "I couldn't find anything about that in the data I have" in capsys.readouterr().out


def test_clarification_exits_nonzero(capsys):
    assert main(["--config", LOCAL_CONFIG, "ask", "What happened with bob?"]) == 1
    assert "needs_clarification" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# generate-catalog
# ---------------------------------------------------------------------------


class ScriptedLLM:
    def generate(self, system, prompt, output_model):
        if output_model is GenVocabulary:
            return GenVocabulary(entities=[GenEntity(name="host", keywords=["hosts"])])
        return GenSource(description="Synthetic assets.",
                         fields=[GenField(name="hostname", semantic_type="host")], entities=["host"])


@pytest.fixture
def gen_config(tmp_path, monkeypatch):
    (tmp_path / "gen.py").write_text("TABLES = {'assets': lambda limit=None: [{'hostname': 'web-1', 'os': 'linux'}]}\n")
    cfg = tmp_path / "duckduck.json"
    cfg.write_text(json.dumps({
        "services": {"syn": {"connector": "python", "module": "gen.py", "table_prefix": ""}},
        "ai_providers": {"claude": {"authentication": {"type": "local", "api_key": "sk-test"}}},
        "semantic": {"default_llm": "claude", "catalog_generation": {"output_path": "drafted.yaml"}},
    }))
    from duckduck.semantic.config import SemanticConfig

    monkeypatch.setattr(SemanticConfig, "build_llm", lambda self, duck, stage=None: ScriptedLLM())
    return tmp_path, str(cfg)


def test_generate_catalog_python_and_cli(gen_config, capsys):
    folder, cfg = gen_config
    result = generate_catalog(config_path=cfg)
    assert result.path == str(folder / "drafted.yaml") and os.path.isfile(result.path)
    assert list(result.catalog.sources) == ["assets"]

    out = str(folder / "other.yaml")
    assert main(["--config", cfg, "generate-catalog", "--out", out]) == 0
    assert capsys.readouterr().out.startswith(f"wrote {out}: 1 sources")
    assert generate_catalog(config_path=cfg, write=False).path is None


# ---------------------------------------------------------------------------
# jev-check
# ---------------------------------------------------------------------------


def test_jev_check_python_and_cli(tmp_path, monkeypatch, capsys):
    cfg = tmp_path / "duckduck.json"
    cfg.write_text(json.dumps({
        "services": {},
        "ai_providers": {"jev": {"provider": "jev", "authentication": {"type": "local", "api_key": "k"}}},
        "semantic": {"decision_engine": {"ai_provider": "jev"}},
    }))

    def fake_post(self, url, data, timeout):
        options = json.loads(data)["questions"]["answer"]["criteria"]
        r = MagicMock(status_code=200, ok=True)
        r.json.return_value = {"code": 0, "data": {"answers": {"answer": {
            "probabilities": {o: (0.9 if o == "user" else 0.05) for o in options}}}}}
        return r

    monkeypatch.setattr("requests.Session.post", fake_post)
    assert jev_check(config_path=str(cfg)).choice == "user"
    assert main(["--config", str(cfg), "jev-check"]) == 0
    assert capsys.readouterr().out.startswith("Jev OK: [('user', 0.9")


def test_jev_check_without_jev_configured(capsys):
    with pytest.raises(ValueError, match="names no ai_provider"):
        jev_check(config_path=LOCAL_CONFIG)
    assert main(["--config", LOCAL_CONFIG, "jev-check"]) == 1

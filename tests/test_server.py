"""The web app's API (FastAPI TestClient) and the feedback commands (CLI ↔ Python)."""

import json

import pytest

pytest.importorskip("pydantic")
pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

from duckduck.semantic import (Catalog, SemanticSearch, feedback_report, feedback_suggest,  # noqa: E402
                               feedback_to_eval, serve)
from duckduck.semantic.__main__ import main  # noqa: E402
from duckduck.semantic.feedback import FeedbackStore  # noqa: E402
from duckduck.semantic.server import create_app  # noqa: E402

from test_answer_shapes import CATALOG, _duck  # noqa: E402


@pytest.fixture
def project(tmp_path):
    (tmp_path / "catalog.json").write_text(json.dumps(CATALOG))
    return tmp_path


def _client(project, token=None):
    store = FeedbackStore(str(project / "feedback.duckdb"))
    catalog = str(project / "catalog.json")

    def factory():
        return SemanticSearch(Catalog.load(catalog), _duck(), feedback=store, memory=True)

    app = create_app(factory, store, catalog_path=catalog,
                     learned_shapes_path=str(project / "answer_shapes.learned.yaml"), min_support=1, token=token)
    return TestClient(app), store


def test_page_and_meta(project):
    client, _ = _client(project)
    assert "<title>Duckduck Ask</title>" in client.get("/").text
    meta = client.get("/api/meta").json()
    assert meta["verdicts"] == ["answered", "partial", "not_answered"] and "wrong_tables" in meta["categories"]
    assert set(meta["sources"]) == {"alerts", "owners"} and meta["values"]["alerts.severity"] == ["critical", "high", "low"]
    assert meta["answer_shapes"]["lookup"].startswith("everything about it") and meta["can_accept"]


def test_ask_answer_rate(project):
    client, store = _client(project)
    first = client.post("/api/ask", json={"question": "How many alerts per rule?", "user": "ana"}).json()
    assert first["done"] and first["result"]["status"] == "ok"
    assert first["result"]["results"][0] == {"rule": "brute_force", "count": 3}
    sid = first["result"]["search_id"]
    assert client.post("/api/feedback", json={"search_id": sid, "verdict": "answered", "user": "ana"}).status_code == 200
    assert client.post("/api/feedback", json={"search_id": sid, "verdict": "meh"}).status_code == 400
    assert client.post("/api/feedback", json={"search_id": "nope", "verdict": "answered"}).status_code == 404
    history = client.get("/api/searches").json()
    assert history[0]["question"] == "How many alerts per rule?" and history[0]["verdict"] == "answered"
    assert client.get(f"/api/searches/{sid}").json()["answer_shape"] == "count_by"
    stats = client.get("/api/stats").json()
    assert stats["overall"]["answer_rate"] == 1.0 and stats["by_answer_shape"][0]["key"] == "count_by"


def test_a_clarification_round_trip(project):
    client, _ = _client(project)
    asked = client.post("/api/ask", json={"question": "What is the weather like?"}).json()
    assert not asked["done"] and asked["result"]["followup"]["options"]
    answered = client.post("/api/answer", json={"conversation_id": asked["conversation_id"], "reply": "1"}).json()
    assert answered["history"][0]["understood"] is not None
    assert answered["conversation_id"] == asked["conversation_id"]
    assert client.post("/api/answer", json={"conversation_id": "gone", "reply": "1"}).status_code == 404


def test_suggestions_accept_and_dismiss(project):
    client, store = _client(project)
    sid = client.post("/api/ask", json={"question": "Which alerts are urgent?"}).json()["result"]["search_id"]
    client.post("/api/feedback", json={"search_id": sid, "verdict": "not_answered", "categories": ["wrong_values"],
                                       "expected": {"synonym": {"field": "alerts.severity", "value": "critical",
                                                                "word": "urgent"},
                                                    "sources": ["owners"]}})
    items = client.get("/api/suggestions").json()
    kinds = {s["kind"]: s for s in items}
    assert kinds["value_synonym"]["questions"] == ["Which alerts are urgent?"]
    accepted = client.post(f"/api/suggestions/{kinds['value_synonym']['id']}/accept", json={"user": "ana"}).json()
    assert "urgent" in accepted["applied"]
    assert "urgent" in Catalog.load(str(project / "catalog.json")).sources["alerts"].fields["severity"].values["critical"]
    client.post(f"/api/suggestions/{kinds['source_example']['id']}/dismiss", json={})
    assert client.get("/api/suggestions").json() == []
    # the accepted synonym is in the catalog now — nothing left to suggest; the dismissed one stays on record
    assert [s["status"] for s in client.get("/api/suggestions?all=1").json()] == ["dismissed"]
    # the reloaded search reads the new synonym
    urgent = client.post("/api/ask", json={"question": "Which hosts have urgent alerts?"}).json()["result"]
    assert urgent["status"] == "ok" and {"ip": "10.0.0.1"} in urgent["results"]


def test_evaluate(project):
    client, _ = _client(project)
    assert client.post("/api/evaluate", json={}).json()["size"] == 0
    sid = client.post("/api/ask", json={"question": "How many alerts per rule?"}).json()["result"]["search_id"]
    client.post("/api/feedback", json={"search_id": sid, "verdict": "answered"})
    report = client.post("/api/evaluate", json={}).json()
    assert report["size"] == 1 and report["metrics"]["answer_shape_accuracy"] == 1.0 and "thresholds" in report
    dataset = client.get("/api/evaluation.json").json()
    assert dataset[0]["expected_answer_shape"] == "count_by"


def test_a_token_guards_the_api(project):
    client, _ = _client(project, token="s3cret")
    assert client.get("/").status_code == 200  # the page itself, so it can ask for the token
    assert client.get("/api/meta").status_code == 401
    assert client.get("/api/meta", headers={"X-Duckduck-Token": "wrong"}).status_code == 401
    assert client.get("/api/meta", headers={"X-Duckduck-Token": "s3cret"}).status_code == 200


# ---------------------------------------------------------------------------
# Commands


@pytest.fixture
def config(project):
    (project / "tables.py").write_text(
        "import pandas as pd\n"
        "def tables():\n"
        "    alerts = pd.DataFrame({'ip': ['10.0.0.1', '10.0.0.2'], 'rule': ['brute_force', 'malware'],"
        " 'severity': ['low', 'critical']})\n"
        "    owners = pd.DataFrame({'ip': ['10.0.0.1'], 'owner': ['ana'], 'department': ['eng']})\n"
        "    return {'alerts': lambda limit=None: alerts, 'owners': lambda limit=None: owners}\n")
    path = project / "duckduck.json"
    path.write_text(json.dumps({
        "services": {"t": {"connector": "python", "module": "tables.py", "table_prefix": ""}},
        "semantic": {"catalog_path": "catalog.json", "feedback": {"enabled": True, "min_support": 1}},
    }))
    return str(path)


def test_serve_builds_the_app_and_records(config, project):
    client = TestClient(serve(config_path=config, run=False))
    sid = client.post("/api/ask", json={"question": "How many alerts per rule?"}).json()["result"]["search_id"]
    client.post("/api/feedback", json={"search_id": sid, "verdict": "not_answered",
                                       "expected": {"synonym": {"field": "alerts.severity", "value": "critical",
                                                                "word": "urgent"}}})
    client.app.state.duckduck["search"].feedback_store.close()


def test_feedback_commands_cli_and_python_print_the_same(config, project, capsys):
    test_serve_builds_the_app_and_records(config, project)
    report = feedback_report(config_path=config)
    assert "1 searches" in report.summary() and "answered 0% of rated" in report.summary()
    assert main(["--config", config, "feedback-report"]) == 0
    assert capsys.readouterr().out.strip() == report.summary()

    evaluation = feedback_to_eval(config_path=config)
    assert evaluation.dataset == [] and "no rated questions" in evaluation.summary()  # a synonym labels no reading
    assert main(["--config", config, "feedback-to-eval"]) == 0
    assert capsys.readouterr().out.strip() == evaluation.summary()

    suggestions = feedback_suggest(config_path=config)
    [s] = suggestions.suggestions
    assert s.kind == "value_synonym"
    assert main(["--config", config, "feedback-suggest"]) == 0
    assert capsys.readouterr().out.strip() == suggestions.summary()
    done = feedback_suggest(config_path=config, accept=[s.id])
    assert done.applied and done.suggestions == []
    assert "urgent" in Catalog.load(str(project / "catalog.json")).sources["alerts"].fields["severity"].values["critical"]
    with pytest.raises(ValueError, match="no open suggestion"):
        feedback_suggest(config_path=config, dismiss=[s.id])


def test_feedback_to_eval_writes_the_set_and_scores_it(config, project):
    client = TestClient(serve(config_path=config, run=False))
    sid = client.post("/api/ask", json={"question": "How many alerts per rule?"}).json()["result"]["search_id"]
    client.post("/api/feedback", json={"search_id": sid, "verdict": "answered"})
    client.app.state.duckduck["search"].feedback_store.close()
    evaluation = feedback_to_eval(config_path=config)
    written = json.loads((project / "feedback_evaluation.json").read_text())
    assert written[0]["expected_answer_shape"] == "count_by" and evaluation.report.metrics["answer_shape_accuracy"] == 1.0
    assert evaluation.summary().startswith("wrote 1 rated questions") and '"thresholds"' in evaluation.summary()

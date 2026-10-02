"""API requests tried again after a timeout, a dropped connection, 429 or 5xx — set per service and per run."""

import logging

import pandas as pd
import pytest
import requests

from duckduck import DuckAPI
from duckduck.common import progress
from duckduck.pipeline import PipelineError, run_pipeline
from duckduck.common.retry import RetryPolicy, policy_problem, send
from duckduck.connectors.api.servicenow import ServiceNow


class _Response:
    def __init__(self, status=200, rows=(), headers=None):
        self.status_code, self.headers, self._rows = status, headers or {}, list(rows)
        self.url = "https://x.service-now.com/api/now/table/incident"

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code}", response=self)

    def json(self):
        return {"result": self._rows}


class _Flaky:
    """A session whose first answers fail: a timeout, then a 503, then the rows."""

    def __init__(self, failures):
        self.failures, self.calls, self.timeouts = list(failures), 0, []

    def get(self, url, params=None, timeout=None):
        self.calls += 1
        self.timeouts.append(timeout)
        if self.failures:
            failure = self.failures.pop(0)
            if isinstance(failure, Exception):
                raise failure
            return _Response(failure)
        return _Response(200, [{"number": "INC1", "state": "1"}])


def _sn(failures, **retry):
    sn = ServiceNow("x", "u", "p")
    sn.session = _Flaky(failures)
    sn.retry = RetryPolicy(backoff=0).with_(retry or None)
    return sn


def test_a_timeout_and_a_503_are_tried_again_then_the_rows_come(caplog):
    sn = _sn([requests.ReadTimeout("read timed out"), 503])
    with caplog.at_level(logging.WARNING, logger="duckduck"):
        assert sn.table("incident")["number"].tolist() == ["INC1"]
    assert sn.session.calls == 3
    lines = [r.getMessage() for r in caplog.records]
    assert any("ReadTimeout (no answer in time); try 2 of 4" in line for line in lines)
    assert any("HTTP 503; try 3 of 4" in line for line in lines)


def test_giving_up_says_how_many_tries_and_what_to_change():
    sn = _sn([requests.ReadTimeout("read timed out")] * 3, retries=2, timeout=5)
    with pytest.raises(requests.ReadTimeout, match=r'gave up after 3 tries of up to 5s each; raise the service.s "retry"'):
        sn.table("incident")
    assert sn.session.calls == 3 and sn.session.timeouts == [5, 5, 5]


def test_no_retries_and_statuses_not_retried():
    sn = _sn([requests.ConnectionError("reset")], retries=0)
    with pytest.raises(requests.ConnectionError, match="gave up after 1 try"):
        sn.table("incident")
    sn = _sn([404])
    with pytest.raises(requests.HTTPError, match="404"):
        sn.table("incident")
    assert sn.session.calls == 1  # a 404 isn't worth a second try
    other = _sn([500, 500], statuses=[503])
    with pytest.raises(requests.HTTPError, match="500"):
        other.table("incident")
    assert other.session.calls == 1


def test_the_wait_doubles_is_capped_and_follows_retry_after():
    waits = []
    policy = RetryPolicy(retries=4, backoff=1, max_wait=3)
    answers = [_Response(503), _Response(503), _Response(429, headers={"Retry-After": "2"}), _Response(503),
               _Response(200)]
    assert send(policy, "t", lambda timeout: answers.pop(0), 10, sleep=waits.append).status_code == 200
    assert sum(waits) == 1 + 2 + 2 + 3  # 1, 2, Retry-After 2, then 8 capped at 3 (slept in slices)


def test_a_cancel_stops_the_wait():
    p = progress.Progress()
    calls = []

    def call(timeout):
        calls.append(1)
        p.cancel()
        return _Response(503)

    with progress.tracking(p), pytest.raises(progress.Cancelled):
        send(RetryPolicy(backoff=30), "t", call, 10)
    assert len(calls) == 1


def test_the_setting_is_checked():
    assert policy_problem({"retries": -1}).startswith("retry.retries is a whole number")
    assert policy_problem({"tries": 3}).startswith("retry: unknown key(s) tries")
    assert policy_problem({"statuses": ["503"]}).startswith("retry.statuses is a list")
    assert RetryPolicy().with_(False).retries == 0
    assert RetryPolicy().with_({"statuses": [503]}).statuses == (503,)


def test_auto_register_takes_retry_per_service():
    duck = DuckAPI()
    auth = {"username": "u", "password": "p"}
    sn = duck.auto_register({"sn": {"connector": "servicenow", "instance": "x", "authentication": auth,
                                    "retry": {"retries": 6, "timeout": 120}}})["sn"]
    assert (sn.retry.retries, sn.retry.timeout, sn.retry.backoff) == (6, 120, 2.0)
    with pytest.raises(ValueError, match="'sn': retry.timeout is the seconds"):
        DuckAPI().auto_register({"sn": {"connector": "servicenow", "instance": "x", "authentication": auth,
                                        "retry": {"timeout": 0}}})


def test_validate_config_knows_retry():
    from duckduck.semantic.admin import validate_config

    service = {"connector": "servicenow", "instance": "x", "authentication": {"username": "u", "password": "p"}}
    assert validate_config({"services": {"sn": {**service, "retry": {"retries": 5}}}}) == {"errors": [], "warnings": []}
    assert validate_config({"services": {"sn": {**service, "retry": {"retries": "5"}}}})["errors"] == [
        "services.sn.retry.retries is a whole number ≥ 0 (how many times a failed request is tried again)"]


def test_a_pipeline_sets_retry_for_its_run_and_puts_it_back(tmp_path):
    sn = _sn([])
    sn.retry = RetryPolicy(retries=1)
    seen = []
    duck = DuckAPI()

    def incidents(limit=None):
        seen.append(sn.retry)
        return pd.DataFrame({"id": [1]})

    incidents.__self__ = sn  # what a connector's bound method carries
    duck.functions["sn_incident"] = incidents
    duck.service_of["sn_incident"] = "servicenow"
    spec = {"pipeline": "p", "sql": "SELECT * FROM sn_incident", "target": str(tmp_path / "o"),
            "retry": {"retries": 8, "timeout": 300}}
    run_pipeline(spec, duck=duck)
    assert (seen[-1].retries, seen[-1].timeout) == (8, 300) and sn.retry.retries == 1  # this run only
    run_pipeline({**spec, "retry": {"servicenow": {"timeout": 90}}}, duck=duck)
    assert (seen[-1].retries, seen[-1].timeout) == (1, 90)  # on top of the service's own
    run_pipeline({**spec, "retry": False}, duck=duck)
    assert seen[-1].retries == 0 and sn.retry.retries == 1
    with pytest.raises(PipelineError, match="retry: no connector jira"):
        run_pipeline({**spec, "retry": {"jira": {"retries": 2}}}, duck=duck)
    with pytest.raises(PipelineError, match="'retry' for this run: retry.retries is a whole number"):
        run_pipeline({**spec, "retry": {"retries": "x"}}, duck=duck)

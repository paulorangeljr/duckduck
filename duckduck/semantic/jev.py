"""
HTTP client for the Jev decisions API — the ``JEVBackend`` that
``JEVAdapter`` wraps.

Uses Jev's *native* endpoint (``POST /api/v1/decisions``) rather than one
of its presets, since none of them (tool-guard, model-route, ...) fits
catalog reasoning:

- a yes/no judgment → one ``noul`` question (returns P(yes));
- a pick-one judgment → one ``choice`` question, the option descriptions
  as its ``criteria``;
- several yes/no judgments on the same state (e.g. "is each of these 5
  sources relevant?") → one request with several ``noul`` questions, via
  ``decide_batch``.

Auth is ``Authorization: Bearer <key>`` (a personal key from Jev's
``/agent/keys``). Responses are ``{code, message, data}``; ``code == 0`` is
success and each question's result sits under ``data.answers.<key>``.

The per-answer shape is parsed defensively (a bare number, or an object
with ``noul`` / ``probabilities`` / ``choice`` + ``confidence``) — the
public docs describe the fields but not every envelope variant, so an
unrecognized answer raises ``JevAPIError`` quoting what came back rather
than guessing.
"""

import json
import logging
import os
import time
from typing import Any, Dict, List, Mapping, Optional

import requests

#: Jev caps request bodies at 32 KiB.
MAX_BODY_BYTES = 32 * 1024

logger = logging.getLogger("duckduck.semantic.jev")
#: Long catalog descriptions are trimmed to keep requests well under the cap.
_MAX_TEXT = 1500


class JevAPIError(RuntimeError):
    """
    A Jev call failed. ``retryable`` tells ``JEVAdapter`` whether trying
    again can help (rate limit, 5xx, network) or not (bad key, bad
    request, oversized body, unparseable answer).
    """

    def __init__(self, message: str, retryable: bool = False, status: Optional[int] = None):
        super().__init__(message)
        self.retryable = retryable
        self.status = status


def _trim(text: Any) -> Any:
    if isinstance(text, str) and len(text) > _MAX_TEXT:
        return text[:_MAX_TEXT] + " …"
    return text


class JevClient:
    """
    Parameters
    ----------
    api_key : str, optional
        Defaults to the ``JEV_API_KEY`` environment variable. In a config
        file, prefer an ``authentication`` block (aws/azure secret) over a
        literal key — see ``SemanticSearch.from_config``.
    model : str, optional
        A Jev model identifier (e.g. ``"typesafe-ai/jev"``); omitted →
        the server's default.
    url : str, optional
        The full Decisions API endpoint, for any host that serves it —
        e.g. ``OPENROUTER_DECISIONS_URL`` (Jev on OpenRouter, model
        ``typesafe/jev-1.13``). Omitted → ``{base_url}/api/v1/decisions``.
    api_key_env : str
        The environment variable the key falls back to.
    """

    DEFAULT_BASE_URL = "https://www.jevai.org"
    OPENROUTER_DECISIONS_URL = "https://openrouter.ai/api/alpha/decisions"

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: str = DEFAULT_BASE_URL,
        model: Optional[str] = None,
        timeout: float = 15.0,
        session: Optional[requests.Session] = None,
        url: Optional[str] = None,
        api_key_env: str = "JEV_API_KEY",
    ):
        api_key = api_key or os.environ.get(api_key_env)
        if not api_key:
            raise ValueError(f"The Decisions API needs an API key: pass api_key=, or set {api_key_env}.")
        self.url = url or base_url.rstrip("/") + "/api/v1/decisions"
        self.model = model
        self.timeout = timeout
        self.session = session or requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        })

    @classmethod
    def from_secret(cls, secret: Mapping[str, Any], **overrides) -> "JevClient":
        """Builds from a resolved ``authentication`` block (needs ``api_key``)."""
        if "api_key" not in secret:
            raise ValueError("Jev credentials need an 'api_key' field.")
        return cls(api_key=secret["api_key"], **overrides)

    # ------------------------------------------------------------------
    # JEVBackend protocol
    # ------------------------------------------------------------------

    def decide(self, state: Dict[str, Any], question: str, options: List[str]) -> Mapping[str, float]:
        answers = self._post(self._state(state), {"answer": {"type": "noul", "instructions": question}})
        p_yes = self._parse_noul(answers, "answer")
        return {options[0]: p_yes, options[1]: 1.0 - p_yes}

    def classify(self, state: Dict[str, Any], question: str, options: List[str]) -> Mapping[str, float]:
        descriptions = state.get("options") or {}
        criteria = {label: _trim(descriptions.get(label) or label) for label in options}
        answers = self._post(self._state(state), {
            "answer": {"type": "choice", "instructions": question, "criteria": criteria},
        })
        return self._parse_choice(answers, "answer", options)

    def ask(self, state: Dict[str, Any], questions: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
        """
        Mixed ``noul``/``choice`` questions on one state → key → P(yes) or
        ``{option: probability}``. One request — split in as many as it takes
        when the body would go over the API's 32 KiB cap.
        """
        spec = {
            key: {**q, **({"criteria": {c: _trim(v) for c, v in q["criteria"].items()}} if q.get("criteria") else {})}
            for key, q in questions.items()
        }
        answers = self._post_fitting(self._state(state), spec)
        return {
            key: self._parse_choice(answers, key, list(q["criteria"])) if q["type"] == "choice"
            else self._parse_noul(answers, key)
            for key, q in spec.items()
        }

    def _post_fitting(self, state: Dict[str, Any], questions: Dict[str, Any]) -> Dict[str, Any]:
        body = {"state": state, "questions": questions, **({"model": self.model} if self.model else {})}
        if len(questions) > 1 and len(json.dumps(body, default=str).encode("utf-8")) > MAX_BODY_BYTES:
            keys = list(questions)
            answers: Dict[str, Any] = {}
            for half in (keys[: len(keys) // 2], keys[len(keys) // 2:]):
                part_state = dict(state)
                if "items" in state:
                    part_state["items"] = {k: v for k, v in state["items"].items() if k in half}
                answers.update(self._post_fitting(part_state, {k: questions[k] for k in half}))
            return answers
        return self._post(state, questions)

    def decide_batch(self, state: Dict[str, Any], questions: Mapping[str, str]) -> Dict[str, float]:
        """Several yes/no questions on one state, one request → ``{key: P(yes)}``."""
        answers = self._post(self._state(state), {
            key: {"type": "noul", "instructions": text} for key, text in questions.items()
        })
        return {key: self._parse_noul(answers, key) for key in questions}

    # ------------------------------------------------------------------

    @staticmethod
    def _state(state: Dict[str, Any]) -> Dict[str, Any]:
        """Compact business fields only — no lexical hints, nothing empty."""
        out: Dict[str, Any] = {"user_question": state.get("query", "")}
        if state.get("subject"):
            out["subject"] = _trim(state["subject"])
        if state.get("candidates"):
            out["candidates"] = {k: _trim(v) for k, v in state["candidates"].items()}
        if state.get("facts"):
            out["facts"] = state["facts"]
        if state.get("prior") is not None:
            out["catalog_prior_probability"] = state["prior"]
        if state.get("items"):
            out["items"] = {
                key: {k: (_trim(v) if k == "subject" else v) for k, v in item.items()}
                for key, item in state["items"].items()
            }
        return out

    def _post(self, state: Dict[str, Any], questions: Dict[str, Any]) -> Dict[str, Any]:
        body: Dict[str, Any] = {"state": state, "questions": questions}
        if self.model:
            body["model"] = self.model
        payload = json.dumps(body, default=str)
        if len(payload.encode("utf-8")) > MAX_BODY_BYTES:
            raise JevAPIError(f"request body is {len(payload)} bytes, over Jev's 32 KiB cap")
        started = time.perf_counter()
        try:
            r = self.session.post(self.url, data=payload, timeout=self.timeout)
        except requests.RequestException as exc:
            raise JevAPIError(f"Jev request failed: {exc}", retryable=True) from exc
        if r.status_code == 429 or r.status_code >= 500:
            raise JevAPIError(f"Jev HTTP {r.status_code}: {r.text[:300]}", retryable=True, status=r.status_code)
        if not r.ok:
            raise JevAPIError(f"Jev HTTP {r.status_code}: {r.text[:300]}", status=r.status_code)
        try:
            envelope = r.json()
        except ValueError as exc:
            raise JevAPIError(f"Jev returned non-JSON: {r.text[:300]}") from exc
        # TypeSafe's own API wraps it as {code, message, data: {answers}};
        # OpenRouter's returns {id, model, answers, usage} at the top level.
        if "code" in envelope and envelope.get("code") != 0:
            raise JevAPIError(f"Jev error code {envelope.get('code')}: {envelope.get('message')}")
        answers = envelope.get("answers", (envelope.get("data") or {}).get("answers"))
        if not isinstance(answers, dict):
            raise JevAPIError(f"Decisions API response has no answers: {str(envelope)[:300]}")
        self._log_usage(envelope, len(questions), time.perf_counter() - started)
        return answers

    #: Running total of what the API reported as cost (USD) — OpenRouter reports it per call.
    total_cost: float = 0.0

    def _log_usage(self, envelope: Dict[str, Any], n_questions: int, seconds: float) -> None:
        try:
            usage = envelope.get("usage") or {}
            cost = usage.get("cost")
            if isinstance(cost, (int, float)):
                self.total_cost += cost
            logger.info(
                "  decisions %s: %d question%s · %.2fs%s%s", envelope.get("model") or self.model or "",
                n_questions, "" if n_questions == 1 else "s", seconds,
                f" · {usage['input_tokens']:,} tokens in" if isinstance(usage.get("input_tokens"), int) else "",
                f" · ${cost:.6f} (session ${self.total_cost:.6f})" if isinstance(cost, (int, float)) else "",
            )
        except Exception as exc:  # diagnostics must never break the call
            logger.debug("could not log decisions usage: %r", exc)

    @staticmethod
    def _parse_noul(answers: Dict[str, Any], key: str) -> float:
        raw = answers.get(key)
        value = raw
        if isinstance(raw, dict):
            value = next((raw[k] for k in ("noul", "probability", "yes") if k in raw), None)
        try:
            p = float(value)
        except (TypeError, ValueError):
            raise JevAPIError(f"can't read a noul probability for '{key}' from {raw!r}")
        if not 0.0 <= p <= 1.0:
            raise JevAPIError(f"noul for '{key}' out of [0, 1]: {p}")
        return p

    @staticmethod
    def _parse_choice(answers: Dict[str, Any], key: str, options: List[str]) -> Dict[str, float]:
        raw = answers.get(key)
        if isinstance(raw, dict):
            probs = raw.get("probabilities")
            if isinstance(probs, dict) and probs:
                return {str(k): float(v) for k, v in probs.items()}
            if raw.get("choice") in options and raw.get("confidence") is not None:
                conf = float(raw["confidence"])
                rest = (1.0 - conf) / max(len(options) - 1, 1)
                return {o: (conf if o == raw["choice"] else rest) for o in options}
        raise JevAPIError(f"can't read choice probabilities for '{key}' from {raw!r}")

"""
Decisions made by a chat model — the decision engine over any
``ai_providers`` entry that isn't Jev's native API (OpenRouter, Claude,
Azure OpenAI...). That is also how Jev itself is reached through OpenRouter.

``LLMDecisionBackend`` implements the same ``JEVBackend`` contract as
``JevClient`` and is wrapped by the same ``JEVAdapter`` (retries, timeout,
normalization, batched source relevance), so the rest of the pipeline
can't tell them apart. The model only ever returns probabilities through
a structured output — never free text, never SQL.
"""

import json
from typing import Any, Dict, List, Mapping

from pydantic import BaseModel, Field

from .jev import JevClient
from .llm import LLMClient

DEFAULT_DECISION_PROMPT = """\
You are the decision function of a semantic search system over security
data. You never answer the user's question yourself: you receive the
user's question, a small JSON state (catalog facts and a prior when there
is one) and ONE judgment to make about it, and you return calibrated
probabilities for that judgment only.

- Yes/no judgments: the probability that the answer is yes.
- Choices: a probability for every option given, summing to 1.
- Several yes/no judgments at once: one probability per key given.

Calibrate: 0.5 means you cannot tell; reserve values above 0.9 or below
0.1 for cases the question and the state make unambiguous. When the state
carries catalog_prior_probability, treat it as the catalog's own estimate
and move away from it only on evidence in the question.
"""


class _YesNo(BaseModel):
    probability: float = Field(description="P(yes), between 0 and 1")


class _Scored(BaseModel):
    key: str
    probability: float


class _Scores(BaseModel):
    scores: List[_Scored]


class LLMDecisionBackend:
    """``JEVBackend`` over an ``LLMClient`` with structured output."""

    def __init__(self, llm: LLMClient, system_prompt: str = DEFAULT_DECISION_PROMPT):
        self.llm = llm
        self.system_prompt = system_prompt

    def decide(self, state: Dict[str, Any], question: str, options: List[str]) -> Mapping[str, float]:
        answer = self.llm.generate(self.system_prompt, self._prompt(state, question), _YesNo)
        p = min(max(float(answer.probability), 0.0), 1.0)
        return {options[0]: p, options[1]: 1.0 - p}

    def classify(self, state: Dict[str, Any], question: str, options: List[str]) -> Mapping[str, float]:
        descriptions = state.get("options") or {}
        prompt = self._prompt(state, question, options={o: descriptions.get(o, o) for o in options})
        answer = self.llm.generate(self.system_prompt, prompt, _Scores)
        return {s.key: s.probability for s in answer.scores}

    def decide_batch(self, state: Dict[str, Any], questions: Mapping[str, str]) -> Dict[str, float]:
        prompt = self._prompt(state, "Answer each yes/no judgment below, one score per key.",
                              judgments=dict(questions))
        answer = self.llm.generate(self.system_prompt, prompt, _Scores)
        got = {s.key: min(max(float(s.probability), 0.0), 1.0) for s in answer.scores}
        return {key: got.get(key, 0.5) for key in questions}

    @staticmethod
    def _prompt(state: Dict[str, Any], question: str, **extra: Any) -> str:
        # the same compact state Jev's native API gets (no lexical hints)
        body = {"judgment": question, "state": JevClient._state(state), **extra}
        return json.dumps(body, indent=1, default=str)

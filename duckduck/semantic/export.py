"""
The still-failing questions, as a brief for whoever changes the code.

Suggestions (``suggest.py``) fix what the catalog can fix: words, synonyms,
example questions, wording. What keeps failing after that is usually a
missing capability or a bug — work for a developer. ``build_export`` turns
the feedback into a Markdown document they can act on: each gap once
(questions grouped by template, so "10.0.0.1" and "10.0.0.2" asked the same
way are one gap), how often and by how many people, what the system did
(answer kind, tables, what it asked back, every decision with its
probability, the SQL), what users said and what they said would have been
right, which open suggestion might already fix it, and how to reproduce it.

Only templates whose *latest* rating is still not "answered" are gaps — a
question that has been answered since isn't reported.

``redact=True`` shows templates instead of questions ("what can I find for
<ip_address>"), and leaves out the SQL and the English reading. Users'
free-text reasons are kept as written: read the document before sharing
it outside the team.
"""

import datetime as dt
from collections import defaultdict
from typing import Any, Dict, List, Optional

from .feedback import CATEGORIES

_VERDICT = {"not_answered": "not answered", "partial": "partly answered"}


def build_export(
    store: Any,
    suggestions: Optional[List[Any]] = None,
    since: Optional[dt.datetime] = None,
    limit: int = 50,
    include_partial: bool = True,
    redact: bool = False,
    title: str = "Semantic search — questions that still fail",
    config_path: Optional[str] = None,
) -> Dict[str, Any]:
    """``{"markdown": str, "gaps": [...], "counts": {...}}`` — see the module docstring."""
    rows = [r for r in store.rated() if since is None or _as_dt(r["rated_at"]) >= since]
    by_template: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_template[row["template"]].append(row)

    failing = {"not_answered", "partial"} if include_partial else {"not_answered"}
    gaps = []
    for template, group in by_template.items():
        group.sort(key=lambda r: str(r["rated_at"]))
        if group[-1]["verdict"] not in failing:
            continue  # answered since (or never failed)
        failures = [r for r in group if r["verdict"] in failing]
        gaps.append({"template": template, "latest": group[-1], "failures": failures,
                     "users": sorted({r.get("user_name") or "anonymous" for r in failures})})
    gaps.sort(key=lambda g: str(g["latest"]["rated_at"]), reverse=True)  # newest first ...
    gaps.sort(key=lambda g: len(g["failures"]), reverse=True)  # ... within the most frequent first
    total_gaps = len(gaps)
    gaps = gaps[:limit]

    fixes: Dict[str, List[Any]] = defaultdict(list)
    for s in suggestions or []:
        for sid in s.evidence:
            fixes[sid].append(s)

    stats = store.stats()
    o = stats["overall"]
    lines = [f"# {title}", ""]
    lines.append(f"_Generated {dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} from "
                 f"`{store.path}`{' since ' + since.strftime('%Y-%m-%d') if since else ''}._")
    lines += ["", "## Summary", ""]
    rate = "—" if o["answer_rate"] is None else f"{o['answer_rate']:.0%}"
    asked = "—" if o["asked_back_rate"] is None else f"{o['asked_back_rate']:.0%}"
    lines.append(f"- {o['searches']} searches, {o['rated']} rated, **{rate} answered** (of rated); "
                 f"asked a question back in {asked} of searches.")
    lines.append(f"- **{total_gaps} question pattern(s) still failing**"
                 + (f" (the {len(gaps)} most frequent below)" if total_gaps > len(gaps) else "") + ".")
    cats = stats["by_category"]
    if not cats.empty:
        lines.append("- What users said went wrong: " + ", ".join(
            f"{r['label']} ({r['count']})" for r in cats.to_dict(orient="records")) + ".")
    open_suggestions = [s for s in (suggestions or []) if s.status == "open"]
    if open_suggestions:
        lines.append(f"- {len(open_suggestions)} catalog suggestion(s) are waiting for review — accept those first "
                     f"(web app → Suggestions, or `python -m duckduck.semantic feedback-suggest`); they may fix "
                     f"some gaps without code.")

    lines += ["", "## How to use this", "",
              "Each gap below is a question pattern users keep rating as not answered. For each one:",
              "",
              ("1. Reproduce it: the questions are hidden here — look them up by their search ids in the "
               "feedback file, then run them with `ask -v debug`." if redact else
               "1. Reproduce it: the command under the gap runs the question with full logging."),
              "2. Decide whether it's the **catalog** (a missing word, field description, relationship — "
              "fix the catalog or accept a suggestion) or the **code** (a kind of answer that doesn't "
              "exist, a wrong decision, a bug).",
              "3. After the change, `python -m duckduck.semantic feedback-to-eval` replays every rated "
              "question: what users confirmed must stay right.",
              ""]
    if not gaps:
        lines += ["## Gaps", "", "None — every rated question pattern was answered in its latest rating.", ""]

    command = "python -m duckduck.semantic" + (f" --config {_shell(config_path)}" if config_path else "")
    for i, gap in enumerate(gaps, 1):
        lines += _gap(i, gap, fixes, redact, command)

    lines += ["## Open suggestions", ""]
    if open_suggestions:
        lines += ["| id | kind | target | change | questions |", "|---|---|---|---|---|"]
        lines += [f"| `{s.id}` | {s.kind} | {_cell(s.target or '')} | {_cell(s.change)} | {len(s.evidence)} |"
                  for s in open_suggestions]
    else:
        lines.append("None.")
    lines.append("")
    return {"markdown": "\n".join(lines), "gaps": gaps, "counts": {"gaps": total_gaps, "shown": len(gaps)}}


def _gap(i: int, gap: Dict[str, Any], fixes: Dict[str, List[Any]], redact: bool, command: str) -> List[str]:
    latest, failures = gap["latest"], gap["failures"]
    question = gap["template"] if redact else latest["question"]
    verdicts = ", ".join(f"{sum(r['verdict'] == v for r in failures)}× {label}"
                         for v, label in _VERDICT.items() if any(r["verdict"] == v for r in failures))
    people = f"{len(gap['users'])} user{'s' if len(gap['users']) != 1 else ''}"
    lines = [f"## {i}. “{question}”", "",
             f"{verdicts} · {people} · last {str(latest['rated_at'])[:16]}", ""]
    if latest.get("english_question") and not redact:
        lines.append(f"- **Read as:** {latest['english_question']}")
    if len({r['question'] for r in failures}) > 1 and not redact:
        others = sorted({r["question"] for r in failures} - {latest["question"]})[:4]
        lines.append("- **Also asked as:** " + "; ".join(f"“{q}”" for q in others))
    what = [f"status `{latest['status']}`", f"answer `{latest.get('answer_shape') or '—'}`"]
    if latest.get("sources"):
        what.append("tables " + ", ".join(f"`{s}`" for s in latest["sources"]))
    if latest.get("rows") is not None:
        what.append(f"{latest['rows']} rows")
    lines.append("- **What it did:** " + " · ".join(what))
    if latest.get("clarification"):
        lines.append(f"- **It said / asked back:** {latest['clarification']}")
    said = []
    for r in failures[-5:]:
        cats = ", ".join(CATEGORIES.get(c, c) for c in (r.get("categories") or []))
        text = (r.get("reason") or "").strip()
        who = r.get("user_name") or "anonymous"
        if cats or text:
            said.append(f"  - {who}: {cats + (' — ' if cats and text else '')}{'“' + text + '”' if text else ''}")
    if said:
        lines.append("- **What users said:**")
        lines += said
    expected = [r["expected"] for r in failures if r.get("expected")]
    if expected:
        lines.append("- **What would have been right:** " + "; ".join(_expected(e) for e in expected[-3:]))
    related = {s.id: s for r in failures for s in fixes.get(r["id"], [])}
    for s in related.values():
        lines.append(f"- **May be fixed by suggestion `{s.id}`** ({s.status}): {s.kind} "
                     f"{s.target or ''} → “{s.change}”")
    decisions = latest.get("decisions") or []
    if decisions:
        lines += ["", "| decision | about | answer | p | by |", "|---|---|---|---|---|"]
        lines += [f"| {d['kind']} | {_cell(d.get('subject') or '')} | {_cell(_short(d.get('answer')))} | "
                  f"{d['probability']:.2f} | {d.get('decided_by', '')} |" for d in decisions]
    if latest.get("pinned"):
        lines.append("")
        lines.append("Pinned by the user's answers: " + ", ".join(
            f"`{k}` = {'…' if redact else _short(v)}" for k, v in latest["pinned"].items()))
    if latest.get("sql") and not redact:
        lines += ["", "```sql", latest["sql"], "```"]
    if not redact:
        lines += ["", "Reproduce:", "", "```bash",
                  f"{command} -v debug ask {_shell(latest['question'])}", "```"]
    lines += ["", f"Search ids: {', '.join('`' + r['id'] + '`' for r in failures[-5:])}", ""]
    return lines


def _expected(e: Dict[str, Any]) -> str:
    parts = []
    if e.get("sources"):
        parts.append("tables " + ", ".join(e["sources"]))
    if e.get("answer_shape"):
        parts.append(f"answer {e['answer_shape']}")
    if e.get("entity"):
        parts.append(f"about {e['entity']}")
    if e.get("activity"):
        parts.append(f"activity {e['activity']}")
    if e.get("synonym"):
        syn = e["synonym"]
        parts.append(f"“{syn['word']}” = {syn['field']} {syn['value']!r}")
    return ", ".join(parts)


def _short(value: Any, n: int = 60) -> str:
    text = str(value)
    return text if len(text) <= n else text[:n] + "…"


def _cell(text: Any) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def _shell(text: str) -> str:
    return "'" + text.replace("'", "'\\''") + "'"


def _as_dt(value: Any) -> dt.datetime:
    if isinstance(value, dt.datetime):
        return value.replace(tzinfo=None)
    return dt.datetime.fromisoformat(str(value)[:19])

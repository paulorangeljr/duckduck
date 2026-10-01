# Examples

| Folder / file | What it shows | Needs |
|---|---|---|
| [`local/`](local/) | Virtualization only: a synthetic "API" + local CSV/JSONL files, joined with SQL | nothing — offline |
| [`semantic/`](semantic/) with `duckduck.local.json` | Natural-language questions over 5 synthetic security sources | nothing — offline |
| [`semantic/`](semantic/) with `duckduck.online.json` | The same, with an LLM (catalog drafting + value extraction) and Jev (decisions) | `ANTHROPIC_API_KEY`, `JEV_API_KEY` |
| [`pipeline/`](pipeline/) | Declarative pipelines: bronze → silver (inline SQL) and gold (a notebook), with the sip following hosts through both | nothing — offline |
| [`../duckduck.example.json`](../duckduck.example.json) | Reference for every real connector and every `semantic` option | your own credentials — copy it to `duckduck.json` and edit |

Every config here uses relative paths, resolved against the config file's
own folder, so all commands below work from any directory (paths shown
from the repo root).

## `local/` — virtualization, offline

| File | |
|---|---|
| `duckduck.local.json` | two services: `python` (→ `synthetic.py`) and `files` (→ `data/`), no table prefix |
| `synthetic.py` | `tables(rows, seed)` → a synthetic `assets` table with real push-down params (`os`, `hostname_ilike`, `risk_gte`) |
| `data/` | `owners.csv`, `alerts.jsonl` — each file is a table |
| `run.py` | `SHOW TABLES` + a 3-source join, with verbose output |

```bash
python examples/local/run.py
```

```python
from duckduck import DuckAPI

duck = DuckAPI(verbose=True)
duck.auto_register(config_path="examples/local/duckduck.local.json")
duck.sql("SHOW TABLES")
duck.sql("SELECT a.hostname, o.owner FROM assets a JOIN owners o ON a.ip = o.ip WHERE a.os = 'linux'").df()
```

## `pipeline/` — bronze → silver → gold, offline

| File | |
|---|---|
| `duckduck.pipeline.json` | the connectors: `local/`'s synthetic API and files, plus `lake` — a `files` connector over what the pipelines write (`lake_silver_assets`…) |
| `assets_silver.json` | bronze (the API as it came, a partition per `{{ run_date }}`) and silver (one row per host, merged) — SQL inline |
| `gold_risk.json` + `gold_risk.ipynb` | gold: silver joined to the owners file, risky hosts per department; the SQL is the notebook's `%%sql` cells |
| `run.py` | runs both, then prints `web-0001`'s way through the four steps of the two pipelines |

```bash
python examples/pipeline/run.py
python -m duckduck.pipeline plan examples/pipeline/gold_risk.json
```

Everything is written under `examples/pipeline/lake/` (git-ignored), the sip
in `lake/_sip/`.

## `semantic/` — natural-language search

| File | |
|---|---|
| `catalog.yaml` | the hand-written semantic catalog for the 5 sample sources |
| `sample_sources.py` | the 5 synthetic sources (proxy, DNS, firewall, auth, asset inventory); `tables(now=None)` for the `python` connector |
| `evaluation.json` | the 6 MVP questions with their expected answers |
| `demo.py` | answers + scores the 6 questions (fixed clock, so the expected answers always hold) |
| `duckduck.local.json` | offline: lexical decision engine + rule-based extraction |
| `duckduck.online.json` | everything declared under `ai_providers`: Jev (TypeSafe's API, with a `jev_openrouter` entry to switch to) and two Claude models (`claude_strong` by default and for catalog drafting, `claude_fast` for extraction); drafts into `generated_catalog.yaml` (git-ignored) |
| `prompts/` | the default system prompts, as files to customize (used by `duckduck.online.json`) |

### Offline

```bash
python examples/semantic/demo.py
python -m duckduck.semantic --config examples/semantic/duckduck.local.json ask "Which machines communicated with 203.0.113.9?"
```

```python
from duckduck.semantic import ask

result = ask("Which machines communicated with 203.0.113.9?",
             config_path="examples/semantic/duckduck.local.json")
print(result.report())
result.results
```

`generate-catalog` needs an LLM, so it isn't available with this config
(you get an error saying which block to add) — use the online one.

### Online (LLM + Jev)

```bash
pip install -e ".[semantic,llm]"
export ANTHROPIC_API_KEY=...        # or an "authentication" block in its "ai_providers" entry
export JEV_API_KEY=...              # same; for Jev on OpenRouter instead: OPENROUTER_API_KEY and
                                    # "decision_engine": {"ai_provider": "jev_openrouter"}
```

```bash
python -m duckduck.semantic --config examples/semantic/duckduck.online.json jev-check
python -m duckduck.semantic --config examples/semantic/duckduck.online.json generate-catalog
python -m duckduck.semantic --config examples/semantic/duckduck.online.json -v ask "Which users accessed github in the last 24hrs?"
```

```python
from duckduck.semantic import ask, generate_catalog, jev_check

CONFIG = "examples/semantic/duckduck.online.json"

print(jev_check(config_path=CONFIG).ranked())

draft = generate_catalog(config_path=CONFIG)     # → examples/semantic/generated_catalog.yaml
print(draft.summary())                           # compare it with the hand-written catalog.yaml

print(ask("Which users accessed github in the last 24hrs?", config_path=CONFIG, verbose="info").report())
```

Without `catalog_generation.tables`, the draft covers every plain table plus every table
discovered through connector catalogs (Glue, ADX, SQL databases); `include` / `exclude` /
`max_tables` narrow it down (see the main README). Here the sample sources are plain tables.

This config sets `output_path`, so drafts go to their own file instead of
`catalog.yaml` (the hand-written catalog `demo.py` and the tests rely on).
Review it, then point `catalog_path` at it once you trust it. Without
`output_path`, generation maintains `catalog_path` itself: it drafts only
what's new or expired and keeps your hand-written sources and `notes` (see
"Keeping the catalog up to date" in the main README). The keys
never live in the file: without an `authentication` block, the LLM key
comes from `ANTHROPIC_API_KEY` and the Jev key from `JEV_API_KEY`. In a
real setup, use `authentication` blocks pointing at AWS Secrets Manager
or Azure Key Vault, as in `duckduck.example.json`.

## `duckduck.example.json` — reference for real connectors

Every connector (SharePoint, InsightVM, SQL databases, ServiceNow Basic +
OAuth2, Axonius, S3/Glue, Azure Blob, ADX with app registration and
managed identity) and a full `semantic` section. Copy it to
`duckduck.json` at your project root (git-ignored — it may end up holding
secrets), delete what you don't use, fill in the rest.

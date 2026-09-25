# duckduck

Query HTTP APIs with SQL. `duckduck` registers Python functions as SQL
"tables" backed by [DuckDB](https://duckdb.org/): it parses your query,
pushes `WHERE`/`LIMIT` down to the API as function arguments, fetches only
what's needed, and lets DuckDB handle joins, aggregations and anything
else on the result.

Ships with two ready-to-use wrappers — **SharePoint** (Microsoft Graph
API) and **InsightVM** (Rapid7) — plus the core engine for wrapping any
other HTTP API the same way.

## Install

```bash
pip install -e .

# Optional extras
pip install -e ".[cert]"    # SharePoint auth via PFX/P12 or PEM certificate
pip install -e ".[aws]"     # AWS Secrets Manager support (auto_register)
pip install -e ".[azure]"   # Azure Key Vault support (auto_register)
pip install -e ".[adx]"     # Azure Data Explorer (Kusto) connector
pip install -e ".[database]"  # SQL databases (+ your engine's driver, e.g. pyodbc)
pip install -e ".[semantic]"  # natural-language search (duckduck.semantic)
pip install -e ".[llm]"     # Claude (Claude API or Microsoft Foundry), for catalog drafting / LLM extraction
pip install -e ".[azure-openai]"  # an Azure OpenAI deployment instead
pip install -e ".[openrouter]"    # any model on OpenRouter (Jev on OpenRouter needs nothing extra)
pip install -e ".[dev]"     # pytest, for running the test suite
```

Requires Python 3.10+.

**Examples**: [`examples/README.md`](examples/README.md) lists every
example — which ones run fully offline, which need keys — with the
commands to run each from the terminal and from Python.

## Quickstart

The core idea: register any function that returns `list[dict]` /
`pd.DataFrame`, then query it as a table.

```python
from duckduck import DuckAPI

def assets(hostname=None, ip=None, limit=None):
    # call your API here, using hostname/ip/limit if provided
    ...

duck = DuckAPI()
duck.register_api_function("assets", assets)

df = duck.sql("SELECT * FROM assets WHERE hostname = 'web-prod' LIMIT 50").df()
```

`hostname` and `limit` above are pushed down automatically: DuckAPI
inspects `assets`'s signature, matches it against the `WHERE`/`LIMIT` in
your SQL, and calls `assets(hostname='web-prod', limit=50)` — one HTTP
call, not "fetch everything and filter in Python." Any predicate the
function doesn't accept is still applied by DuckDB on the result, so
queries are always correct, just not always server-side filtered.

### Structural parameters vs. column filters

Some parameters build the URL itself (e.g. `asset_id` in
`/assets/{id}/vulnerabilities`) rather than filtering a column. Pass those
inline with `func(param=val)`; put everything else in `WHERE`:

```python
duck.sql(
    "SELECT * FROM asset_vulnerabilities(asset_id=42) WHERE severity = 'critical'"
)
```

## Using the SharePoint wrapper

```python
from duckduck import DuckAPI, SharePoint

sp = SharePoint(tenant_id, client_id, client_secret)
duck = DuckAPI()

duck.register_api_function("sites", sp.sites)
duck.register_api_function("lists", sp.lists)
duck.register_api_function("list_items", sp.list_items)

duck.sql("SELECT id, displayName, webUrl FROM sites").df()

duck.sql(
    "SELECT * FROM list_items"
    " WHERE site_name = 'Intranet' AND list_name = 'Tasks'"
    " AND Status = 'Active'"
).df()
```

`site_name`/`list_name` in the `WHERE` clause are resolved to IDs
automatically (looked up by display name), then stripped from the query
before DuckDB runs it — they aren't real result columns. List item fields
come back under their SharePoint UI display name by default (pass
`column_names='internal'` to get the Graph API's raw field names instead).

If your tenant is single-site, set a default so you never need
`site_id`/`site_name` at all:

```python
sp = SharePoint(
    tenant_id, client_id, client_secret,
    hostname="company.sharepoint.com",
    site_path="/teams/myteam",
)
duck.sql("SELECT * FROM list_items WHERE list_name = 'Tasks'").df()
```

**Authentication modes** — pick whichever matches your Azure AD app registration:

```python
SharePoint(tenant_id, client_id, client_secret)
SharePoint.from_thumbprint(tenant_id, client_id, thumbprint, private_key_pem)
SharePoint.from_pfx(tenant_id, client_id, "/path/cert.pfx", pfx_password="...")
SharePoint.from_pem_cert(tenant_id, client_id, private_key_pem, cert_pem)
```

## Using the InsightVM wrapper

```python
from duckduck import DuckAPI, InsightVM

r7 = InsightVM(host="console.local", username="admin", password="...")
duck = DuckAPI()

duck.register_api_function("assets", r7.assets)
duck.register_api_function("vulnerabilities", r7.vulnerabilities)

duck.sql("SELECT * FROM assets WHERE hostname = 'web-prod' LIMIT 25").df()
```

## Seeing what happens: verbose mode

```python
duck = DuckAPI(verbose=True)      # or "debug", or DUCKDUCK_VERBOSE=info
duck.sql("SELECT hostName FROM assets WHERE riskScore >= 200 LIMIT 3").df()
```

```
▶ assets()
    ✗ riskscore >= 200 — no riskscore_gte parameter, DuckDB filters
    ✗ LIMIT 3 — not every WHERE condition reached the source
GET https://console.local/api/3/assets?size=500&page=0 → 200 (0.15s, 779 B)
/assets: page 1/3 · 500/1,250 rows · 0.2s elapsed · ~0.3s left
...
  assets: 1,250 rows × 4 columns in 0.46s
```

Every table call shows what was sent to the source and what DuckDB had to
filter itself (and why), every HTTP request, pagination progress with time
left (when the API reports a total), and the SQL/KQL sent to databases and
ADX. `"debug"` adds request bodies. Secrets are always masked.

## Streaming large results

`sql()` waits for all pages before returning. For large datasets, use
`stream()` to get one `pd.DataFrame` per page as it arrives — register the
matching `iter_*` generator alongside the regular function:

```python
duck.register_api_function("assets", r7.assets)
duck.register_streaming_function("assets", r7.iter_assets)

for chunk in duck.stream("SELECT * FROM assets WHERE severity = 'critical'"):
    display(chunk)  # e.g. in Jupyter — shows up as each page arrives
```

Note `WHERE`/`LIMIT` in `stream()` apply per page, not globally — see
`CLAUDE.md` for the full list of limitations.

## Bootstrapping everything at once: `auto_register`

Instead of instantiating each wrapper and calling `register_api_function`
per method, `auto_register()` builds every configured wrapper and
registers all of its tables in one call. Each service has a `connector`
(which wrapper it is) and an `authentication` block (where its credentials
come from) — `authentication.type` picks one of three sources:

**1. `"local"` — hardcoded, fully offline, no secret store touched:**

```python
from duckduck import DuckAPI

duck = DuckAPI()
duck.auto_register({
    "sharepoint": {
        "authentication": {
            "type": "local",
            "tenant_id": "...", "client_id": "...", "client_secret": "...",
        },
    },
})
```

**2. `"aws"` — AWS Secrets Manager:**

```python
duck.auto_register({
    "sharepoint": {
        "hostname": "company.sharepoint.com",
        "site_path": "/teams/myteam",
        "authentication": {
            "type": "aws",
            "region_name": "us-east-1",
            "secret_id": "prod/sharepoint/duckduck",
        },
    },
})
```

**3. `"azure"` — Azure Key Vault:**

```python
duck.auto_register({
    "sharepoint": {
        "authentication": {
            "type": "azure",
            "vault_url": "https://my-vault.vault.azure.net/",
            "secret_id": "prod-sharepoint",
        },
    },
})
```

For `"aws"`/`"azure"`, the fetched secret is a JSON object matching the
wrapper's constructor — for SharePoint that's `tenant_id` + `client_id` +
one of `client_secret` / `thumbprint`+`private_key_pem` / `pfx_path` /
`private_key_pem`+`cert_pem`; for InsightVM it's `host` + `username` +
`password`. A PEM value with an escaped `\n` (common when a key passes
through a secrets manager) is fixed automatically. Neither backend needs
its SDK installed unless you actually use it (`pip install
"duckduck[aws]"` / `"duckduck[azure]"`), and Azure auth to the vault
itself uses `DefaultAzureCredential` (env vars, managed identity, `az
login`) rather than another hardcoded secret in the config.

Any field in the `authentication` block on top of `type`/`secret_id` is
layered onto the fetched secret: a plain value overrides that key, and a
value written as `"$secret.<key>"` pulls from a *different* key in the
same secret — handy when the vault's field names don't match what the
connector expects:

```python
"authentication": {
    "type": "aws",
    "secret_id": "prod/insightvm",
    "password": "$secret.svc_password",  # secret has "svc_password", not "password"
},
```

Registered tables are always prefixed by the key you gave the service in
the config dict, so `sharepoint`/`insightvm` above become
`sharepoint_list_items`, `insightvm_assets`, etc. This is what avoids
collisions when two wrappers expose a same-named table (both SharePoint
and InsightVM have a `sites` table) and lets you register more than one
instance of the same wrapper — use `connector` when the name you pick
doesn't match a wrapper name directly:

```python
instances = duck.auto_register({
    "sharepoint": {
        "connector": "sharepoint",
        "hostname": "company.sharepoint.com",
        "site_path": "/teams/myteam",
        "authentication": {
            "type": "aws", "region_name": "us-east-1",
            "secret_id": "prod/sharepoint/duckduck",
        },
    },
    "insightvm_prod": {
        "connector": "insightvm",
        "authentication": {
            "type": "aws", "region_name": "us-east-1", "secret_id": "prod/insightvm",
        },
    },
    "insightvm_dev": {
        "connector": "insightvm",
        "host": "dev.local",
        "authentication": {"type": "local", "username": "a", "password": "b"},
    },
})

duck.sql("SELECT * FROM sharepoint_list_items WHERE list_name = 'Tasks'")
duck.sql("SELECT * FROM insightvm_prod_assets WHERE hostname = 'web-prod'")

# The instance dict also gives you direct access to wrapper methods
# that don't map to a table:
instances["sharepoint"].site_by_path("company.sharepoint.com", "/sites/marketing")
```

Both `insightvm_prod` and `sharepoint` share the same AWS region here — a
single `SecretsManager`/boto3 client is built and reused across them
automatically. If you'd rather build the backend yourself (tests, or
reusing one client across several `auto_register()` calls), pass it via
`secrets={"aws": my_secrets_manager}` / `secrets={"azure":
my_keyvault_client}`, and it's used as-is instead.

### Loading the config from a JSON file

To avoid declaring the `services` dict in code at all, drop it in a JSON
file and call `auto_register()` with no arguments. Copy
[`duckduck.example.json`](duckduck.example.json) to `duckduck.json` and
fill in your own values — `duckduck.json` is gitignored on purpose, since
a `"local"` block can hold literal secrets:

```bash
cp duckduck.example.json duckduck.json
# edit duckduck.json with your real secret_id / credentials
```

```python
duck = DuckAPI()
duck.auto_register()
```

`duckduck.json` in the current directory is used by default; pass
`config_path="/path/to/file.json"` or set the `DUCKDUCK_CONFIG` environment
variable to point elsewhere. Passing a `services=` dict explicitly (as in
every example above) always skips the file lookup.

## Running locally with no connectors

Two connectors load local data through the same `auto_register()` config
— no credentials, no network:

```json
{
  "services": {
    "synthetic": {"connector": "python", "module": "synthetic.py", "kwargs": {"rows": 1000}, "table_prefix": ""},
    "files":     {"connector": "files",  "path": "data",                                       "table_prefix": ""}
  }
}
```

- `files`: every CSV / TSV / Parquet / JSON / JSONL in the folder is a
  table (a sub-folder of partitioned Parquet is one table); WHERE runs
  inside the DuckDB scan.
- `python`: your module's `tables(**kwargs)` returns `{name: function}`;
  each function is a table with normal push-down — a stand-in for an API.

```python
duck = DuckAPI()
duck.auto_register(config_path="examples/local/duckduck.local.json")
duck.sql("SELECT a.hostname, o.owner FROM assets a JOIN owners o ON a.ip = o.ip WHERE a.os = 'linux'").df()
```

`table_prefix: ""` keeps bare table names (the default prefixes them with
the service name). Semantic search runs fully offline the same way:

```bash
python -m duckduck.semantic --config examples/semantic/duckduck.local.json ask "Which machines communicated with 203.0.113.9?"
```

```python
from duckduck.semantic import ask

print(ask("Which machines communicated with 203.0.113.9?",
          config_path="examples/semantic/duckduck.local.json").report())
```

## Discovering what's registered

```python
duck.sql("SHOW TABLES")          # or duck.list_tables() for a pd.DataFrame
```

Everything registered is queryable with `SELECT ... FROM`, but not
everything is a plain table — the `kind` column says which is which, and
`usage` shows how to query it:

| `kind` | What it is | `usage` example |
|---|---|---|
| `table` | data, selectable as-is | `SELECT * FROM alerts LIMIT 10` |
| `table function` | data behind required arguments that say *which* data | `SELECT * FROM glue_table(database='<database>', table_name='<table_name>') LIMIT 10` |
| `catalog` | lists what a source contains — how you find those arguments | `SELECT * FROM glue_tables` |
| `raw query` | runs a query you write in the source's own language | `SELECT * FROM db_query(sql='<sql>')` |

The other columns: `pushdown` (which WHERE conditions / LIMIT the source
applies itself — anything else still works, DuckDB filters after
fetching), `source` (the connector), `endpoint` (URL, connection string
with the password redacted, or file path) and `description`.

```python
duck.list_tables(kind="table")                        # only plain tables
duck.list_tables(kind=["table", "table function"])    # only data
duck.list_tables(details=True)                        # + streaming, raw Python signature
```

## Natural-language search (`duckduck.semantic`)

On top of the tables above, `duckduck.semantic` answers questions in
plain language. It uses a hand-maintained **semantic catalog** (YAML:
what each source's fields mean, entities, activities, and how sources
join). Questions go through independent, probability-scored decisions,
then a validated logical plan, and only then SQL. Low-confidence
decisions come back as a clarification request and never execute.

```bash
pip install -e ".[semantic]"
python examples/semantic/demo.py   # the 6 MVP questions against sample data
```

```python
from duckduck import DuckAPI
from duckduck.semantic import SemanticSearch

duck = DuckAPI()
duck.auto_register()                               # the virtualization layer
search = SemanticSearch("catalog.yaml", duck)      # table: names = registered tables

result = search.search("Which users accessed github in the last 24hrs?")
result.status      # "ok" | "needs_clarification" | "invalid_plan"
result.sql         # the SQL that ran
result.results     # pd.DataFrame
result.decisions   # every judgment, with its probability
```

Everything can be configured in `duckduck.json` (see
`duckduck.example.json`), with API keys pulled from the same
local/AWS/Azure `authentication` blocks the connectors use. Every AI model
is declared once, by a name you choose, in the top-level `ai_providers`
section (see [AI providers](#ai-providers-llms-and-the-decision-engine)).
The `semantic` section only uses those names:

- **The decision engine** makes the judgments: which entity, which
  activity, which sources are relevant. Set it with
  `decision_engine.ai_provider`. That can be Jev on TypeSafe's API, Jev on
  OpenRouter, or any chat model. Without it, an offline lexical baseline
  decides.
- **An LLM** (`default_llm`, or one per stage) drafts the semantic catalog
  from your registered tables and extracts values from questions
  (`extractor.type: "llm"`). You can replace every system prompt. A
  Claude key comes from `ANTHROPIC_API_KEY` or from an `authentication`
  block in the entry that yields an `api_key`
  (`{"type": "aws", "secret_id": "prod/anthropic", "api_key": "$secret.key"}`).
  If it finds neither, it fails at startup and tells you where to put the key.
  In a notebook, the kernel only sees environment variables that existed
  when it started, so either restart the kernel or set
  `os.environ["ANTHROPIC_API_KEY"]` before calling anything.
  Without an explicit table list, it drafts every plain table **and every
  table behind your connectors**, found through their catalogs (each Glue
  table via `glue_table(database=…, table_name=…)`, each ADX table, each
  table/view of a SQL database). Narrow it down in `catalog_generation`:

  ```json
  "catalog_generation": {"include": ["security.*", "ProxyLogs"], "exclude": ["*_tmp"], "max_tables": 50}
  ```

### Keeping the catalog up to date

`generate-catalog` maintains the catalog `ask` reads (`catalog_path`) in
place. It doesn't rewrite the file from scratch:

- Each source it drafts is stamped with `generated_at` (and
  `generated_by`, the model that drafted it).
- **Each run drafts only what needs it.** That means tables not in the
  catalog yet, generated sources older than `max_age`, and whatever you
  force. Everything else is kept as it is. With nothing to draft, it
  makes no LLM call and leaves the file untouched.
- **Sources without `generated_at` are yours.** Sources you wrote by hand,
  or drafted ones whose `generated_at` you deleted to keep your edits, are
  only redrafted when forced by name.
- **`notes` are yours too.** Every source and every field in the
  generated YAML has a `notes: ''` slot right below its `description`.
  Write there. The LLM never writes them. They're kept when the source is
  redrafted and given to the LLM as context then. YAML `#` comments are not
  kept, because the file is rewritten.
- Your own `critical` flags, entity and activity definitions, and
  relationships are kept as well. New ones are added.
- `max_tables` caps how many sources one run drafts; the rest wait for the
  next run.

```json
"catalog_generation": {"max_age": "7d", "auto_refresh": true}
```

`max_age` takes `"7d"`, `"12h"`, `"30m"`, `"2w"` or a number of seconds.
Omitted, drafted sources never expire, so only new tables get drafted.
`auto_refresh` makes `ask` / `SemanticSearch.from_config` draft whatever
is missing or expired before answering. If that fails, it warns and
answers with the catalog as it is.

```yaml
sources:
  proxy_logs:
    table: glue_table
    args: {database: security, table_name: proxy_logs}
    description: One row per HTTP request through the Zscaler proxy.
    notes: Only covers the corporate network; VPN traffic is in vpn_logs.   # yours, kept
    generated_at: 2026-09-25T12:00:00+00:00                              # delete to lock your edits
    generated_by: claude_strong (anthropic claude-opus-5)
    fields:
      url:
        semantic_type: url
        notes: Query strings are stripped before logging.                   # yours, kept
```

Force a redraft from the terminal or from Python:

```bash
python -m duckduck.semantic generate-catalog --force                     # every generated source
python -m duckduck.semantic generate-catalog --force proxy_logs "adx_*"  # these, even hand-written
```

**Watching it work.** Generation uses the same verbose mode as the
virtualization layer. Turn it on with `generate_catalog(verbose=True)`,
`python -m duckduck.semantic -v generate-catalog`, `DuckAPI(verbose=True)`
or `DUCKDUCK_VERBOSE=info`. You get the plan (how many tables are new,
expired or kept), then `[n/total]` per table with its profile and each LLM
call's time and tokens, time elapsed and left, then the linking pass and
what the merge dropped:

```
catalog: 12 tables — drafting 3 (2 new, 1 expired), keeping 9
[1/3] security_proxy_logs (new) — profiling glue_table(database='security', table_name='proxy_logs')
  18 columns, 5 sample rows, with your notes — asking catalog (openrouter anthropic/claude-sonnet-5)
  llm anthropic/claude-sonnet-5 → GenSource: 6.2s · 3,410 in / 820 out tokens
  → 14 fields, entities user, host, activities web_access · 6.4s elapsed · ~12.8s left
...
linking 12 sources (3 drafted, 9 kept) — asking catalog (...) for entities, activities and joins
merged: 12 sources, 6 entities, 4 activities, 9 relationships · 24.1s total
```

**Using the catalog in your process.** Whatever reads `catalog_path`
picks the catalog up, with nothing extra to load:

```python
from duckduck.semantic import SemanticSearch, ask, connect

ask("Which users accessed github in the last 24hrs?")      # reads duckduck.json → catalog_path

duck = connect()                                          # or keep the pieces around
search = SemanticSearch.from_config(duck)                 # catalog_path, engine, LLMs from the config
search.search("Which hosts queried example.com?")

search = SemanticSearch("semantic_catalog.yaml", duck)    # or any catalog file, directly
```

The catalog is read once, when the `SemanticSearch` is built. After
regenerating, build a new one (`ask()` builds one per call). With
`auto_refresh`, building it also brings the catalog up to date first.

To review drafts before they go live, set `catalog_generation.output_path`
to another file. Generation then maintains that file, and you copy what
you approve into `catalog_path`.

Every command works from the terminal **and** from Python — the CLI is a
thin wrapper over the same functions, so both run identical code:

```bash
python -m duckduck.semantic generate-catalog   # draft what's new or expired into the catalog
python -m duckduck.semantic ask "Which users accessed github in the last 24hrs?"
python -m duckduck.semantic jev-check          # one real decision call: key, network, parsing
```

```python
from duckduck.semantic import ask, generate_catalog, jev_check

result = generate_catalog()                    # writes semantic_catalog.yaml
print(result.summary())                        # what the CLI prints (+ result.warnings, result.catalog)

result = ask("Which users accessed github in the last 24hrs?")
print(result.report())                         # what the CLI prints
result.results                                 # the answer as a pd.DataFrame
result.sql, result.decisions                   # the SQL that ran, every judgment

print(jev_check().ranked())                    # raises if the key/network/parsing fails
```

| CLI | Python |
|---|---|
| `ask "..."` | `ask("...")` → `SearchResult` (`.report()`, `.results`, `.sql`, `.decisions`) |
| `ask "..." --plan-only` | `ask("...", execute=False)` |
| `ask "..." --json` | `ask("...").to_dict()` |
| `generate-catalog` | `generate_catalog()` → `GenerationResult` (`.summary()`, `.catalog`, `.warnings`, `.path`) |
| `generate-catalog --out x.yaml` | `generate_catalog(out="x.yaml")` (`write=False` to keep it in memory) |
| `generate-catalog --force` | `generate_catalog(force=True)` |
| `generate-catalog --force proxy_logs "adx_*"` | `generate_catalog(force=["proxy_logs", "adx_*"])` |
| `jev-check` | `jev_check()` → `Classification` |
| `--config path` | `config_path="path"` |
| `-v` / `-v debug` | `verbose="info"` / `verbose="debug"` |

Each call builds a `DuckAPI` from the config's `services`. In a notebook,
build it once and pass it along instead:

```python
from duckduck.semantic import SemanticSearch, ask, connect

duck = connect()                               # DuckAPI + auto_register() from duckduck.json
ask("Which users accessed github in the last 24hrs?", duck=duck)

search = SemanticSearch.from_config(duck)      # or keep the whole pipeline around
search.search("Which hosts queried example.com?")
```

### AI providers: LLMs and the decision engine

Every AI model is declared once, by a name you choose, in the top-level
`ai_providers` section of `duckduck.json`, next to `services`. Each entry
is complete: provider, model, credentials. `semantic` only refers to
entries by name. Where a name is used decides the entry's role, so one
entry can serve several stages:

```json
{
  "services": { ... },

  "ai_providers": {
    "jev":           {"provider": "openrouter", "api": "decisions", "model": "typesafe/jev-1.13"},
    "claude_strong": {"provider": "anthropic", "model": "claude-opus-5"},
    "claude_fast":   {"provider": "anthropic", "model": "claude-haiku-4-5"},
    "azure_gpt":     {"provider": "azure_openai", "endpoint": "https://my-resource.openai.azure.com",
                      "deployment": "gpt-mini",
                      "authentication": {"type": "azure", "vault_url": "https://kv.vault.azure.net/",
                                         "secret_id": "azure-openai", "api_key": "$secret.key"}}
  },

  "semantic": {
    "decision_engine":    {"ai_provider": "jev"},
    "default_llm":        "claude_strong",
    "extractor":          {"type": "llm", "llm": "azure_gpt"},
    "catalog_generation": {"llm": "claude_fast", "link_llm": "claude_strong"}
  }
}
```

| In `semantic` | Role | Calls | If omitted |
|---|---|---|---|
| `decision_engine.ai_provider` | the judgments (entity, activity, source/field/join relevance) | a few per question | offline lexical baseline |
| `default_llm` | the LLM every stage below uses unless it names its own | | no LLM |
| `extractor.llm` | pulls values out of each question (`extractor.type: "llm"`) | 1 per question | `default_llm` |
| `catalog_generation.llm` | drafts each table | 1 per table | `default_llm` |
| `catalog_generation.link_llm` | entities, activities and joins across all tables | 1 per run | `catalog_generation.llm`, then `default_llm` |

**`provider` and `api`.** An entry's `api` is either `chat` (text
generation) or `decisions` (a typed Decisions API: `noul` / `choice`
questions, probabilities back).

| `provider` | `api` | Settings | Key (if none is found, the Azure providers use Entra ID via `DefaultAzureCredential`) |
|---|---|---|---|
| `anthropic` (default) | `chat` | `model`, `base_url` (a gateway), `headers` | `ANTHROPIC_API_KEY` |
| `foundry`: Claude on Microsoft Foundry | `chat` | `model` (the deployment name), `resource` **or** `endpoint`, `tenant_id` | `ANTHROPIC_FOUNDRY_API_KEY`; endpoint from `ANTHROPIC_FOUNDRY_RESOURCE` / `ANTHROPIC_FOUNDRY_BASE_URL` |
| `azure_openai` | `chat` | `deployment`, `endpoint`, `api_version`, `tenant_id`, `headers` | `AZURE_OPENAI_API_KEY`; `AZURE_OPENAI_ENDPOINT`, `OPENAI_API_VERSION` |
| `openrouter` | `chat` (default) or `decisions` | `model` (required: the `vendor/model` id), `headers`, `require_parameters` | `OPENROUTER_API_KEY` |
| `jev`: TypeSafe's own API | `decisions` | `model`, `base_url`, `timeout` | `JEV_API_KEY` |

- **Any `decisions` entry** can point at another host serving the same
  API with `decisions_url`.
- **A `decisions` entry can only be a decision engine.** A `chat` entry can
  be an LLM or a decision engine. As a decision engine it's asked for
  probabilities through structured output; its system prompt is
  `decision_engine.system_prompt` (or `_file`).
- **Every connection setting** can be written in the entry, stored in its
  `authentication` secret (same `local`/`aws`/`azure` blocks as the
  connectors), or left to the provider's standard environment variables.
  Header values written as `"$secret.<key>"` are also read from the secret.

**Jev, from TypeSafe or from OpenRouter.** Both serve the same Decisions
API, so moving between them only changes the entry:

```json
"ai_providers": {
  "jev_typesafe":   {"provider": "jev", "model": "typesafe-ai/jev"},
  "jev_openrouter": {"provider": "openrouter", "api": "decisions", "model": "typesafe/jev-1.13"}
},
"semantic": {"decision_engine": {"ai_provider": "jev_openrouter"}}
```

On OpenRouter it goes to `https://openrouter.ai/api/alpha/decisions` with
your `OPENROUTER_API_KEY`. Pin `typesafe/jev-1.13` so the thresholds you
tuned stay valid, or use `~typesafe/jev-latest` to follow new releases.
`jev-check` makes one real call through whichever entry is configured.

**On Azure:**

```json
"ai_providers": {
  "azure_gpt": {
    "provider": "azure_openai",
    "endpoint": "https://my-resource.openai.azure.com",
    "deployment": "gpt-prod",
    "api_version": "2024-10-21",
    "headers": {"Ocp-Apim-Subscription-Key": "$secret.apim_key"},
    "authentication": {"type": "azure", "vault_url": "https://kv.vault.azure.net/", "secret_id": "azure-openai",
                       "api_key": "$secret.key"}
  },
  "claude_foundry": {"provider": "foundry", "resource": "my-foundry", "model": "claude-opus-5"}
}
```

`claude_foundry` sets no key, so it uses Entra ID: `az login`, a
managed identity, or the `AZURE_*` variables. Entra ID needs
`pip install -e ".[azure]"`.

When the config loads, it's rejected if an entry is written inside
`semantic`, if a name isn't declared, or if a `decisions` entry is used
as an LLM. Each error says what to move where, or lists the declared
names. The old formats (`llms`, `semantic.llm`, an inline Jev
`decision_engine`) fail with the exact block to write instead. With `-v`,
each stage logs the entry it got (`llm for extractor: azure_gpt
(azure_openai gpt-mini)`).

The same thing from Python, building the pieces yourself:

```python
from duckduck.semantic import (AzureOpenAILLM, Catalog, CatalogGenerator, ClaudeLLM, JEVAdapter, JevClient,
                               LLMExtractor, OpenRouterLLM, SemanticSearch)

llm = AzureOpenAILLM("gpt-prod", endpoint="https://my-resource.openai.azure.com", api_key=...)
llm = ClaudeLLM.on_foundry(model="claude-opus-5", resource="my-foundry")      # Entra ID
llm = OpenRouterLLM("vendor/model")                                            # OPENROUTER_API_KEY
llm = ClaudeLLM(base_url="https://llm-gateway.corp", default_headers={"Authorization": "Bearer ..."})

jev = JEVAdapter(JevClient(url=JevClient.OPENROUTER_DECISIONS_URL, model="typesafe/jev-1.13",
                           api_key_env="OPENROUTER_API_KEY"))
draft = CatalogGenerator(llm, duck).generate()                     # catalog drafting
catalog = Catalog.load("semantic_catalog.yaml")
search = SemanticSearch(catalog, duck, engine=jev, extractor=LLMExtractor(catalog, llm))
```

Anything else (another cloud, a local model) plugs in by implementing
`generate(system, prompt, output_model)`: it must return `output_model`
parsed from the answer, so the provider needs structured output (JSON
schema).

See `examples/semantic/catalog.yaml` for the catalog format and the
"Semantic search" section of `CLAUDE.md` for the design.

## Adding your own API wrapper

Any Python object works as long as its methods follow the convention:
required positional args for structural (URL-building) parameters,
`Optional[X] = None` for column filters, and `limit: Optional[int] = None`
last. See the "Adding a new API wrapper" and "Auto-registration" sections
of `CLAUDE.md` for the full contract, including how to plug a new wrapper
into `auto_register()`.

## Development

```bash
pip install -e ".[dev]"
python -m pytest tests/
```

`CLAUDE.md` documents the internal architecture (push-down flow, SQL
rewriting, streaming contract) in more depth — read it before making
changes to `duckduck/core.py` or adding a new wrapper.

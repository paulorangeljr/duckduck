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

## Public APIs: NVD (CVEs) and REST Countries

Two connectors for APIs from [public-apis](https://github.com/public-apis/public-apis):

```python
from duckduck import DuckAPI

duck = DuckAPI()
duck.auto_register({
    "nvd": {},                                   # no key needed (one raises the rate limit)
    "world": {"connector": "restcountries",      # v5 needs a free key: restcountries.com/sign-up
              "authentication": {"type": "local", "api_key": "rc_live_..."}},
})

# CVEs: severity, KEV, CWE and date ranges go to NVD as its own filters
duck.sql("""SELECT id, cvss_score, kev_added, description FROM nvd_cves
            WHERE severity = 'CRITICAL' AND in_kev = true AND published >= '2026-06-01'""").df()
duck.sql("SELECT id, severity FROM nvd_cves(keyword='log4j') ORDER BY cvss_score DESC").df()

# Countries: a code, name, capital, subregion or region picks the request
duck.sql("SELECT name, capital, population FROM world_countries WHERE region = 'Americas'").df()
```

- **`nvd_cves`** (NVD CVE API 2.0): `id`, `published`, `last_modified`,
  `status`, `description`, `severity` / `cvss_score` / `cvss_vector` (CVSS
  v3.x), `cvss4_severity` / `cvss4_score`, `cwe`, `in_kev` + `kev_added` /
  `kev_due` / `kev_action` / `kev_name` (CISA's Known Exploited
  Vulnerabilities), `source`, `references`. `id`, `severity`,
  `cvss4_severity`, `cwe`, `in_kev = true` and ranges on `published` /
  `last_modified` are asked of NVD (ranges cut into its 120-day windows);
  `keyword=` inline is NVD's word search. Without a key NVD allows 5
  requests per 30 s, so the connector waits 6 s between pages (and retries
  when rate limited); an `api_key` in the `authentication` block (optional)
  raises that to 50 and the pause to 0.6 s. Rejected CVE IDs are left out
  unless `include_rejected: true`.
- **`world_countries`** (REST Countries **v5**, `api.restcountries.com` —
  the old keyless v3.1 API was retired and now only redirects to a notice,
  which the connector reports as an error instead of reading it as a
  country): `name`, `official_name`, `cca2`/`cca3`/`ccn3`, `region`,
  `subregion`, `capital`, `population`, `area`, `languages`, `currencies`,
  `continents`, `timezones`, `borders`, `independent`, `un_member`,
  `landlocked`, `latitude`/`longitude`, `tld`, `flag`, `calling_codes`,
  `memberships` (`un, nato, g7`…), `leaders`. A code (`/code/{c}`), a name,
  a capital, a subregion or a region picks the request; anything else reads
  every country (~250, paged). Fields a record doesn't carry stay empty.
  Needs `api_key` (free tier; their README's demo key is `rc_live_demo`).

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

`sql()` itself (and the web page's SQL tab) also reads a table with a
streaming function page by page when its `LIMIT` can't be sent to the API:
each page is filtered by DuckDB as it arrives and only the matching rows
and the referenced columns are kept (every column for `SELECT *`), so a
filter the API doesn't take never holds the whole API result in memory. A
single-table query with a `LIMIT` and nothing that needs every row (ORDER
BY, aggregates, DISTINCT…) stops asking for pages once it has enough.
`DuckAPI(stream_pages=False)` turns it off.

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

How a question is read — rules find, the decision engine (Jev) decides:

- **Rules extract** what can be read exactly: values (IPs, e-mails,
  domains, quoted text), known values from the catalog (`critical`,
  `failed`), time windows (`in the last year`, `entre ontem e hoje`), and
  what the wording suggests (a count? a list? a question about the
  catalog?).
- **The engine decides**, in one batch per question: what the question
  is about — **the data, this assistant, or neither** (so "…in the CISA
  KEV catalog" is about CVEs, not about this assistant's catalog) —, what
  it asks for, which tables, fields and joins. The rules' findings are
  facts it reads, not decisions.
- **Before asking you, it judges whole plans.** When a decision is in
  doubt, every reading the question allows is planned **in parallel**
  (no data read), described in plain words, and judged side by side in
  one batch: *which of these answers the question?* and, for each, *does
  it answer exactly what was asked?*. A clear winner runs (the answer
  shows *Readings considered*); otherwise you get the question, as
  before. `semantic.hypotheses` tunes it (`max_hypotheses`, `depth`,
  `threshold`, `margin`, `check`, `enabled`).
- **You see what it's doing, and can stop it.** While a question runs,
  the page lists each step as it happens — reading the question, finding
  the tables, asking the decision engine (and what about), laying out the
  plan, writing the SQL, reading each table (page by page, with rows read
  and kept). **⏸ Pause** stops at the next step and shows what it has so
  far: the SQL, the tables read, the decisions — *Open in the SQL tab* to
  take it from there — then **▶ Continue**. **✕ Cancel** gives up at once
  and shows the same. A call already in flight (to Jev, an LLM or an API)
  finishes first; pages of a big table are the natural stopping points. In
  Python: `with duckduck.progress.tracking(p := Progress()): search.search(q)`
  from a thread, and `p.pause()` / `p.resume()` / `p.cancel()` (raises
  `duckduck.progress.Cancelled` in the search) from another.
- **A quick sample first, every row on request.** Next to *Ask*, pick how
  many rows an answer shows first (10 · 50 · 100 · 500 · 1,000 · all;
  remembered per browser). The question stops reading as soon as it has
  them (one more, to know whether there are more): a big API gets one
  request of that size when it can apply every filter itself, or is read
  page by page only until then — even for "which/different …" answers,
  whose de-duplication and order then apply to the sample (the full, sorted
  answer is *Get all rows*). When there are more, **Get all rows** fetches them
  as a background job, with the same steps, pause and cancel — instantly,
  without reading again, when what was read was already everything the
  filters let through. In Python: `search.search(q, sample=100)` (or
  `SemanticSearch(sample=…)`, `semantic.sample`), `result.has_more`,
  `search.fetch_all(result)`.
- **What was read is kept.** The rows each table contributed (every catalog
  column, after its filters) stay in DuckDB for the latest answers
  (`keep_answers`, 20): *＋ Columns* and *Get all rows* answer from them
  (the panel says "From the rows already read — nothing fetched again");
  when they aren't there anymore, the page shows the steps of reading
  again.
- **More columns after the answer.** An answer that is rows has a
  **＋ Columns** button: the other fields of the tables it read, the ones
  the question filtered on (and the time field) first — "add all" shows
  them in one tap. Each pick runs **the same plan** again with that column
  next to what was asked: nothing is decided again (no Jev or LLM call),
  the filters stay, and *Take over from here* takes what's on screen. In
  Python: `search.with_columns(result, ["nvd_cves.severity",
  "nvd_cves.published"])` (or `conversation.with_columns(...)`;
  `search.column_options(result)` lists what can be added; `[]` goes back).
- **Big sources are read page by page.** When a source can't be capped
  at the API (a filter it doesn't take, a join, a count), and its table
  has a streaming function, each page is filtered by DuckDB as it arrives
  and only matching rows (and only the columns used) are kept — the whole
  API result is never held in memory. A plain list stops asking for pages
  once it has enough rows. `semantic.stream: false` turns it off.
- **"not" excludes.** "alerts that are *not* critical" filters
  `severity <> 'critical'` ("não", "sem", "never", "without"… too); accents
  never stop a match ("críticos" = "criticos").
- **Yes/no columns get words.** Catalog generation gives every boolean
  field words for its `true` value — from its name (`in_kev` → "in kev",
  "kev"; `mfa_enabled` → "mfa") plus what the LLM adds ("cisa kev", "known
  exploited") — so "CVEs in the KEV catalog" becomes `in_kev = true` and
  "not in KEV" `in_kev <> true`. A catalog drafted before this needs the
  table redrafted (Config → Semantic catalog → Redraft) or the words added
  by hand: `in_kev: {values: {"true": [kev, cisa kev, known exploited]}}`.

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
result.results     # pd.DataFrame (None unless status == "ok")
result.decisions   # every judgment, with its probability

# showing it to someone
result.report()                      # text: status, decisions, the answer or why not
result.to_json(results_only=True)    # just the rows, as JSON (dates in ISO)
result.to_json(indent=2)             # everything: status, SQL, decisions, rows, clarification
result.clarification, result.options # when status == "needs_clarification": why, and what to pick from
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
  (`extractor.type: "llm"`). You can replace every system prompt.
  **Questions in any language**: that same extraction call also reads
  the question in English (`intent.english_question`), so answer wording,
  retrieval and the catalog's keywords and synonyms only need to exist in
  English. "Quantos alertas críticos por regra?" is read as "How many
  critical alerts per rule?", and "críticos" matches `severity =
  'critical'`. No extra call is made.
  - **Values are protected.** IPs, emails, domains and quoted text become
    `⟦n⟧` placeholders before the LLM sees the question, and are put back
    after. Every other value it extracts must appear in the question as
    written, and verbatim in the translation.
  - **Unsafe translations are dropped.** A translation that loses a value
    is discarded, with a warning in the log, and the question is read as
    asked.
  - **The original is kept.** The decision engine gets both the English
    reading and the original. Clarifications and the transcript show
    your own words.
  - **Turning it off.** Use `"extractor": {"type": "llm", "translate":
    false}`. With `"type": "rules"` there is no translation, and the
    Portuguese answer wording in `shapes.py` still applies.
  **No LLM at question time?** Use `"extractor": {"type": "rules"}`.
  Values are then pulled out by rules and the catalog: IPs, domains,
  emails, time ranges ("last 24 hours"), catalog values and their
  synonyms, quoted terms, "user X". Every judgment stays with the decision
  engine (Jev), and the LLM only drafts the catalog. A
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

**Forcing a redraft, or updating part of the catalog.** Both take
*selectors*, which are fnmatch patterns and ignore case. The "service" is
the name you gave the connector in `services`:

| Selector | Matches |
|---|---|
| `glue` | every table of the `glue` service |
| `glue:security.*` | the `glue` tables in database `security` |
| `glue:security.proxy_logs` or `glue:proxy_logs` | one table of that service |
| `proxy_logs` / `security` | a table by its catalog name, its registered table, `database.table`, or any single argument |
| `*` | everything |

```bash
python -m duckduck.semantic generate-catalog                                   # new + expired only
python -m duckduck.semantic generate-catalog --force                           # redraft every generated source
python -m duckduck.semantic generate-catalog --force "*"                       # rewrite everything, hand-written too
python -m duckduck.semantic generate-catalog --force glue:security.proxy_logs  # exactly this table, nothing else
python -m duckduck.semantic generate-catalog --force glue adx:ProxyLogs        # these, nothing else
python -m duckduck.semantic generate-catalog --only glue                       # update (new + expired) glue's tables only
python -m duckduck.semantic generate-catalog --only glue --force               # redraft all of glue's tables
```

```python
generate_catalog(force=True)
generate_catalog(force="glue:security.proxy_logs")
generate_catalog(only="glue")
generate_catalog(only=["glue:security.*", "adx"], force=True)
```

- **Forcing by name** redrafts exactly those tables. It doesn't also
  pick up new or expired ones, and it includes hand-written sources.
- **Your `notes`** are kept in every case.
- **New tables in a connector** (a new Glue table, a new ADX table) are
  picked up by any run without `--force`, or with `--only` naming their
  service.
- **To start from nothing,** move the file away. Your notes go with it.

**What the data looks like.** Generation also profiles each table from
up to `profile_rows` rows (default 1000), with pandas and not the LLM,
and stores the result in the catalog:

- **Per field:** a `profile` with distinct values, share of empty
  values, and min/max (numbers and dates, including dates stored as text).
- **Per source:** how many rows were sampled and the date range of its
  time field.
- **Category columns** (few distinct values that repeat, like `severity`
  or `rule`) get every value added to `values`, so "critical" or "brute
  force" match even if the LLM didn't list them. Identifiers (IP, user,
  domain...) never become lists.

All of this goes into what the decision engine reads, e.g. *"… Known
values: 'low', 'critical'"*, *"Mostly empty (95% of sampled rows)"*,
*"Sampled records span 2026-09-01 to 2026-09-20"*. It's refreshed
whenever the source is redrafted. It describes the sampled rows, not
necessarily the whole table.

Real example values are **opt-in**, because they write real data into
the file: `"sample_values": 3`. Even then they're skipped for user/email
fields unless you set `"sample_sensitive": true`.

```json
"catalog_generation": {"profile_rows": 1000, "sample_values": 3, "max_enum_values": 20}
```

**Entities and their keywords, from the data.** After the LLM's drafts
are merged, generation tidies the vocabulary with plain rules (no model):

- **Value shapes type fields.** A column whose values are nearly all
  (≥ 90%) IP addresses, emails, URLs or domains gets that
  `semantic_type` when the LLM left it untyped. The shape is kept in
  the field's `profile`. The LLM's own typing always wins.
- **An entity no field holds is merged.** Say the LLM invents `host`,
  but your hosts are identified by IP address. No field is typed
  `host`, so a question about hosts could never be answered, only asked
  back. Instead, `host` is merged into the entity that is held: the one
  its description or keywords name ("identified by its IP address"),
  else the one held by the sources that claimed it. Its name and
  keywords (`host`, `hosts`, `machine`...) become keywords of that
  entity (`ip_address`), sources that listed it now list the target,
  and the run's warnings say so. It also applies to entities already in
  your catalog, on the next redraft. An orphan with nothing to merge
  into is kept, with a warning.
- **Field names become keywords** of the entity they hold (`src_ip` →
  "src ip" on `ip_address`, `owner` on `user`). Keywords are capped at 25.

So you don't need to add `host`/`hosts` to `ip_address` by hand: run
`generate_catalog(force=True)` and check `entities` in the file. You can
still add keywords yourself; the ones you add are kept.

**API documentation.** For a table behind an HTTP API, the API's own docs
say what the data sample can't: what a field means, every allowed value,
units and formats. Point tables at their docs with `api_docs`, keyed by
the same selectors as `--force` / `--only` (a whole service, or
`service:table`, which wins over the whole service):

```json
"catalog_generation": {
  "api_docs": {
    "insightvm": "docs/insightvm-openapi.json",
    "servicenow:incidents": {"location": "https://intranet.example.com/docs/incident.md"},
    "billing": {"location": "docs/billing.yaml", "operation": "GET /v2/invoices"},
    "helpdesk": {"content": {"tables": {
      "tickets": {
        "description": "Help-desk tickets, one per request.",
        "notes": "Closed tickets are purged after 90 days.",
        "fields": {
          "st": {"description": "Ticket state", "values": {"O": ["open"], "C": ["closed", "done"]}},
          "prio": "Priority, 1 (highest) to 4"
        }
      }
    }}}
  }
}
```

Docs can come from a **file** (a relative path resolves against the
config file's folder), a **URL**, or **inline** in the config
(`content`). Inline docs suit internal APIs with no published docs.
Three formats are understood:

- **OpenAPI / Swagger** (JSON or YAML). The operation behind each table
  is found by name: the table, the connector method, and its structural
  args (`assets` → `GET /api/3/assets`, the listing rather than
  `/assets/{id}`). Set `operation` when the name doesn't match. Only that
  operation goes to the LLM: its summary, its parameters, and the
  response fields that match the table's columns. Paged envelopes
  (`resources`, `value`, `data`...) are unwrapped, and nested objects are
  flattened with `_` the way DuckAPI does (`os.family` → `os_family`).
- **Field docs** (JSON or YAML) that you write for an API without a spec:
  `{"description", "notes", "fields": {column: "text" | {description,
  enum, values}}}`. For several tables in one file, use
  `{"tables": {name: {...}}}`; the entry named like the table is used.
- **Text** (Markdown, HTML, plain). Only the sections that mention the
  table or its columns are sent, up to `api_docs_max_chars` (default
  6000). A short document is sent whole.

What the docs settle is applied without the LLM:

- Documented `enum`s, and `values` with their synonyms, are added to the
  field's value list, even values the sample didn't contain.
- A field the LLM left without a description gets the documented one.

Each source records its docs as `api_docs: GET /api/3/assets
(docs/insightvm-openapi.json)`. A file that can't be read, or an
operation that can't be matched, is a warning, and the table is drafted
without docs. The docs describe the API as designed. The columns and the
profile describe what the connector actually returns, and only real
columns are cataloged.

**Watching it work.** Generation uses the same verbose mode, with the
same levels, as the virtualization layer: `True` / `"info"` shows
progress, and `"debug"` also shows what goes to the LLM (each table's
profile and sample rows) and what comes back. Turn it on with
`generate_catalog(verbose="info")`, which also works when you pass your
own `duck=`. The CLI takes `-v` / `-v debug`; `DuckAPI(verbose=...)` and
`DUCKDUCK_VERBOSE=info` work too. You get the plan (how many tables are new,
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
| `ask "..." --json` | `ask("...").to_dict()` (or `.to_json()`) |
| `ask "..." --interactive` | `search.conversation("...")` + `.answer(reply)` until `.done` |
| `generate-catalog` | `generate_catalog()` → `GenerationResult` (`.summary()`, `.catalog`, `.warnings`, `.path`) |
| `generate-catalog --out x.yaml` | `generate_catalog(out="x.yaml")` (`write=False` to keep it in memory) |
| `generate-catalog --force` | `generate_catalog(force=True)` |
| `generate-catalog --force glue:security.proxy_logs adx` | `generate_catalog(force=["glue:security.proxy_logs", "adx"])` |
| `generate-catalog --only glue` | `generate_catalog(only="glue")` |
| `jev-check` | `jev_check()` → `Classification` |
| `calibrate questions.json` | `calibrate("questions.json")` → `CalibrationReport` (`.summary()`, `.thresholds`) |
| `serve` | `serve()` — the web app ([Feedback](#feedback-learning-from-what-users-say)); `serve(run=False)` → the FastAPI app |
| `serve --edit-config` / `serve --no-sql` | `serve(allow_config_edit=True)` — saving `duckduck.json` from the Config tab; `serve(allow_sql=False)` — turns the SQL tab off |
| `feedback-report` | `feedback_report()` → `FeedbackReport` (`.summary()`, `.stats`) |
| `feedback-to-eval` | `feedback_to_eval()` → `FeedbackEvaluation` (`.summary()`, `.dataset`, `.report`, `.calibration`) |
| `feedback-suggest --accept ID` | `feedback_suggest(accept=["ID"])` → `SuggestionReport` (`.summary()`, `.suggestions`) |
| `feedback-export --since 7d --redact` | `feedback_export(since="7d", redact=True)` → `FeedbackExport` (`.summary()`, `.markdown`, `.path`, `.gaps`) |
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

### Feedback: learning from what users say

Users can say whether each answer answered their question, and why not.
That feedback is tied to the whole decision trail and turned into better
decisions, without retraining anything. None of it changes the catalog
until a person accepts it.

```bash
pip install -e ".[semantic,server]"
python -m duckduck.semantic serve              # http://127.0.0.1:8765
python -m duckduck.semantic feedback-report    # answer rates, what went wrong
python -m duckduck.semantic feedback-to-eval   # rated questions → evaluation set + suggested thresholds
python -m duckduck.semantic feedback-suggest   # catalog suggestions; --accept ID / --dismiss ID
python -m duckduck.semantic feedback-export    # what still fails, as a brief for a developer (Markdown)
```

```json
"semantic": {
  "feedback": {"enabled": true, "path": "feedback.duckdb", "memory": true,
               "max_cases": 3, "min_similarity": 0.6, "min_support": 2}
}
```

**The web page** (`serve()`) has four tabs:

- **Ask.** The follow-up questions show up as buttons, or you can answer
  in your own words. You get the answer, the SQL and "how it was decided"
  (every decision, its probability, and who made it). Under each answer:
  *"Did this answer your question?"* 👍 Yes / 🤏 Partly / 👎 No — **one tap
  saves it**, no send button, and tapping another changes it. After Partly
  or No, optional chips say what went wrong (wrong tables, wrong kind of
  answer, wrong filter, misread value, missing data, too many questions),
  a box takes your words, and *What would have been right?* takes the
  tables, the kind of answer, what it asks about, or *"word X means field
  = value"*. Everything saves as you go (one feedback per answer, updated
  in place). **Take over from here counts as a yes**: an answer nobody
  rated yet is recorded as answered when you take its rows over; a rating
  you already gave stays.
- **Three ways to read a question: 🦆 Paddle, 🤿 Dive or 🪽 Fly — or 🧭 Auto
  to pick one per question.** A switch next to the question picks the reader. Paddle skims the words on the surface: the
  `rules` reader. Dive goes under for what you meant: the `llm` reader. The
  API, CLI and config call them `rules` and `llm`. The **?** next to the
  switch explains both: how each reads, what it's good at, its limits, and
  its speed and cost. The choice is remembered in your browser;
  `semantic.reader` sets the default, `ask --reader llm` / `search(q,
  reader="llm")` in code.
  - **Paddle — `rules`** (as before). The wording decides the kind of answer and what
    it's about. The decision engine (Jev) settles what the wording leaves
    open, and a doubt is asked back.
  - **Dive — `llm`.** An LLM reads the question first, in the same call that
    translates it. It returns the kind of answer, what it's about (an
    entity, a field, a table or a value) and the grouping, using only
    names that exist in the catalog; anything else is dropped. Then Jev
    decides:
    - where the LLM and the rules agree, nothing changes;
    - where they disagree, both readings go to Jev, which also sees the
      LLM's reading on every question it's asked;
    - a doubt is still asked back, never guessed.

    The reading shows as a chip (*Dive found: a count of values · about field
    owners.department*) and as the first line of *How it was decided*.
    Needs an LLM (`extractor.llm` or `default_llm`). Without one the switch
    is disabled and says why. If the LLM call fails, the search falls back
    to the rules.
  - **Fly — `llm_decides`.** The LLM reads the question as in Dive *and*
    makes every decision Jev would: what it's about, which tables, fields
    and joins, the kind of answer. No Jev at all. It uses the same decision
    contract (`LLMDecisionBackend`, one LLM call per batch of decisions), so
    the guarantees stay: a doubt is asked back, and SQL is written only by
    the compiler from a checked plan. It's the slowest and costs the most;
    thresholds calibrated for Jev may fit it less well.
  - **Auto — `auto`.** Picks Paddle, Dive or Fly for each question from how
    similar questions went before:
    - **Past runs** (`FeedbackStore.runs()`): one per conversation, in the
      mode that started it, with the last round's outcome. A run counts as
      a success unless it was rated *not answered* (0) or *partly* (½), or
      its plan was invalid: **no negative feedback counts as positive**.
    - **Similar runs**: the question's template against each run's
      (question or English reading, whichever is closer), at least
      `router.min_similarity` (0.5); the `router.max_runs` (30) closest.
    - **Evidence per mode**: similarity-weighted runs and successes, a
      smoothed success rate ((ok + 1) / (runs + 2) — a mode with no similar
      runs has no evidence, not a bad record), median time, calls, how often
      it asked back, a few of the similar questions.
    - **Jev decides**: one choice question with that evidence as facts
      (the offline engine takes the best rate). **A tie goes to the
      cheaper mode**: modes within `router.tie_margin` (0.05) of the top
      are tied, and Paddle beats Dive beats Fly.
      Below `thresholds.router` (0.5), or if routing fails, the
      `router.fallback` mode (Paddle) reads it — a doubt here is never
      asked back.
    - The pick shows as a chip (*Auto picked Dive · llm: 2/2 similar ok*)
      and as the first line of *How it was decided*; a conversation keeps
      that mode for every round. The history marks it 🧭→, and the
      dashboard's *Auto's picks* shows how each pick went.
    - Limits: it needs rated history to learn anything; it costs one more
      decision-engine call per question (and per preview); it only learns
      about modes that have been used on similar questions. Evaluations
      turn the history off, so they never read the answers they measure.
  - **Every search records how it ran:** its mode, the engine that decided,
    its time, decision-engine calls, LLM calls and tokens, and the cost
    where the provider reports it (the Decisions API does, per call).
    - Previews while typing are metered too and kept apart per mode, so the
      cost of typing shows and nothing is counted twice. A reading reused
      from the preview shows as *reading from the preview* on the answer.
    - **Dashboard → Modes compared:** searches, answer rate, how often it
      asked back, median time, calls, tokens and cost per mode, plus the
      cost of typing.
    - **History:** a Mode column (hover for the engine), time and calls,
      and a filter by mode.
    - **Suggestions → Evaluate:** *also compare the modes* replays your
      rated questions through each mode and shows accuracy, asked-back
      rate, time and calls side by side
      (`evaluation.compare_readers(search, cases)` in Python).
    - The developer brief says which mode each failing question ran in,
      and its reproduce command includes `--reader`.
  - **Ask waits for the reading.** While the question is being read
    (either way), the Ask button is disabled and reads *Paddling…* or
    *Diving…*, while the duck says what it's up to ("rubber-duck debugging
    your question…"). Pressing
    Enter then asks as soon as the reading is done. With the LLM, the
    reading is cached for 5 minutes, so the preview and the search that
    follows make **one** LLM call.
- **Choosing systems while typing.** When you pause, the page asks for
  a *preview*: one decision-engine batch for entity, kind of answer and
  the relevance of candidate tables. It uses rule-based extraction (no
  LLM), runs nothing and records nothing. Chips then appear next to the
  input: **Systems** (the systems the question points at) and **About**
  (the entity).
  - Clicking *Systems* lists every table you may use, grouped by system
    (the `auto_register` service: SharePoint, the database...). The
    suggested ones are ticked, with their relevance, and each system has
    an **only this** button. Your choice goes with the question: only
    those tables are used, joins included.
  - **Joins.** When the answer needs another table to find what the
    question asks for ("which machines…" on firewall logs needs the asset
    inventory for hostnames), that table is ticked too, tagged *joined*,
    and the panel lists the join (`firewall_logs.src_ip =
    asset_inventory.ip_address`, to find the host). Untick a join and the
    answer routes around it (`join:<a>=<b>` pinned to no), e.g. through
    `dst_ip` instead.
  - Clicking *About* lets you pick the entity, which is then never asked.
  - *Let it choose* goes back to the automatic choice.
  - A choice belongs to the question it was made for: small edits keep
    it, a different question starts from its own suggestions. Asking
    before the pause, or clicking a suggested question, updates the chips
    too.

  In Python: `search.preview("...")` (with `joins`) and `search.search("...",
  only_sources=["sharepoint_list_items"])`, or
  `search.conversation(..., only_sources=..., pinned={"entity": "user"})`.
  The restriction narrows `allowed_sources`, never widens it. It's
  recorded as a `scope` decision (by the user). When the chosen tables
  can't answer, the question back only offers what they can: "which
  machines…" limited to the firewall offers its IPs, since the hostnames
  live in the inventory.
- **Icons.** Every table shows the kind of source it comes from:
  SharePoint, SQL database, ServiceNow, InsightVM, Axonius, NVD, REST Countries, S3/Glue,
  Azure Blob, Azure Data Explorer, local files, Python module, DuckDB, or
  another API. These are generic shapes, not brand logos, drawn from the
  connector behind each table (`SemanticSearch.source_icon`).
- **History.** Every question, with its result and its rating — and you
  can rate it right there: 👍 / 🤏 / 👎 on the row save with one tap (or
  change the rating it had). Click a question to open it: **the SQL it
  ran** (for "everything about X", one query per table it looked in), with
  *Open in the SQL tab*, and the rest of the feedback — what went wrong,
  your words, what would have been right — filled with what was saved and
  saving as you change it. After 🤏 or 👎 the row opens by itself.
- **Dashboard.** Answer rate overall, by kind of answer and by table; what
  went wrong; how often it asked back; searches per day.
- **Suggestions.** The catalog changes proposed from the feedback, to
  accept or dismiss, plus *Evaluate and calibrate*.

- **SQL**, right next to Ask and always there (on by default; `serve
  --no-sql` turns the console off and the tab says so). `duck.sql` on the registered tables, with
  push-down and all. The tables are listed by system with their icons;
  clicking one writes a query. *Open in the SQL tab* on an answer brings
  its SQL over. Each run shows the rows and **what went to each source**:
  which WHERE / LIMIT reached the API.
  - **Read queries only:** `SELECT`, `WITH`, `FROM`, `SHOW`, `DESCRIBE`,
    `SUMMARIZE`, `EXPLAIN`, `VALUES`, one at a time.
  - **Isolated connection.** They run on their own DuckDB connection,
    sharing your registered tables, with `enable_external_access = false`
    and `lock_configuration = true`: no files (`read_csv('/etc/passwd')`,
    `FROM 'x.csv'`), no `COPY` / `ATTACH` / `INSTALL`, no stored secrets,
    and the SQL can't turn that back on. Your own `DuckAPI` and the
    semantic search are untouched.
  - **Limits:** the first 1000 rows are shown, and a query is interrupted
    after 2 minutes.
- **Take over from here.** On an answer, *Take over from here* opens a
  dialog showing the rows and columns you'd get, with a table name
  suggested from the question. You can change the name; it's checked as
  you type. When the answer stopped at its row cap, it offers to fetch
  every matching row. Then it registers the table and opens the SQL tab
  on it. From there it's SQL: filter,
  aggregate, join it with the source tables.
  - **The whole subset.** When the answer stopped at its row cap
    (`default_limit`, 1000), its plan runs again without it, so the table
    holds every matching row. `full=False` keeps just the rows shown.
  - **A snapshot in memory.** Querying it never calls the sources again.
    Take over again (same name) for fresh rows. It lasts until the server
    restarts or the config is saved; nothing is written to disk.
  - **In Python:** `taken = search.take_over(result, "machines_seen")` (or
    `conversation.take_over(...)`), then
    `duck.sql("SELECT ... FROM machines_seen")`. `list_tables()` shows these
    tables as *Taken over*, with the question they came from.
- **Config → Semantic catalog.** Generate the catalog from the page — what
  `generate-catalog` does. The card shows the catalog file, the LLM that
  drafts it, and every table: when it was drafted and by which LLM (or
  *written by hand*), whether it has your notes, and the registered tables
  **not in the catalog yet**.
  - **Update catalog** drafts the new tables and those older than
    `max_age`; **Redraft all generated** redrafts every generated table (it
    tells you how many LLM calls first); **Redraft** / **Add** on a row does
    just that table.
  - It runs in the background: a progress bar (*Drafting 3 of 12…*), the
    generation's log as it happens, then what was drafted, kept or left for
    the next run (`max_tables`) and the warnings. The app reloads with the
    new catalog when it's done — questions use it right away. One run at a
    time.
  - Hand-written tables and your `notes` are kept, as in the CLI. Like
    saving the config, it rewrites a file and spends LLM calls, so it needs
    `serve --edit-config`; without it the card shows the catalog and says
    why the buttons are off.
- **Config.** `duckduck.json` as a **form** or as **JSON** (a toggle; both
  edit the same config, so a change in one shows in the other), next to
  **every option there is**, searchable.
  - **The form** is built from that same options reference: *General*
    (`on_error`), one card per **service** (name, connector, its options,
    `table_prefix`, and an `authentication` block that shows the keys its
    `type` needs, with buttons for the connector's credentials, e.g.
    `+ client_secret`), one card per **AI provider**, and every
    **semantic** section. Each option gets the right input: a list of
    choices, a number, true/false, comma-separated names, or a small JSON
    box for dicts. An empty field is left out of the file, so the default
    applies. Keys the form doesn't know are kept as they are.
  - **The JSON** is what gets saved: the form writes it as you type.
  - The reference lists each connector's parameters (with the tables it
    registers), the `authentication` block, an `ai_providers` entry, and
    the whole `semantic` section with defaults and descriptions.
  - **Masked secrets.** Passwords, secrets, tokens, API keys, private keys
    and connection strings show as `"***"`; `"$secret.<key>"` references
    and `*_id` / `*_env` names aren't secrets. Leave `"***"` to keep the
    saved value.
  - **Validate** checks connectors, `authentication` and the `semantic`
    section without saving.
  - **Save and reload** needs `serve --edit-config`. It validates first,
    keeps `duckduck.json.bak`, writes atomically and reconnects every
    service. A new feedback file path needs a restart.

It has no user accounts, and it answers from your data with your
credentials, so it listens on this machine only. To open it to others,
set `DUCKDUCK_SERVER_TOKEN`: every API call then needs that token, and the
page asks for it once.

**What gets recorded** (`FeedbackStore`, a DuckDB file) for each search,
including every round of a conversation:

- the question as asked, its English reading, and its *template*, with the
  values replaced by their kinds ("what can I find for this ip
  <ip_address>");
- the answer shape, entity, activity and tables;
- every decision, and the pins the user set;
- the SQL and the row count, **never the rows**;
- the catalog and engine versions.

In Python, `SemanticSearch(..., feedback=FeedbackStore(path))` records
automatically (`result.search_id`), and
`search.feedback(result, "not_answered", ["wrong_tables"], "why",
expected={"sources": ["owners"]})` stores what the user said.

**What's built from it:**

- **An evaluation set.** Answered questions label themselves: tables,
  entity, activity, kind of answer. A "no" with a correction carries the
  correction. `feedback_to_eval()` writes it in the `evaluation.json`
  format, scores the current catalog and engine on it (planning only),
  and runs the same threshold calibration as `calibrate`, now including
  `answer_shape`, on real questions.
- **Case memory** (`feedback.memory`). Questions whose template resembles
  one users confirmed or corrected before (`min_similarity`, at most
  `max_cases`) get those cases as evidence, never as a rule:
  - the decision engine sees them (`similar_confirmed_questions`, and per
    table `used_in_similar_confirmed_questions`), and still decides;
  - tables those cases used become candidates even when retrieval missed
    them, and are still judged;
  - offline, the lexical engine takes a very similar case's answer only
    where it had no evidence of its own.

  `result.intent.similar_cases` shows which cases were used. A later
  "no" on the same template retires an old "yes".
- **Catalog suggestions** (`CatalogSuggester`), each with the questions
  behind it:

  | Suggestion | From |
  |---|---|
  | synonym for a value | a correction "*word* means *field = value*" |
  | example question for a table | a correction naming the right tables |
  | wording for a kind of answer | a correction naming the right kind: the question's opening words ("give me a rundown") |
  | keyword for an entity/activity, note on a table | a word the catalog doesn't know, in at least `min_support` failed questions, whose corrections point there |
  | word the catalog doesn't know | the same, with no correction saying where it belongs (shown, nothing to apply) |

  Accepting a suggestion writes it to the catalog file, keeping a `.bak`
  copy. The file is rewritten, so `#` comments are lost; `notes` are
  kept. Accepted wording goes to `feedback.learned_answer_shapes`
  (`answer_shapes.learned.yaml`), which is loaded on top of
  `answer_shapes`. The web app reloads right away.

**What suggestions can't fix: the developer brief.** Suggestions fix
what the catalog can fix. A question that keeps failing after that is
usually a missing capability or a bug. `feedback_export()` (web app →
Suggestions → *Download the brief*) writes those questions as a Markdown
document, `feedback_export.md`, to hand to whoever changes the code.

- **What counts.** Only question patterns whose *latest* rating is still
  "not answered" or "partly". Questions are grouped by template, so
  "10.0.0.1" and "10.0.0.2" asked the same way are one gap. The most
  frequent come first.
- **For each gap:**
  - how often, and by how many people;
  - what the system did: kind of answer, tables, rows, and what it said
    or asked back;
  - every decision, with its probability and who made it;
  - the SQL;
  - what users said, and what they said would have been right;
  - an open suggestion that might already fix it;
  - a command that reproduces it with full logging.
- **The workflow it suggests:** reproduce the gap, decide whether it's the
  catalog or the code, fix it, then run `feedback-to-eval`. That run
  replays every rated question, so what users confirmed stays right.
- **Options:**
  - `since` (`"2026-09-01"` or `"7d"`) and `limit` narrow what's included;
  - `include_partial=False` leaves out "partly" ratings;
  - `redact=True` shows templates instead of questions and leaves out the
    SQL.
- **Before sharing.** Users' free-text reasons are kept as written, so
  read the brief before sharing it outside the team.

Measuring never learns from itself. `evaluate` and `calibrate` switch off
recording and case memory while they run, since the memory would hand
the engine the very answers they grade.

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

**How the decision engine is asked.** Each question takes **two
requests**:

- **One while interpreting it:** entity, activity, the type of a value
  nothing else could type, and the relevance of every candidate source,
  all independent, so they go together.
- **One after planning:** every field and JOIN the plan relies on.

If the question used words the catalog doesn't, a small third request
looks at the sources that declare the chosen entity/activity but weren't
among the candidates. Other details:

- **Criteria.** Yes/no questions carry explicit criteria. Source relevance,
  for example, counts "needed to connect the answer to what the question
  asks for" as relevant, so a lookup table like `owners` isn't judged
  irrelevant.
- **Notes.** Your `notes` (source and field) are part of what the engine
  reads, and also feed the lexical search that proposes the candidates.
- **Size and cost.** A request over the API's 32 KiB is split
  automatically. Repeated questions in a session come from a cache, and
  the cost the API reports is logged per call, with a session total.

**What kind of answer (which SQL).** Every question gets an
`answer_shape` decision, and the shape decides the SQL:

| Shape | Example | SQL |
|---|---|---|
| `list` | "Which hosts have critical alerts?" | `SELECT DISTINCT ip` (records: `SELECT *`) |
| `count` | "How many hosts have brute force alerts?" | `COUNT(*)` over that list |
| `values` | "Show me the different severities", "quais os tipos de regra" | `SELECT DISTINCT severity` |
| `count_values` | "How many different severities are there?" | `COUNT(*)` over `SELECT DISTINCT severity` |
| `count_by` | "How many alerts per rule?", "quantos alertas por cada regra" | `SELECT rule, COUNT(*) … GROUP BY rule` (hosts per rule: distinct hosts per rule) |
| `lookup` | "What can I find for 10.0.0.196?", "tudo sobre 10.0.0.196" | one `SELECT *` per table that has a field of the value's type |
| `locate` | "Which tables contain 10.0.0.196?", "em quais tabelas existe o ip …" | one `COUNT(*)` per table; no value → from the catalog alone |
| `catalog` | "What kind of information do you have?", "quais entidades existem no catálogo?", "what columns does alerts have?" | no SQL: answered from the catalog (see below) |

The decision works like a small tree:

1. **Clear wording is settled without a model.** "how many" / "quantos"
   is a count, "different" / "distinct" / "types of" / "diferentes" asks
   for values, and "per" / "for each" / "por cada" asks for a count per
   group. Combinations like "how many different" and "how many … per"
   are recognized too. No wording at all means a list.
2. **Ambiguous wording goes to the decision engine.** "How many alerts
   *by* severity?" could be one number or a breakdown, and "alerts for
   each rule" could be a list or a count per rule. Only the shapes the
   wording allows are offered, as a choice question in the same Jev
   request as entity and activity, so it costs no extra round trip.
3. **A doubt is asked back.** Below `thresholds.answer_shape` (0.60), the
   result is a `needs_clarification` with *"What kind of answer do you
   want?"* and one option per shape.

The shapes about a field (`values`, `count_values`, `count_by`) also need
to know which field:

1. **The field the question names** is used directly ("severities" →
   `severity`; for a count per group, the word after "per" / "by" wins).
2. **Otherwise the engine picks** among the primary source's fields
   ("the different detections" → `rule`).
3. **A doubt is asked back:** *"The different values of what?"* or *"A
   count for each what?"*. Its "none of these" answers as a plain list.

"The different users" names an entity, whose list is already distinct,
so it's answered as the list of users. Pin `answer_shape` (one of the
five) and `values_field` (`source.field`) to force either decision.
Words like "per", "different" or "types" are never taken as values to
filter on.

**Questions about the data itself (`catalog`).** "What kind of
information do you have access to?" is about the catalog, not about
records. It's answered from the catalog: no data is read, and nothing
else is decided or asked back (no entity, activity or table questions).
What it answers depends on the question's topic (`intent.catalog_topic`):

| Topic | Example | Answer |
|---|---|---|
| systems | "Which systems are connected?", "quais sistemas estão conectados?" | each connected system (the `auto_register` service): what it is, how many tables it registered, how many the catalog describes, examples |
| tables | "What kind of information do you have?", "show me your catalog" | each table, what it holds, an example question |
| entities | "Quais entidades existem no seu catálogo?" | each entity: description, keywords, the tables and fields that hold it |
| activities | "Which activities are there?" | each activity: description, keywords, what it's about, its tables |
| fields | "What columns does alerts have?", "quais campos tem a tabela owners?" | the named table's fields (all tables if none is named): type, meaning, description, known values |
| relationships | "How are the tables related?", "como as tabelas se relacionam?" | the joins: from, to, type, confidence |

Only the tables you're allowed to see (`allowed_sources`) show up.

**About me, or about your data?** Some questions could be either: "which
systems are connected?" could be about my setup, or about hosts connected
to each other in your data. Wording like that (systems, sources,
connectors or integrations near connected, configured or available, in
English or Portuguese; `answer_shapes.catalog.maybe_wording`) puts both
readings to the decision engine. If it isn't sure, the question comes
back as *"… could be about me — my setup — or about your data"* with both
options. A value in the question ("…connected to 10.0.0.5") makes it about
the data. Add
your own wording under `answer_shapes.catalog`; the topic is read from
the words entity/activity/field/column/relationship (and their
Portuguese forms).

**Small talk and questions that aren't about the data get a direct
reply.** No plan, no follow-up question:

- **Small talk** ("hi", "thanks a lot", "olá, tudo bem?", "tchau") is
  recognized by its wording (`answer_shapes.small_talk`), only when
  nothing else is asked: "hi, which hosts have critical alerts?" is a
  real question. It never calls the decision engine. A greeting is
  answered with example questions; thanks and goodbye get just a reply.
- **Out of scope** ("How is the weather in Lisbon?"): the decision engine
  gets one more yes/no in the same batch, "Is the question about the data
  these tables hold?". Below `thresholds.out_of_scope` (0.20) the answer
  is a reply saying what can be asked (the entities and activities of the
  tables you may use) plus example questions (each table's catalog
  `examples`, one per table first). Anything in between takes the normal
  path: in doubt, it tries the data. The offline lexical engine uses a
  rule instead: no catalog word, value, time range or retrieval hit means
  out of scope.
- **"It is about the data — try anyway"**: pin `in_scope: True`
  (`search(q, pinned={"in_scope": True})`, or the button in the web app)
  and neither check runs.

`SearchResult.reply` and `.suggestions` hold the reply, and `status` is
`"ok"`. The wording is in `clarification_texts` (`reply.greeting`,
`reply.thanks`, `reply.goodbye`, `reply.out_of_scope`, `reply.topics`
with `{topics}`, `reply.examples`, `reply.ask_anyway`).

**Dates and time windows.** A table's time field (its catalog
`time_field`, or the field typed `event_time`) takes the question's time
window as a filter: `>= start AND < end`, newest first. Questions in both
languages are understood by the rules (Paddle) as well as the LLM:

| Question | Window |
|---|---|
| "between today and tomorrow", "entre hoje e amanhã" | today 00:00 → the day after tomorrow 00:00 (the last day counts whole) |
| "from 2026-09-20 to 2026-09-22", "de 20/09/2026 até 22/09/2026" | Sept 20 00:00 → Sept 23 00:00 |
| "from 2026-09-20 10:00 to 2026-09-20 18:00" | exactly those times |
| "yesterday", "ontem", "anteontem" | that whole day |
| "today", "this week", "este mês" | from its start, no end (so far) |
| "last week" (rolling 7 days), "semana passada", "mês passado" (calendar) | as said |
| "in september", "em setembro de 2025" | that month (no year: this year, or last year if it's still to come) |
| "since monday", "desde segunda", "after yesterday" | from then on |
| "before 2026-09-21", "antes de ontem", "até ontem" | until then (*until/até* includes that day) |
| "in the last 24 hours", "nas últimas 24 horas", "últimos 7 dias" | ending now |

- Slash dates are day-first (20/09/2026). Month-first applies only when
  day-first can't be a date (09/25/2026).
- Weeks start on Monday.
- Times are in the search's clock, UTC by default, so "today" is the UTC day.
- A table without a time field isn't used for a question with a time
  window.
- "Show me the logins …" lists the login *records*: when the head noun
  names an activity (logins, connections, conexões) and no entity, the
  engine is told that the records of that activity are what's wanted.

**Naming a table shows it.** "show me table owners", "the alerts
table", "mostre a tabela owners", "preview owners" or "abra a tabela de
owners" answers with **that table's rows, every column**, without asking
anything. The table can be named by its catalog name, by the table it's
bound to, or with spaces for `_` ("the proxy logs table"), when *table /
tabela / dataset* is next to it or the question starts with *preview /
browse / open / abra*. Anything else the question says that fits the
table still applies: its known values ("table alerts with critical
severity"), a value of a type it has ("show table alerts for 10.0.0.3"),
the time range, and a row count ("first 20 rows of…", "últimas 5
linhas…"; otherwise `default_limit`). "Show me the owners" (no *table*)
is still the list of owners. Pin `answer_shape` to read it the usual way.

**A question that names a field asks for its values.** "What are the
severities of the events?" / "Quais são as severidades…?" / "List the
severities" has none of the *different/distinct* wording, but what it asks
for, the phrase after *what are the / list the / quais são as*, is a
field's name (`severity`). So the answer is that field's distinct values,
not every row. When the phrase names an entity instead ("list the
owners"), it stays a list, which is already one row per owner.
When the question names such a field, which thing it is *about* doesn't
matter. So the entity isn't asked (one less engine question) and the table
holding the field is picked first. The chips show *About: department
(owners)* and *Answer: the different values*. Clicking *About* lists the
fields of the relevant tables to pick another (`/api/ask` `values_field`,
or `pinned={"values_field": "owners.department"}` in Python), just as it
lists entities for a question about things.

**Everything about a value (`lookup`) and where it is (`locate`).** Both
look in **every table that has a field of the value's type**, not only
in the tables retrieval picked. Each value in the question is looked up
on its own, and no join is needed: it's one query per table.

- **`lookup` answers with the information itself.** `results` holds every
  row found, from every table, under one set of columns, with `source`
  first.
- **`locate` answers with the tables.** Its `results` is the summary.
- **Both fill `summary`** (one row per table: `source`, `found`, `rows`,
  `matched_on`, `description`) and **`sections`** (each table's part).

```python
result = search.search("Bring me information for this ip 10.0.0.196")
result.results     # source | ip | rule | severity | owner | department — the rows found
result.summary     # which tables had it, and how many rows
for sec in result.sections:
    print(sec.source, sec.matched_on, sec.rows)
    sec.results    # that table's rows, its own columns (None for locate, which only counts)
result.to_json()   # "results", "summary", "sections": [{source, matched_on, rows, sql, results}, ...]
```

- **Clear wording is a lookup:** "information for/about/on", "details
  for", "bring me data on", "everything related to", "tudo sobre" and so
  on. A question that carries a value but no such wording ("10.0.0.196?",
  "show me 10.0.0.196") asks the decision engine whether it wants a list
  or everything about the value, in the same batch as entity and
  activity. Offline, the lexical engine assumes a list.
- **No question about which table.** A lookup never asks which table
  should answer, since it looks in all of them.
- The question's other values (enumerated ones like "critical", a time
  range) apply in the tables that have those fields. With a time range,
  tables without a time field are skipped.
- `locate` without a value ("which tables have hosts?", "which tables
  exist?") is answered from the catalog, without reading data.
- `lookup` without a value ("tell me about the critical alerts") is
  answered as a list.
- A list that could only repeat the value asked about becomes a
  `lookup`, recorded as a deterministic `answer_shape` decision. For
  example, "which hosts are 10.0.0.196" lists the IP field filtered by
  that same IP, directly or through a join. A different field of the same
  type is still a real list: "which IPs connected to 10.0.0.5" filters
  `dst_ip` and lists `src_ip`. Pin `answer_shape: "list"` to keep the
  list anyway.

**Your own wording.** The phrases for each kind of answer live in
`duckduck/semantic/shapes.py` (`DEFAULT_WORDING`). Add yours, without
touching code, in `semantic.answer_shapes`, inline or as the path of a
JSON/YAML file (relative to `duckduck.json`):

```json
"semantic": {"answer_shapes": "answer_shapes.yaml"}
```

```yaml
# answer_shapes.yaml: added to the defaults, per shape
lookup:
  wording: ["o que rola com", "ficha do", "re:\\bdossi[eê]\\b"]
locate:
  wording: ["em que sistema"]
count_by:
  wording: ["distribuição por"]   # sure: a count per group
  maybe_wording: ["segundo"]      # maybe: the decision engine chooses
values:
  wording: ["quais os possíveis"]
  description: "the different values of an attribute"   # what the engine reads (optional)
```

- **Shapes:** `count`, `values`, `count_values`, `count_by`, `lookup` and
  `locate`. A plain list is what's left when nothing matches.
- **Phrases** match whole words, case-insensitive. `re:` starts a regex.
- **`replace: true`** swaps a shape's defaults for yours instead of adding
  to them.
- **Validation:** an unknown shape or key, or a bad regex, fails when the
  config loads.
- **Never a filter value:** matched words are dropped from the values to
  filter on.
- **Labels:** the options shown when the shape is asked back are the
  `answer_shape.<shape>` keys of `clarification_texts`.

**Asking the user back.** When a decision falls below its threshold,
the result is `needs_clarification`. It carries a `followup`: a question
for the user plus options, built from templates and the catalog (no
LLM). Each option *pins* the decision it answers. On the next run that
decision counts as the user's (`decided_by="user"`) and is never asked
again, so every round settles one doubt and the loop ends.

```python
conversation = search.conversation("Which hosts have brute force alerts?")
while not conversation.done:
    print(conversation.result.clarification_question)   # question + numbered options
    conversation.answer(input("> "))                     # "2", an option's name, or free text
result = conversation.result                             # "ok", or unresolvable: rephrase / fix the catalog
print(conversation.transcript)                           # the question, each clarification and reply
```

```
By “failed”, do you mean records whose result of the sign-in attempt is “failure”?
In the identity provider sign-in logs, the result of the sign-in attempt (auth_logs.outcome) has a value “failure” that looks like what you wrote.
  1) yes — only those records
  2) no — “failed” means something else
> 1
```

Every question comes with a `context` line (why it's being asked) and
yes/no options with a `detail` (what answering does). The texts use the
catalog's own descriptions, so good descriptions make good questions.
Technical names only appear in parentheses.

**Your own wording or language.** Override any text in
`semantic.clarification_texts` (the keys and placeholders are listed in
`duckduck/semantic/clarify.py`). An unknown key or placeholder fails
when the config loads:

```json
"clarification_texts": {
  "yes": "sim", "no": "não",
  "source.question": "Qual destes dados deve responder à sua pergunta?",
  "source.context": "Nenhum dos dados pareceu claramente certo para “{question}”.",
  "field_value.question": "Por “{term}”, você quer dizer registros em que {field} é “{value_label}”?",
  "field_value.yes": "só esses registros",
  "field_value.no": "“{term}” quer dizer outra coisa"
}
```

Replies are matched against the option labels, so with the texts above
the user can answer `sim` / `não`.

| `followup.kind` | Asked when | Choosing pins |
|---|---|---|
| `entity` | what the answer should list is unclear | `entity` |
| `value_type` | what a value is (`'github'`: domain? user?) is unclear | `value:<value>` |
| `value_term` | several words could be the value | `value_term` |
| `source` | no source looks relevant | `source:<name>` |
| `field` | a field (or a value's field) is doubtful: yes / no | `field:<source.field>` |
| `join` | a JOIN is doubtful: yes / no | `join:<a.x>=<b.y>` |
| `ignore_term` | every reading of a term was refused: answer without it? | `term:<words>` |
| `unresolvable` | no answer would help (nothing left to try) | nothing: no options |

**"No" opens the other possibilities.** A "no" is not the end of the
conversation. The refused field or JOIN is left out and the plan looks
for another way; only when there's none does the follow-up change to
what's still possible:

| Refused | Next |
|---|---|
| the field a value was searched in | another field of that type, in any source that fits; else *"What is “github”?"* without that type |
| the reading of a term (`“failed”` = `outcome 'failure'`) | another field that value appears in; else *"Should I answer without “failed”?"* |
| the field listed as the answer | another field that represents it, e.g. the owner of the machine via a JOIN; else the kinds of things that *can* be listed from there |
| a JOIN | another join path, each question naming the fields it matches |
| every option shown (entity, source) | **none of these** (always the last option): those are ruled out and the next ones are offered |

- **Free-text replies.** A reply that isn't an option's number or name
  goes to the decision engine as a choice question. If it isn't sure
  (below `thresholds.reply`, 0.70), the question stays open.
- **Round limit.** `max_rounds` (default 5) caps the loop.
- **Stateless use.** A web app can skip `Conversation` and keep the pins
  itself: `search.search(question, pinned={**previous, **option.pins})`.
  `result.to_json()` includes `followup` and `pinned`.
- **In the terminal:** `python -m duckduck.semantic ask -i "..."` asks at
  the prompt.

**Live evidence (opt-in).** Before deciding, the engine can be told
whether the question's values actually appear in the data. Each value
("github") is probed in the candidate sources' fields, and each field's
result goes to the decision engine as a fact:

- **Source relevance** gets "found in `proxy.domain`, not in `dns.query`".
- **"What is 'github'?"** gets the types of the fields it was found in.
- **A field confirmation** gets whether the value is in that field.

```json
"live_evidence": {"enabled": true, "max_probes": 8, "timeout": 5}
```

- **Only cheap probes.** A probe is `WHERE field ILIKE '%value%' LIMIT 1`
  at the source, so it only runs when the connector applies that filter
  itself (SQL, ADX, Glue/Blob, local files, ServiceNow `where`, `*_ilike`
  params) and takes a `limit`. Anything else is skipped as "couldn't
  check"; nothing is ever downloaded in full to check a value.
- **Bounded.** There's a budget per question (`max_probes`) and a
  timeout per probe. A failing or slow probe never fails the question.
- **Visible.** Each probe is logged (`evidence: 'github' in proxy.domain
  → found (0.08s)`) and listed in `result.evidence`.

It adds one small query per probe to every question, so it's off by
default. Turn it on when your sources answer such filters quickly.

**Tuning the thresholds.** The defaults (`source` 0.80, `field` 0.85, ...)
are round numbers. Tune them for your engine version with labeled
questions (a JSON list like `examples/semantic/evaluation.json`):

```bash
python -m duckduck.semantic calibrate my_questions.json                 # a wrong answer costs 5×, asking back 1×
python -m duckduck.semantic calibrate my_questions.json --cost-wrong 20 # being wrong is much worse
```

```python
from duckduck.semantic import calibrate

report = calibrate("my_questions.json", cost_wrong=5, cost_ask=1)
print(report.summary())
report.thresholds          # {"entity": ..., "activity": ..., "source": ...} → semantic.thresholds
```

It plans every question without fetching data, records every
probability, and picks per decision type the threshold with the lowest
total cost. It needs dozens of labeled questions per type to be
meaningful. Re-run it when you change the Jev version or the catalog.
`field` and `relationship` have no labels in that format, so set those by
hand if needed, e.g. `"thresholds": {"field": 0.8}`.

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

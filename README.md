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
pip install -e ".[dev]"     # pytest, for running the test suite
```

Requires Python 3.10+.

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

## Discovering what's registered

```python
duck.list_tables()          # pd.DataFrame
duck.sql("SHOW TABLES").df()
duck.sql("list all tables").df()
```

All three return the same thing — one row per registered table, with
`table_name`, `source` (which connector, or the function's own module for
a custom one), `endpoint` (its `base_url`/connection string when one
exists — password redacted), `streaming` (whether `stream()` also works
for it), `signature`, and `description` (the first line of its
docstring). Handy right after `auto_register()` to see what actually got
wired up, or from a notebook where you've lost track of what's been
registered.

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

The default decision engine is an offline lexical baseline. To use JEV,
implement its two-method `JEVBackend` protocol and pass
`engine=JEVAdapter(backend)`. See `examples/semantic/catalog.yaml` for the
catalog format and the "Semantic search" section of `CLAUDE.md` for the
design.

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

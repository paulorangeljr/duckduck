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
pip install -e ".[cert]"   # SharePoint auth via PFX/P12 or PEM certificate
pip install -e ".[aws]"    # AWS Secrets Manager support (auto_register)
pip install -e ".[dev]"    # pytest, for running the test suite
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
registers all of its tables in one call. It resolves credentials three
ways, so pick whichever fits how you manage secrets:

**1. AWS Secrets Manager, with the secret reference hardcoded in your code:**

```python
from duckduck import DuckAPI, SecretsManager

duck = DuckAPI()
duck.auto_register(
    {"sharepoint": {"secret_id": "prod/sharepoint/duckduck"}},
    secrets=SecretsManager(region_name="us-east-1"),
)
```

**2. AWS Secrets Manager, with the reference supplied at init time** (env
var, config file, whatever your deployment uses):

```python
import os

duck.auto_register(
    {"sharepoint": {"secret_id": os.environ["SP_SECRET_ID"]}},
    secrets=SecretsManager(),
)
```

**3. Offline — credentials passed directly, no AWS call at all:**

```python
duck.auto_register({
    "sharepoint": {
        "credentials": {
            "tenant_id": "...", "client_id": "...", "client_secret": "...",
        },
    },
})
```

The secret (from AWS or passed offline) is a plain dict/JSON matching the
wrapper's constructor — for SharePoint that's `tenant_id` + `client_id` +
one of `client_secret` / `thumbprint`+`private_key_pem` / `pfx_path` /
`private_key_pem`+`cert_pem`; for InsightVM it's `host` + `username` +
`password`. A PEM value with an escaped `\n` (common when a key passes
through a secrets manager) is fixed automatically.

Registered tables are always prefixed by the key you gave the service in
the config dict, so `sharepoint`/`insightvm` above become
`sharepoint_list_items`, `insightvm_assets`, etc. This is what avoids
collisions when two wrappers expose a same-named table (both SharePoint
and InsightVM have a `sites` table) and lets you register more than one
instance of the same wrapper:

```python
instances = duck.auto_register(
    {
        "sharepoint": {
            "secret_id": "prod/sharepoint/duckduck",
            "hostname": "company.sharepoint.com",
            "site_path": "/teams/myteam",
        },
        "insightvm_prod": {"type": "insightvm", "secret_id": "prod/insightvm"},
        "insightvm_dev": {
            "type": "insightvm",
            "credentials": {"host": "dev.local", "username": "a", "password": "b"},
        },
    },
    secrets=SecretsManager(region_name="us-east-1"),
)

duck.sql("SELECT * FROM sharepoint_list_items WHERE list_name = 'Tasks'")
duck.sql("SELECT * FROM insightvm_prod_assets WHERE hostname = 'web-prod'")

# The instance dict also gives you direct access to wrapper methods
# that don't map to a table:
instances["sharepoint"].site_by_path("company.sharepoint.com", "/sites/marketing")
```

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

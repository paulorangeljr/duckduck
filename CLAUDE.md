# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Install (editable, with dev deps)
pip install -e ".[dev]"

# Run all tests
python -m pytest tests/

# Run a single test
python -m pytest tests/test_core.py::test_limit_pushdown
```

## Architecture

`duckduck` is a thin SQL engine that lets callers query HTTP APIs as if they were tables. The two main layers are:

1. **`duckduck/core.py` — `DuckAPI`**: Parses SQL with `sqlglot`, extracts push-down predicates, rewrites the query replacing `func(...)` / `FROM func` patterns with in-memory DuckDB table names, then executes the rewritten SQL against registered DataFrames.

2. **`duckduck/rapid7.py` — `InsightVM`**: An example API client whose public methods are registered as DuckAPI tables. Each method accepts the push-down kwargs that DuckAPI injects and returns `list[dict]` or `pd.DataFrame`.

### Push-down flow

```
sql("SELECT * FROM assets WHERE hostname = 'web' LIMIT 10")
        │
        ▼
_extract_pushdown()   →  PushDownContext(limit=10, filters={"hostname": "web"})
        │
        ▼
_merge_kwargs()       →  intersects filters with function signature
                         explicit inline kwargs always win
        │
        ▼
fetch_function(**kwargs)  →  single API call (limit set) or full pagination
        │
        ▼
_to_dataframe()       →  normalises list[dict] / paged dict / DataFrame
        │
        ▼
conn.register(tmp_table, df)
        │
        ▼
conn.sql(rewritten_query)   ←  DuckDB applies remaining predicates
```

`PushDownContext` carries `limit: int | None` and `filters: dict[str, Any]`. Only equality/LIKE/comparison conditions on bare column names are extracted; OR, NOT, and nested expressions are left entirely to DuckDB.

### SQL rewriting

`DuckAPI.sql()` iterates registered functions and rewrites the query with two regex passes per function:

1. `func(kwargs)` — replaces the entire call expression with the temp table name.
2. `FROM/JOIN func` (no parens) — replaces the bare name after the keyword.

Temp table names are `_api_{fn_name}_{counter}`, unique per materialisation.

### Inline syntax convention

`func(param=val)` is **only for structural parameters** — those that determine the API URL path and are not columns in the result (e.g. `asset_id` → `/assets/{id}/vulnerabilities`).

Column filters belong in the `WHERE` clause; DuckAPI pushes them down automatically if the function accepts a matching parameter name.

```sql
-- CORRECT: structural param inline, column filter in WHERE
SELECT * FROM asset_vulnerabilities(asset_id=42) WHERE severity = 'critical'

-- CORRECT: column filter in WHERE, pushed down to assets()
SELECT * FROM assets WHERE hostname = 'web-prod' LIMIT 50
```

### `_fetch` vs full pagination

Every API method calls `self._fetch(path, limit=limit)`:

- **`limit` set** → one request, `size=limit, page=0`. Returns immediately.
- **`limit` not set** → iterates all pages with `default_page_size`.

Never pass `limit` as `page_size` to `_paginate`-style loops — that fetches everything N records at a time.

### Discovering registered tables

`DuckAPI.list_tables()` returns a DataFrame (`table_name`, `source`, `endpoint`, `streaming`, `signature`, `description`) for whatever's currently registered — handy after `auto_register()`, which can add many tables in one call. `sql()` intercepts `SHOW TABLES` / `LIST TABLES` (optionally `ALL`, case-insensitive, optional trailing `;`) as a shortcut for it, matched via `DuckAPI._LIST_TABLES_RE` **before** the regular push-down/rewrite path runs — so it never touches functions named literally `tables`, `show`, etc. (those still resolve as normal `FROM tables` queries).

`source`/`endpoint`/`description` are all best-effort introspection over the registered function, never anything tracked at registration time — `_describe_source` maps `fn.__module__` through `_CONNECTOR_SOURCE_LABELS` for a bundled connector (e.g. `"duckduck.servicenow"` → `"ServiceNow (HTTP API)"`), falling back to the module name itself for anything else; `_describe_endpoint` looks at `fn.__self__` (the bound instance, for a bound method) for a `base_url` attribute (every HTTP wrapper has one) or a SQLAlchemy `engine.url` (password redacted via `render_as_string(hide_password=True)`), returning `None` when neither is found (plain functions, `glue`/`blob_storage`, which have no single fixed endpoint); `_describe_function` is just the first line of `inspect.getdoc(fn)`. Adding a new bundled connector: add its module to `_CONNECTOR_SOURCE_LABELS` so it gets a proper `source` label instead of falling back to its raw module path.

```python
duck.list_tables()          # pd.DataFrame directly
duck.sql("SHOW TABLES").df()
duck.sql("list all tables").df()
```

---

## Adding a new API wrapper

Follow the same contract as `InsightVM`:

```python
class MyAPI:
    def __init__(self, ...):
        self.session = requests.Session()
        self.default_page_size = 500

    def _fetch(self, path, params=None, limit=None):
        # single page if limit set, paginate otherwise
        ...

    def records(
        self,
        # structural params (form URL path) — REQUIRED, no default
        resource_id: int,
        # column-filter params (WHERE push-down) — always Optional
        status: Optional[str] = None,
        name: Optional[str] = None,
        # limit is always the last param and always Optional
        limit: Optional[int] = None,
    ) -> pd.DataFrame:
        resources = self._fetch(f"/resources/{resource_id}/items", limit=limit)
        df = pd.json_normalize(resources, sep="_")
        if status and "status" in df.columns:
            df = df[df["status"] == status]
        return df
```

**Rules for each method:**

| Concern | Rule |
|---|---|
| Structural param (URL path) | Required positional, no default, document as "required" |
| Column-filter param | `Optional[X] = None`; filter the DataFrame after fetching |
| `limit` | Always `Optional[int] = None`, always last; pass straight to `_fetch` |
| Return type | `pd.DataFrame`; dot-separated nested keys become `_` via `sep="_"` |
| Server-side filter | Use it when the API supports it (fewer bytes over the wire) |
| Client-side filter | When the API has no filter param; filter the DataFrame, never slice with `head(limit)` — `_fetch` already capped the rows |

**Registration:**

```python
from duckduck import DuckAPI
from duckduck.myapi import MyAPI

api = MyAPI(...)
duck = DuckAPI()
duck.register_api_function("records", api.records)
duck.register_api_function("other",   api.other_method)
```

**Tests:** mock the HTTP session (`requests.Session`) and assert that push-down kwargs reach the method. See `tests/test_core.py` for the `tracked_*` wrapper pattern used to spy on calls.

---

## Streaming (incremental pagination)

`DuckAPI.sql()` materializes all the data before returning. For large datasets, use `DuckAPI.stream()`, which yields one `pd.DataFrame` per page as each HTTP request completes.

### Dual registration

```python
duck.register_api_function("assets", r7.assets)          # for sql()
duck.register_streaming_function("assets", r7.iter_assets)  # for stream()
```

### Usage in Jupyter

```python
for chunk in duck.stream("SELECT * FROM assets WHERE severity = 'critical'"):
    display(chunk)   # shows up as each page arrives
```

### `iter_*` contract (InsightVM and new wrappers)

- Accepts the same column filters as the regular method (no `limit` — stream iterates everything).
- `yield`s one `pd.DataFrame` per page via `self._iter_pages(path)`.
- Client-side filters are applied before the yield; pages that end up empty after filtering are skipped.

```python
def iter_records(self, status=None) -> Iterator[pd.DataFrame]:
    for page in self._iter_pages("/records"):
        df = pd.json_normalize(page, sep="_")
        if status and "status" in df.columns:
            df = df[df["status"] == status]
        if not df.empty:
            yield df
```

### `stream()` limitations

| Behavior | Detail |
|---|---|
| Tables | One per query — no JOINs between functions |
| `WHERE` / `SELECT` | Applied per chunk (correct) |
| `LIMIT N` | Applied per chunk, not globally |
| `ORDER BY` / aggregations | Operate per chunk, not over the total |

---

## SharePoint (Microsoft Graph API)

`duckduck/sharepoint.py` implements the same contract above but with protocol differences.

### Authentication (MSAL)

```python
from duckduck import SharePoint

# Client secret
sp = SharePoint(tenant_id, client_id, client_secret)

# SHA-1 thumbprint + PEM key (no extra dependency)
sp = SharePoint.from_thumbprint(tenant_id, client_id, thumbprint, private_key_pem)

# PFX/P12 file (requires: pip install cryptography)
sp = SharePoint.from_pfx(tenant_id, client_id, "/path/cert.pfx", pfx_password="...")

# PEM cert + PEM key (requires: pip install cryptography)
sp = SharePoint.from_pem_cert(tenant_id, client_id, private_key_pem, cert_pem)
```

`msal.ConfidentialClientApplication` keeps a token cache in memory; the token is renewed automatically when it expires.

### PEM with a literal `\n` (AWS Secrets Manager secrets)

When the private key/certificate comes from a secret (`SecretsManager.get_secret`,
env var, etc.) that was escaped twice, the line break arrives as the literal
2-character text `\n` instead of a real newline — the PEM becomes invalid for
MSAL/`cryptography`. `from_thumbprint` and `from_pem_cert` fix this
automatically via `_normalize_pem` (safe heuristic: a PEM's base64 body never
contains `\`, so it only rewrites when there's no real newline in the
string). This also covers `from_secret`, since it delegates to these two
constructors.

### `@odata.nextLink` pagination

The Graph API does **not** use `page`/`totalPages`. It returns `@odata.nextLink` in the response when there's more data:

```json
{"value": [...], "@odata.nextLink": "https://graph.microsoft.com/v1.0/...&$skiptoken=..."}
```

The `_iter_pages(url)` method follows the link until there's nothing left. The page-size parameter is `$top=N` (not `size` or `pageSize`).

```python
def _iter_pages(self, url: str) -> Iterator[List[Dict]]:
    if "$top=" not in url:
        url = self._with_top(url, self.default_page_size)
    next_url = url
    while next_url:
        payload = self._get(next_url)
        items = payload.get("value", [])
        if items:
            yield items
        next_url = payload.get("@odata.nextLink")
```

### Nested list fields (`_normalize_list_items`)

The Graph API returns list items like this:

```json
{"id": "1", "fields": {"Title": "foo", "Status": "Active"}}
```

`_normalize_list_items()` elevates the `fields` sub-object into top-level columns, prefixing metadata with `_`:

```
_item_id | _created_at | Title | Status
1        | 2024-01-01  | foo   | Active
```

This allows `WHERE Title = 'foo'` directly in SQL, without qualifying with `fields_`.

### Column names: internal vs. displayName (`column_names`)

The Graph API exposes a list item's fields by the column's **internal name**
(`field_1`, `OData__ColorTag`, etc.), which rarely matches the name shown in
the column header in the SharePoint UI. `list_items()` / `iter_list_items()`
accept `column_names: Literal["display", "internal"] = "display"`:

- **`"display"`** (default): fetches `/lists/{id}/columns` (cached per
  `site_id:list_id` in `SharePoint._column_map_cache`) and renames the
  `fields` entries to their `displayName` **before** registering the
  DataFrame in DuckDB. Any `WHERE`/`SELECT` in the query already operates on
  the name shown on the web — there's no additional resolution in
  `core.py`, because the display name is already the DataFrame's real
  column.
- **`"internal"`**: skips the `/columns` lookup (one fewer HTTP request) and
  keeps the Graph API's raw name.

```python
# default — names as shown on the web
duck.sql("SELECT * FROM list_items(site_id='abc', list_id='def') WHERE Status = 'Active'")

# Graph internal names, no extra request
duck.sql("SELECT * FROM list_items(site_id='abc', list_id='def', column_names='internal')")
```

When adding field normalization to another wrapper: always resolve and
rename **before** `self.conn.register(...)` — never after. DuckAPI's
push-down and `WHERE` operate on top of the columns of the already
materialized DataFrame, so any name mapping needs to happen at that single
point.

### Structural parameters in SharePoint

`site_id`, `list_id`, `drive_id`, `item_id`, `folder_path` are always structural (they build the URL). All required, no default:

```python
def list_items(self, site_id: str, list_id: str, limit: Optional[int] = None) -> pd.DataFrame:
    url = f"{GRAPH_BASE}/sites/{site_id}/lists/{list_id}/items?expand=fields"
    return self._normalize_list_items(self._fetch(url, limit=limit))
```

```sql
-- site_id and list_id are structural → inline
SELECT * FROM list_items(site_id='abc', list_id='def') WHERE Title = 'Report'
```

### Dependencies

| Feature | Package |
|---|---|
| Base | `msal>=1.20` (already in `[dependencies]`) |
| PFX / PEM cert auth | `cryptography>=41.0` (`pip install "duckduck[cert]"`) |

---

## Auto-registration (`DuckAPI.auto_register`)

Registering each method by hand (`register_api_function`/`register_streaming_function`
method by method) gets repetitive when you want to bring up every wrapper at
once. `DuckAPI.auto_register()` instantiates the known wrappers (see
`duckduck/registry.py` → `SERVICE_REGISTRY`) and registers all their tables
automatically. Each service in the config has a `connector` (which wrapper
it is) and an `authentication` block (where its credentials come from):

```python
from duckduck import DuckAPI

duck = DuckAPI()
instances = duck.auto_register({
    "sharepoint": {
        "connector": "sharepoint",
        "hostname": "company.sharepoint.com",
        "site_path": "/teams/myteam",
        "authentication": {
            "type": "aws",
            "region_name": "us-east-1",
            "secret_id": "prod/sharepoint/duckduck",
        },
    },
    "insightvm_dev": {
        "connector": "insightvm",       # multiple instances of the same wrapper
        "host": "dev.local",
        "authentication": {"type": "local", "username": "a", "password": "b"},
    },
})

duck.sql("SELECT * FROM sharepoint_list_items WHERE list_name = 'Tasks'")
duck.sql("SELECT * FROM insightvm_dev_assets WHERE hostname = 'web-prod'")
```

### `authentication.type`

| `type` | Extra required keys | Where credential values come from |
|---|---|---|
| `"local"` | none | Every other field in the block, used exactly as written — hardcoded, no secret store involved |
| `"aws"` | `secret_id` (plus optional `region_name`, `profile_name`) | AWS Secrets Manager, via `SecretsManager` (`duckduck/secrets.py`) |
| `"azure"` | `secret_id`, `vault_url` (plus optional `tenant_id`) | Azure Key Vault, via `AzureKeyVaultSecrets` (`duckduck/azure_secrets.py`) |

`profile_name` (aws) selects a named AWS profile/account from `~/.aws/credentials` when more than one is configured locally — without it, the default profile (or instance/task role) is used. `tenant_id` (azure) pins `DefaultAzureCredential` to a specific Azure AD tenant when the caller has access to more than one; it covers the interactive-browser and CLI/env credentials in the default chain but not every possible one — for full control, build the credential yourself and pass it via `auto_register(secrets={"azure": AzureKeyVaultSecrets(vault_url=..., credential=your_credential)})`.

For `"aws"`/`"azure"`, the fetched secret (a JSON object) is used as the base credentials dict. Any other field in the `authentication` block layers on top of that:

- a literal value overrides/adds that key outright (e.g. `"host": "override.local"`);
- a value written as `"$secret.<key>"` is pulled from `<key>` in the fetched secret instead — lets you rename a field without duplicating the rest of the secret by hand (e.g. `"password": "$secret.svc_password"`).

This resolution lives in `DuckAPI._resolve_authentication`, and only touches the `authentication` block — everything else in a service's config (`connector`, `hostname`, `site_path`, `host`, `default_page_size`, ...) is passed straight through to the connector's constructor as `**overrides`.

### Secret backends and caching

`auto_register(secrets=...)` takes an optional `{"aws": <SecretsManager>, "azure": <AzureKeyVaultSecrets>}` override dict — when a service's `authentication.type` has a matching entry, that instance is used as-is (handy for tests, or to reuse a client across calls). Otherwise `DuckAPI._get_secrets_backend` builds one automatically and caches it per distinct `("aws", region_name, profile_name)` / `("azure", vault_url, tenant_id)` key, so N services with the same region+profile (or vault+tenant) share one client, while different ones each get their own.

### Resilience: `on_error`

`on_error="raise"` (default) is unchanged — a bad service raises immediately and stops `auto_register()` cold. `on_error="warn"` wraps each service's entire block (connector lookup, `authentication` resolution, `from_secret`, table/streaming registration) in try/except: a failure becomes a `RuntimeWarning` naming the service and the underlying exception, that service is skipped, and every other service still registers normally. A JSON config file's top-level `"on_error"` key is the default when the parameter isn't passed explicitly; passing `on_error=` always wins over that.

**Gotcha this warning had to work around**: every one of these warnings is raised from the exact same `warnings.warn(...)` call site (one line in `auto_register`). Python's default filter shows only the *first* occurrence of a given `(message, category, module, lineno)` per process — so a second identical failure (retrying a script/notebook cell against the same still-broken connector, or two services failing with the same message) would otherwise silently produce **no warning at all**. The fix is to scope `warnings.simplefilter("always", RuntimeWarning)` inside a `catch_warnings()` block around just this one `warnings.warn()` call, so it always displays regardless of `__warningregistry__` state or the caller's own filters. `tests/test_auto_register.py::test_on_error_warn_fires_every_time_even_for_identical_repeated_failures` guards this specifically — it deliberately avoids `pytest.warns()`/`recwarn`, since both reset filters to `"always"` internally and would mask a regression here even without the fix.

### Loading from a JSON file (`duck.auto_register()` with no arguments)

When `services` is omitted, `auto_register()` loads it from a JSON file instead — so a fully configured instance is just `DuckAPI().auto_register()`. File lookup order: `config_path` argument → `DUCKDUCK_CONFIG` env var → `DuckAPI.DEFAULT_CONFIG_PATH` (`"duckduck.json"`) in the current directory. Resolution happens in `DuckAPI._load_auto_register_config`. See `duckduck.example.json` in the repo root for a full example covering all three `authentication` types; `duckduck.json` itself is gitignored since a `"local"` block can hold literal secrets.

```python
duck = DuckAPI()
duck.auto_register()
```

Passing `services=` explicitly always skips the file lookup entirely, even if no config file exists.

### Rules

| Concern | Rule |
|---|---|
| Table prefix | Always `{name_in_dict}_{table}` — avoids collisions when two services expose the same table (e.g. `sites` in SharePoint and InsightVM) and allows multiple instances of the same wrapper |
| `connector` | Optional; defaults to `name` itself. Use it when `name` doesn't match a `SERVICE_REGISTRY` key (e.g. `insightvm_dev`) |
| `authentication` | Required on every service; always has a `type` (default `"local"` if omitted) |
| Fetched secret shape | Plain JSON with the keys expected by the connector's `from_secret` (`tenant_id`/`client_id`/`client_secret` for SharePoint; `host`/`username`/`password` for InsightVM) |
| Return value | `{name: instance}` — to call methods that didn't become a table (e.g. `instances["sharepoint"].site_by_path(...)`) |

### Generic database connector (`connector: "database"`)

`duckduck/database.py` — `SQLDatabase`, built on SQLAlchemy — covers SQL Server, MySQL, PostgreSQL, SQLite, Oracle, etc. through their SQLAlchemy dialect driver. Unlike SharePoint/InsightVM there's no fixed set of endpoints, so it registers just two tables (`{name}_table`, `{name}_query`) and reuses the existing structural-parameter convention instead of needing a table list in the config:

```sql
SELECT * FROM sqlserver_table(table_name='dbo.Customers') WHERE status = 'active' LIMIT 50
SELECT * FROM mysql_query(sql='SELECT * FROM orders WHERE total > 100')
```

`table_name`/`sql` are structural (required, no default); `limit` on `table()` is pushed down server-side via SQLAlchemy's `.limit()` (translates to `TOP`/`LIMIT`/`FETCH` per dialect); `limit` on `query()` is applied client-side after the raw query runs. Column filters in `WHERE` aren't pushed down server-side for either — DuckDB applies them on the fetched result, same as any push-down parameter a wrapper doesn't recognize. `from_secret` accepts either a full `connection_string` or discrete `drivername`/`username`/`password`/`host`/`port`/`database` fields (the latter matches what managed secrets, e.g. AWS RDS, already store).

**Sourcing the connection string from a secret manager**: if the secret already has a `connection_string` key, `authentication` needs nothing else — `"authentication": {"type": "aws", "secret_id": "prod/sqlserver"}` is enough, since the fetched secret becomes the credentials dict as-is. A managed secret with discrete fields (RDS-style: `username`/`password`/`host`/`port`/`dbname`) needs `drivername` added (RDS never stores it) and any mismatched key renamed via `"$secret.<key>"` — e.g. `"database": "$secret.dbname"` bridges RDS's `dbname` to the `database` field `from_secret` reads. See `duckduck.example.json`'s `sqlserver_from_rds_secret` entry for the full example.

### ServiceNow connector (`connector: "servicenow"`)

`duckduck/servicenow.py` — auth against the ServiceNow Table API (`/api/now/table/{tableName}`), either Basic (`ServiceNow(...)`) or OAuth2 client-credentials (`ServiceNow.from_oauth2(...)`). Like `database`, has a generic escape hatch (`table(table_name=..., query=None, limit=None)`, structural `table_name`) plus dedicated methods with real push-down for the tables people actually query most: `incidents`, `problems`, `change_requests`, `users` (`sys_user`), `cmdb_ci`. Each dedicated method's kwargs (`state`, `priority`, `assigned_to`, `type`, `active`, `sys_class_name`, ...) are joined with `^` into ServiceNow's own encoded `sysparm_query` syntax via `_build_query` — `^` is AND, `^OR` is OR, `^NQ` groups OR'd conditions (pass a raw string via `table(query=...)` for anything beyond simple equality). Pagination is `sysparm_limit`/`sysparm_offset`, incremented by `_iter_pages` until a page comes back shorter than the page size — no `page`/`totalPages` or `@odata.nextLink` here.

**Auth modes** — `from_secret` auto-detects which one from the secret's keys:

- Basic: `username`/`password` plus **either** `instance` (e.g. `"dev12345"` for `dev12345.service-now.com` — no domain) **or** `host` (a full custom domain, used as-is instead of appending `.service-now.com`).
- OAuth2 (`client_id`+`client_secret`+`token_url` present): common in enterprise deployments where an external IdP issues the token (e.g. Azure AD) and/or the data API sits behind a gateway that isn't `*.service-now.com` at all. `token_url` (the OAuth2 token endpoint — Azure AD's, ServiceNow's own `/oauth_token.do`, or a gateway's) and the data API's base URL (`instance`/`host`, or `api_base` for a gateway using its own path convention, e.g. `/v1/now` instead of `/api/now`) are fully independent — one doesn't imply the other. `resource` (Azure AD's audience param) and `scope` are optional form fields on the token request. `_ensure_token` caches the bearer token and refreshes it 60s before `expires_in` runs out, checked in `_get` before every request — same pattern as `SharePoint._get_token`'s MSAL silent-refresh.
- Not yet implemented: retry/backoff on transient failures, and `sys_id`-keyset pagination (an alternative to offset pagination for very large tables, where deep offsets get slow) — both real needs surfaced by comparing this wrapper against an existing internal client, not yet built.

### Axonius connector (`connector: "axonius"`)

`duckduck/axonius.py` — `api-key`/`api-secret` header auth against Axonius's POST-based REST API v2 (`/api/devices`, `/api/users`), which takes a JSON body shaped `{"meta": null, "data": {"type": "entity_request_schema", "attributes": {"page": {"offset", "limit"}, "filter": "<AQL>"}}}`. Column filters (`hostname`, `os_type`, `username`) are translated into AQL fragments and ANDed together via `_build_aql`; pass raw AQL via `devices(filter=...)`/`users(filter=...)` for anything more advanced. Pagination increments `offset` by `default_page_size` until a page comes back short, same shape as ServiceNow/InsightVM. `from_secret` expects `instance`, `api_key`, `api_secret`.

**Caveat**: this session's network policy blocked fetching `docs.axonius.com` while building this wrapper, so the request/response shape is based on published examples (the header auth and `entity_request_schema` body are confirmed) rather than a full read of the reference docs. `_normalize_assets`'s assumed `{"id", "type", "attributes"}` response envelope and the AQL syntax in the convenience filters are the parts most likely to need adjusting against a real instance's own API docs (usually `https://<instance>/api-docs`).

### Lakehouse connectors: `glue` (S3) and `blob_storage` (Azure)

`duckduck/lakehouse.py` — `LakehouseConnection` — is shared scanning plumbing used by both. Unlike every other wrapper, these don't fetch data into Python at all: they build a `read_parquet`/`delta_scan`/`iceberg_scan` expression and run it through a **private DuckDB connection** (`INSTALL`/`LOAD` the `httpfs`/`delta`/`iceberg`/`azure` extensions, `CREATE SECRET` for auth), so the actual scan is native DuckDB reading S3/Blob directly — only the final result crosses into a `pd.DataFrame`, same contract as every other wrapper from DuckAPI's point of view. `INSTALL` needs outbound internet the first time each extension is used (downloaded from DuckDB's own extension repository and cached under `~/.duckdb/extensions`).

- **`glue`** (`duckduck/glue.py` — `GlueTable`): `table(database=, table_name=)` (both structural) looks up the table's location + format in the **AWS Glue Data Catalog** via `boto3`'s `get_table`, then auto-detects Parquet vs Delta (`Parameters.table_type == "DELTA"` or `spark.sql.sources.provider == "delta"`) vs Iceberg (`Parameters.table_type == "ICEBERG"` or a `metadata_location` parameter — the Spark/Athena/PyIceberg Glue-catalog convention) and scans accordingly. `path(s3_path=, format=)` bypasses Glue for ad hoc reads. S3 auth is a `CREATE SECRET` built either from explicit `aws_access_key_id`/`aws_secret_access_key` or `PROVIDER credential_chain` (+ `PROFILE`/`REGION`) — the same credentials boto3's own Glue lookup uses, kept in sync deliberately.
- **`blob_storage`** (`duckduck/blob_storage.py` — `BlobStorage`): `table(container=, path=, format="parquet")` (both structural) reads `az://{container}/{path}` directly; `format` also accepts `csv`/`json`/`delta`/`iceberg`. Auth is `CREATE SECRET (TYPE AZURE, ...)`, either `CONNECTION_STRING` or `PROVIDER CREDENTIAL_CHAIN` + `ACCOUNT_NAME` (Azure CLI login / managed identity — no Glue-Data-Catalog equivalent here, so there's no format auto-detection, just the `format` parameter).

Neither pushes `WHERE` column filters down through the scan — DuckDB applies them on the already-scanned result, same as any push-down parameter a wrapper doesn't recognize (the Parquet/Delta/Iceberg readers do their own internal filter/projection push-down during the scan, independent of DuckAPI's push-down layer). `iter_table()` on both is a post-hoc chunk split of the full scanned result (same trade-off as `SQLDatabase.query()`'s client-side `limit`), not true incremental streaming.

### Adding a wrapper to auto-registration

1. Implement `Wrapper.from_secret(cls, secret: dict, **overrides) -> "Wrapper"` on the wrapper class — it decides the authentication mode from the keys in `secret` and passes `overrides` (hostname, site_path, default_page_size, etc.) through to the constructor.
2. Add an entry to `SERVICE_REGISTRY` (`duckduck/registry.py`) with `factory=Wrapper.from_secret` and the `tables`/`streaming_tables` maps.

### Adding a secrets backend

A backend just needs `get_secret(secret_id: str) -> dict` — see `SecretsManager`/`AzureKeyVaultSecrets` for the pattern (an optional `client=` for tests/DI, an in-memory cache, and a JSON-parse of whatever the underlying store returns). Wire it into `DuckAPI._get_secrets_backend`'s `if auth_type == "aws" / else` branch and extend the `authentication.type` validation in `_resolve_authentication`.

### Dependencies

| Feature | Package |
|---|---|
| AWS Secrets Manager | `boto3>=1.28` (`pip install "duckduck[aws]"`) |
| Azure Key Vault | `azure-identity>=1.15`, `azure-keyvault-secrets>=4.7` (`pip install "duckduck[azure]"`) |
| `connector: "database"` | `sqlalchemy>=2.0` (`pip install "duckduck[database]"`) + the driver for your engine (`pyodbc` for SQL Server, `PyMySQL` for MySQL, `psycopg2-binary` for PostgreSQL, ...) |
| `connector: "glue"` | `boto3>=1.28` (`pip install "duckduck[aws]"`) for the `get_table` lookup; DuckDB's own `httpfs`/`delta`/`iceberg` extensions auto-install on first use (needs outbound internet) |
| `connector: "blob_storage"` | none as a Python package — DuckDB's own `azure`/`delta`/`iceberg` extensions auto-install on first use (needs outbound internet) |
| `authentication.type: "local"` | none — fully offline |

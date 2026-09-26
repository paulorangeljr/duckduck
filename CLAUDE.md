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

`PushDownContext` carries `limit`, `conditions` (every simple `col <op> literal` ANDed in the WHERE, as `duckduck.pushdown.Condition(column, op, value, table)`), `filters` (the `eq` ones only, `{col: value}`, kept for compatibility), `complete` (False when the WHERE has anything not extractable — OR, NOT, IN, functions, column-to-column) and `limit_safe` (False for JOIN / GROUP BY / DISTINCT / ORDER BY / aggregates / windows / subqueries / set operations).

### Operator → parameter convention (`duckduck/pushdown.py`)

What a connector can filter server-side is declared in its signature:

| SQL | Parameter | Receives |
|---|---|---|
| `col = v` | `col` | `v` |
| `col LIKE 'p'` | `col_like` (server matches case-sensitively) or `col_ilike` (case-insensitively — a superset for LIKE, which is fine) | the SQL pattern |
| `col ILIKE 'p'` | `col_ilike` only | the SQL pattern |
| `col > v` / `>=` / `<` / `<=` | `col_gt` / `col_gte` / `col_lt` / `col_lte` | `v` |
| any of the above | `where` | `List[Condition]` — all of them (sources that apply arbitrary conditions natively: `SQLDatabase`, `glue`, `blob_storage`, `adx`, every `servicenow` table method) |

- A LIKE reaches a `_like`/`_ilike` param **only** if `parse_like` can translate it exactly (`'x'`, `'x%'`, `'%x'`, `'%x%'`). `_` (single-char wildcard), inner `%` and backslashes stay with DuckDB — pushing `'web_prod%'` as a literal prefix would return a *subset*. Connectors translate with `require_like()` into their own operator (ServiceNow `LIKE`/`STARTSWITH`/`ENDSWITH`, InsightVM `contains`/`starts-with`/`ends-with`/`is`, Axonius `regex(..., "i")`).
- **Superset, never subset.** DuckDB always re-applies the full WHERE, so a looser server-side match is safe; a stricter one silently loses rows.
- **LIMIT reaches the source only when it can't change the answer**: `limit_safe`, `complete`, and the function consumed *every* condition. Otherwise `WHERE ip = 'x' LIMIT 1` against a function with no `ip` param would fetch 1 arbitrary row and then filter it away. (Before this, LIMIT was pushed unconditionally and LIKE/comparisons were pushed *as equality* — `hostname LIKE '%web%'` arrived as `hostname='%web%'`.)
- **Qualified conditions** (`x.col = 1`) only reach the function whose name or alias (`FROM f AS x`, `JOIN f x`) matches, found by `DuckAPI._alias_at`; bare ones reach every function that accepts them, as before.
- `stream()` maps against the *iter function's* own signature, never pushing LIMIT.
- **Named parameters before `where`** (`assign_conditions`): an `eq` on a declared parameter — above all a structural one, `WHERE table_name = 'x'` — fills that parameter even when the function also takes `where`; `where` only gets what's left. (Sending everything to `where` left required structural params unfilled.)
- **Empty results never fail.** `_materialize` accepts an empty result: a DataFrame that still has columns (SQL/ADX/lakehouse) is used as-is; one with no columns at all (an empty JSON list) becomes an empty table shaped by the columns the query references for that table (`_referenced_columns`: bare, or qualified by its name/alias; minus inline args and required params — `_structural_names`), falling back to a single `_no_rows` column for a bare `SELECT *`. Those columns are `object` dtype on purpose: DuckDB accepts it in numeric comparisons, LIKE, aggregates and timestamp comparisons alike, where `string`/`float64` each fail some.
- **`pushdown_blocker` hook**: a connector taking `where` can define `pushdown_blocker(self, condition) -> Optional[str]` on its class (reason it can't apply that condition, or None). `pushdown.blocker_of(fn)` finds it on the bound method's instance; refused conditions never enter `where`, stay with DuckDB, show as `✗ ... — <reason>` in verbose output, and count as unconsumed for the LIMIT decision. Without the hook, a `where` connector is trusted to apply everything it receives.
- `SQLDatabase` translates DuckDB LIKE semantics exactly per dialect: `\` escaped and declared as ESCAPE (MySQL treats it as one by default), and on SQL Server `[` escaped (a character class there). Values are always bound parameters.

### Verbose mode (`duckduck/logs.py`)

`DuckAPI(verbose=True | "info" | "debug")` (or the `DUCKDUCK_VERBOSE` env var; `python -m duckduck.semantic -v`) attaches one console handler to the `duckduck` logger — standard `logging`, so an app can route the same records elsewhere instead; it's process-wide (last call wins) and silent by default.

- **Per call** (`DuckAPI._plan_call` → `_log_call`): `▶ assets(hostname_ilike='db%', limit=5)` then one line per decision — `✓ cond → param` or `✗ cond — why` (`_why_not_pushed`: no parameter / untranslatable LIKE / `_like` can't take ILIKE), and the LIMIT's fate with its blocker (`_limit_blocker` names it: ORDER BY, JOIN, aggregate...; or "not every WHERE condition reached the source"). `pushdown.assign_conditions` is the single source of truth for condition → parameter, used by both `map_conditions` and the report.
- **HTTP**: `instrument_session()` adds a `requests` response hook (InsightVM, ServiceNow, Axonius sessions); SharePoint and the ServiceNow OAuth token call use `log_http()` directly (the token call with `log_body=False` — its form body is the client secret). One line per response: method, URL, status, time, size; request body at DEBUG.
- **Pagination**: `PageProgress.page(rows, total_pages=, total_rows=)` in each connector's pager logs page n[/total] · rows · elapsed · `~left` — the estimate only when the API reports a total (InsightVM `page.totalPages`/`totalResources`, ServiceNow's `X-Total-Count` header via `ServiceNow._last_total`; Graph and Axonius give none).
- **Catalog generation**: `CatalogGenerator` logs the plan (`_log_plan`), `[n/total]` per table (profile call, columns/sample rows, which LLM), the draft (`_progress`: elapsed · ~left), the link pass and the merge (each dropped item); `plan_specs` logs catalog discovery; `refresh_catalog` logs the target file and whether it was written. Every LLM call (`llm._log_call`, both `ClaudeLLM` and `_ChatCompletionsLLM`) logs model → output model, time and in/out tokens (prompt size at DEBUG). Never raises.
- **Query-language sources**: `SQLDatabase` logs the compiled SQL (bound params at DEBUG), `DataExplorer` the KQL/command, `LakehouseConnection` the DuckDB scan; the semantic executor logs pushed vs. residual per source.
- **Never raises**: `log_http` and `PageProgress.page` swallow their own formatting errors (logged at DEBUG) — a malformed total or an odd response object must not fail the request being described.
- **Secrets**: never logged. `Authorization`-style headers aren't printed at all; URL query params and JSON/form body fields whose name matches `_SENSITIVE` (password, secret, token, api key, auth...) become `***`. `tests/test_verbose.py` asserts this at DEBUG level.

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

`DuckAPI.list_tables(kind=None, details=False)` returns a DataFrame (`name`, `kind`, `usage`, `pushdown`, `source`, `endpoint`, `description`; `details=True` adds `streaming`, `signature`) for whatever's currently registered, ordered by kind then name. `kind` (`duckduck/kinds.py`) exists because not everything registered is a plain table (and catalog generation relies on it: see "What gets drafted"): `table` (selectable as-is), `table function` (has required parameters — inferred from the signature), `catalog` and `raw query` (can't be inferred: connectors mark those methods with the `@catalog` / `@raw_query` decorators, which set `__duckduck_kind__` on the function). `usage` is an example query with required params as `'<placeholders>'` (unquoted for `int`/`float` annotations); `pushdown` summarizes the signature per the operator → parameter convention (`where` → "any column"; `query`/`filter` raw-fragment params and params with a non-None default are left out). Handy after `auto_register()`, which can add many tables in one call. `sql()` intercepts `SHOW TABLES` / `LIST TABLES` (optionally `ALL`, case-insensitive, optional trailing `;`) as a shortcut for it, matched via `DuckAPI._LIST_TABLES_RE` **before** the regular push-down/rewrite path runs — so it never touches functions named literally `tables`, `show`, etc. (those still resolve as normal `FROM tables` queries).

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
| Catalog / raw-query methods | Decorate a method that lists what the source contains with `@catalog`, and one that runs the source's own query language with `@raw_query` (`duckduck.kinds`) — `list_tables()` can't infer those |
| LIKE / comparison push-down | Add `col_ilike` / `col_like` / `col_gt`... params (see "Operator → parameter convention") and translate with `duckduck.pushdown.require_like`; only declare `col_like` if the server matches case-sensitively |
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

### `sql()` reads streaming tables page by page (`stream_pages`)

`DuckAPI(stream_pages=True)` (default): in `sql()`, a function whose kwargs carry no `limit` (not pushable) and that has a streaming function registered goes through `_materialize_pages` instead of `_materialize` (`_pages_instead`). The conditions are mapped against the *iter function's* signature (`allow_limit=False`, logged as `fn (page by page)`); each page is registered as a view and `SELECT <cols> FROM page WHERE conditions_to_sql(...)` keeps only matching rows — the conditions qualified to this table or unqualified — and only the referenced columns (every column when the query has a `*`); kept frames are concatenated and registered as usual. Early stop (generator closed) only for a single-table, `limit_safe`, `complete` query with a LIMIT, once every condition column was in the page. Logs `kept X of Y rows from N page(s), C column(s)`. The SQL console shares it. Tests: `tests/test_sql_pages.py`.

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

`table_name`/`sql` are structural (required, no default); `limit` on `table()` is pushed down server-side via SQLAlchemy's `.limit()` (translates to `TOP`/`LIMIT`/`FETCH` per dialect); `limit` on `query()` is applied client-side after the raw query runs. `table()` takes the `where` push-down param: every simple WHERE condition (`=`, `LIKE`, `ILIKE`, comparisons) becomes a real SQL `WHERE` in the database's dialect, matched to columns case-insensitively. `query()` pushes nothing (it's a raw passthrough). `from_secret` accepts either a full `connection_string` or discrete `drivername`/`username`/`password`/`host`/`port`/`database` fields (the latter matches what managed secrets, e.g. AWS RDS, already store).

**Sourcing the connection string from a secret manager**: if the secret already has a `connection_string` key, `authentication` needs nothing else — `"authentication": {"type": "aws", "secret_id": "prod/sqlserver"}` is enough, since the fetched secret becomes the credentials dict as-is. A managed secret with discrete fields (RDS-style: `username`/`password`/`host`/`port`/`dbname`) needs `drivername` added (RDS never stores it) and any mismatched key renamed via `"$secret.<key>"` — e.g. `"database": "$secret.dbname"` bridges RDS's `dbname` to the `database` field `from_secret` reads. See `duckduck.example.json`'s `sqlserver_from_rds_secret` entry for the full example.

### ServiceNow connector (`connector: "servicenow"`)

`duckduck/servicenow.py` — auth against the ServiceNow Table API (`/api/now/table/{tableName}`), either Basic (`ServiceNow(...)`) or OAuth2 client-credentials (`ServiceNow.from_oauth2(...)`). Like `database`, has a generic escape hatch (`table(table_name=..., query=None, limit=None)`, structural `table_name`) plus dedicated methods with real push-down for the tables people actually query most: `incidents`, `problems`, `change_requests`, `users` (`sys_user`), `cmdb_ci`. Each dedicated method's kwargs (`state`, `priority`, `assigned_to`, `type`, `active`, `sys_class_name`, ...) are joined with `^` into ServiceNow's own encoded `sysparm_query` syntax via `_build_query` — `^` is AND, `^OR` is OR, `^NQ` groups OR'd conditions (pass a raw string via `table(query=...)` for anything beyond simple equality). Pagination is `sysparm_limit`/`sysparm_offset`, incremented by `_iter_pages` until a page comes back shorter than the page size — no `page`/`totalPages` or `@odata.nextLink` here.

**Auth modes** — `from_secret` auto-detects which one from the secret's keys:

- Basic: `username`/`password` plus **either** `instance` (e.g. `"dev12345"` for `dev12345.service-now.com` — no domain) **or** `host` (a full custom domain, used as-is instead of appending `.service-now.com`).
- OAuth2 (`client_id`+`client_secret`+`token_url` present): common in enterprise deployments where an external IdP issues the token (e.g. Azure AD) and/or the data API sits behind a gateway that isn't `*.service-now.com` at all. `token_url` (the OAuth2 token endpoint — Azure AD's, ServiceNow's own `/oauth_token.do`, or a gateway's) and the data API's base URL (`instance`/`host`, or `api_base` for a gateway using its own path convention, e.g. `/v1/now` instead of `/api/now`) are fully independent — one doesn't imply the other. `resource` (Azure AD's audience param) and `scope` are optional form fields on the token request. `_ensure_token` caches the bearer token and refreshes it 60s before `expires_in` runs out, checked in `_get` before every request — same pattern as `SharePoint._get_token`'s MSAL silent-refresh.
- **Any-field push-down**: every table method (`table()` included — the generic one, where no field list is known up front) takes `where`; `_condition_clause` turns each condition into an encoded-query clause (`=`, LIKE/STARTSWITH/ENDSWITH for translatable LIKE/ILIKE, `>`/`>=`/`<`/`<=` for numbers) ANDed onto the method's own filters or the raw `query`. `pushdown_blocker` refuses what could lose rows: `<field>_link`/`<field>_value` columns (json_normalize's flattening of reference fields — not real field names), non-numeric comparisons (dates are read in the session user's timezone), untranslatable LIKE, values containing `^`. A raw `query` containing `^NQ` can't be safely ANDed onto, so `_with_where` leaves everything to DuckDB and `_fetch` drops the limit (the same fallback covers direct callers that bypass DuckAPI's blocker check).
- Not yet implemented: retry/backoff on transient failures, and `sys_id`-keyset pagination (an alternative to offset pagination for very large tables, where deep offsets get slow) — both real needs surfaced by comparing this wrapper against an existing internal client, not yet built.

### Axonius connector (`connector: "axonius"`)

`duckduck/axonius.py` — `api-key`/`api-secret` header auth against Axonius's POST-based REST API v2 (`/api/devices`, `/api/users`), which takes a JSON body shaped `{"meta": null, "data": {"type": "entity_request_schema", "attributes": {"page": {"offset", "limit"}, "filter": "<AQL>"}}}`. Column filters (`hostname`, `os_type`, `username`) are translated into AQL fragments and ANDed together via `_build_aql`; pass raw AQL via `devices(filter=...)`/`users(filter=...)` for anything more advanced. Pagination increments `offset` by `default_page_size` until a page comes back short, same shape as ServiceNow/InsightVM. `from_secret` expects `instance`, `api_key`, `api_secret`.

**Caveat**: this session's network policy blocked fetching `docs.axonius.com` while building this wrapper, so the request/response shape is based on published examples (the header auth and `entity_request_schema` body are confirmed) rather than a full read of the reference docs. `_normalize_assets`'s assumed `{"id", "type", "attributes"}` response envelope and the AQL syntax in the convenience filters are the parts most likely to need adjusting against a real instance's own API docs (usually `https://<instance>/api-docs`).

### Public APIs: `nvd` and `restcountries`

From [public-apis](https://github.com/public-apis/public-apis); `nvd` has `requires_authentication=False` in `SERVICE_REGISTRY` (optional key), `restcountries` needs one. Built from the published docs — the session that wrote them had `services.nvd.nist.gov` / `restcountries.com` blocked by network policy, so `tests/test_public_apis.py` replays the documented response shapes (`test_live_*` call the real APIs with `DUCKDUCK_LIVE=1`).

- **`nvd`** (`duckduck/nvd.py` — `NVD`): table `cves` (+ `iter_cves`), CVE API 2.0. Params follow the operator convention: `id`→`cveId`, `severity`→`cvssV3Severity` (the `severity` column is CVSS v3.x, Primary metric first — NVD matches any v3 metric, a superset), `cvss4_severity`→`cvssV4Severity`, `cwe`→`cweId` (column = first CWE), `in_kev=True`→`hasKev` flag (False not pushable), `published_*`/`last_modified_*` comparisons → `pubStartDate`/`pubEndDate` / `lastMod…` (`_range`: `>` as `>=`, `<` as `<=`; `_windows` cuts into ≤120-day windows, newest first, open end = now, open start = end − 120d; both ranges → every pair), `keyword` = NVD `keywordSearch` (words, never mapped from LIKE: not a superset). Flags are sent without `=` (`_get` builds the query itself). `noRejected` unless `include_rejected`. Pagination `startIndex`/`resultsPerPage` (≤2000) to `totalResults`; `limit` → one request of `limit` rows. Rate limit: `request_interval` (6 s keyless / 0.6 s with `api_key` → `apiKey` header) between requests (`_pause`, injectable `sleep`), 403/429/5xx retried `max_retries` times with doubling backoff (the backoff replaces the pause).
- **`restcountries`** (`duckduck/restcountries.py` — `RestCountries`): table `countries`, **v5** (`api.restcountries.com/countries/v5`, `Authorization: Bearer <api_key>`, required — `requires_authentication` in the registry; the keyless v3.1 was retired and 301s to a `legacy.json` notice, which once came back as a bogus one-row table: `_get` now raises `RestCountriesError` for the retired host, any ≥400 (with the body) and anything that isn't `data.objects`; 404 → no rows). Written from the project's README (endpoints, envelope, the Canada sample) — the field reference at restcountries.com/docs was unreachable, so `_row` reads each field in every plausible shape (`names`/`codes`/`capitals[primary]`/`memberships`/`leaders`/`flag.emoji` as documented; languages/borders/timezones/area/coordinates via `_joined`/`_area`/`_coordinates`) and leaves missing ones empty. One read per call (`_read`: `/code/{c}` > `/names.common/{n}` > `/capitals/{c}` > `/subregion/{s}` > `?region=` > all), then every param taken is applied exactly (case-sensitive `=`, `name_ilike` by pattern kind, lower-cased), sorted by name, `head(limit)`. Paging `limit`/`offset` (`_iter_pages`) stops when a page brings no new country (`_key`: uuid/alpha_3), a short page, a reported total, or `MAX_PAGES` — an API ignoring `offset` can't loop.

### Lakehouse connectors: `glue` (S3) and `blob_storage` (Azure)

`duckduck/lakehouse.py` — `LakehouseConnection` — is shared scanning plumbing used by both. Unlike every other wrapper, these don't fetch data into Python at all: they build a `read_parquet`/`delta_scan`/`iceberg_scan` expression and run it through a **private DuckDB connection** (`INSTALL`/`LOAD` the `httpfs`/`delta`/`iceberg`/`azure` extensions, `CREATE SECRET` for auth), so the actual scan is native DuckDB reading S3/Blob directly — only the final result crosses into a `pd.DataFrame`, same contract as every other wrapper from DuckAPI's point of view. `INSTALL` needs outbound internet the first time each extension is used (downloaded from DuckDB's own extension repository and cached under `~/.duckdb/extensions`).

- **`glue`** (`duckduck/glue.py` — `GlueTable`): `table(database=, table_name=)` (both structural) looks up the table's location + format in the **AWS Glue Data Catalog** via `boto3`'s `get_table`, then auto-detects Parquet vs Delta (`Parameters.table_type == "DELTA"` or `spark.sql.sources.provider == "delta"`) vs Iceberg (`Parameters.table_type == "ICEBERG"` or a `metadata_location` parameter — the Spark/Athena/PyIceberg Glue-catalog convention) and scans accordingly. `path(s3_path=, format=)` bypasses Glue for ad hoc reads. S3 auth is a `CREATE SECRET` built either from explicit `aws_access_key_id`/`aws_secret_access_key` or `PROVIDER credential_chain` (+ `PROFILE`/`REGION`) — the same credentials boto3's own Glue lookup uses, kept in sync deliberately.
- **`blob_storage`** (`duckduck/blob_storage.py` — `BlobStorage`): `table(container=, path=, format="parquet")` (both structural) reads `az://{container}/{path}` directly; `format` also accepts `csv`/`json`/`delta`/`iceberg`. Auth is `CREATE SECRET (TYPE AZURE, ...)`, either `CONNECTION_STRING` or `PROVIDER CREDENTIAL_CHAIN` + `ACCOUNT_NAME` (Azure CLI login / managed identity — no Glue-Data-Catalog equivalent here, so there's no format auto-detection, just the `format` parameter).

Both take the `where` push-down param: the query's simple WHERE conditions go *into* the DuckDB scan (`LakehouseConnection.scan` renders them with `conditions_to_sql` after a metadata-only `DESCRIBE` to skip columns the scan doesn't have), so Parquet row-group/file pruning happens before anything reaches Python. `iter_table()` on both is a post-hoc chunk split of the full scanned result (same trade-off as `SQLDatabase.query()`'s client-side `limit`), not true incremental streaming.

### Azure Data Explorer connector (`connector: "adx"`)

`duckduck/adx.py` — `DataExplorer`, on Microsoft's official `azure-kusto-data` SDK (`pip install "duckduck[adx]"`). Tables: `{name}_table(table_name=...)` (structural `table_name`, takes the `where` push-down param), `{name}_query(kql=...)` (KQL passthrough, `limit` appended as `| take n`), `{name}_tables` (`.show tables details`, falling back to `.show tables` without the permission for details) and `{name}_columns(table_name=...)` (`getschema`).

- **Push-down → KQL**: `=` → `==`; LIKE → `contains_cs`/`startswith_cs`/`endswith_cs` (case-sensitive, like SQL LIKE), ILIKE → `contains`/`startswith`/`endswith`/`=~`; any other LIKE pattern → `matches regex` with `%`→`.*`, `_`→`.` (exact, never approximated — `where` consumes every condition, so an untranslated one would make LIMIT unsafe); comparisons as-is; LIMIT → `| take n`.
- **Typing via schema**: `getschema` (cached per table) maps DuckAPI's lowercased column names back to ADX's real case and gives each column's type; every value becomes an escaped string literal wrapped in the column's converter (`todatetime("...")`, `tolong("...")`), so `WHERE Timestamp >= '2026-09-23'` compares as datetime on the cluster. Conditions on unknown columns are skipped.
- **Injection**: values are always `kql_string` literals (`\\`, `\"`, `\n`, `\r`, `\t` escaped; other control characters rejected); table names must match `_TABLE_NAME_RE` and are bracket-quoted; queries go through `execute_query` (never `execute`, which routes anything starting with `.` to control commands), and `query()` refuses control commands outright. Query parameters (`declare query_parameters`) were deliberately *not* used: their string-value format couldn't be verified while building this (docs host blocked), and escaped literals are unambiguous.
- **Auth** (`from_secret` picks by keys present): `client_id` + `client_secret` + `tenant_id` → app registration (`with_aad_application_key_authentication`); otherwise `DefaultAzureCredential` (managed identity, `az login`, `AZURE_*` env vars), optionally pinned by `tenant_id` (`with_azure_token_credential`). `cluster` accepts `mycluster.region` or a full URL.
- **Truncation**: ADX fails queries over its default result limits; `notruncation=True` sets the `notruncation` request option. `iter_table` is a post-hoc chunk split (ADX returns the filtered result in one response).

### Local autoloading: `files` and `python` connectors

For running with no external system at all — development, demos, synthetic data — two connectors plug into the same `auto_register()` / `duckduck.json` flow (`examples/local/duckduck.local.json`, `examples/semantic/duckduck.local.json`):

- **`files`** (`duckduck/local_files.py` — `LocalFiles`): `path` is a directory; every top-level `.csv`/`.tsv`/`.parquet`/`.json`/`.jsonl`/`.ndjson` file becomes a table (`table_name_for` sanitizes the name), and every top-level sub-directory becomes one table over all files of its dominant format (Hive `key=value` partitions become columns). Names starting with `.`/`_` are skipped. Each table is a `FileTable` callable taking `where`/`limit`, scanned through `LakehouseConnection` (conditions inside the DuckDB scan). Also registers `tables` and `columns`.
- **`python`** (`duckduck/python_source.py` — `PythonSource`): `module` is a `.py` path or dotted module; its `tables(**kwargs)` factory (or `factory=` name, or a `TABLES` dict) returns `{table: callable}`, each a normal DuckAPI function — so column-filter/`*_ilike`/`where`/`limit` params get real push-down, and the same SQL keeps working when the config later points at the real connector. Files load under `duckduck.python_source.<stem>` (labelled "Python module" by `list_tables()`), never into `sys.modules`.

`auto_register` support for them (generic, in `ServiceSpec`): `dynamic_tables` (method returning `{table: callable}` — tables known only at runtime), `requires_authentication=False` (the block becomes optional), `path_options` (relative paths in a JSON config resolve against the config file's directory via `DuckAPI._config_base_dir`; given in code, against the current directory — a path that doesn't exist fails *before* the connector is built, with the original value, the resolved path and which folder it was resolved against). Any service may set `table_prefix` (`""` → bare table names); two services producing the same table name in one `auto_register()` call raise instead of silently overwriting.

### Listing what exists: `tables` / `columns`

Both `glue` and `adx` register discovery tables, queryable like any other (filters push down):

```sql
SELECT * FROM glue_tables WHERE database = 'security' AND table_name LIKE 'proxy%'
SELECT * FROM glue_columns(database='security', table_name='proxy_logs')
SELECT * FROM glue_databases
SELECT * FROM adx_tables WHERE folder = 'network'
SELECT * FROM adx_columns(table_name='ProxyLogs')
```

`GlueTable.tables()` returns database, table_name, detected `format` (`_detect_format`: parquet / delta / iceberg — the same rule `table()` scans by), `table_type`, S3 `location`, `partition_keys`, `column_count`, description, owner and timestamps; `database` omitted → every database in the catalog. `GlueTable.columns()` includes partition keys (`partition_key=True`).

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
| `connector: "adx"` | `azure-kusto-data>=4.0`, `azure-identity>=1.15` (`pip install "duckduck[adx]"`) |
| `connector: "blob_storage"` | none as a Python package — DuckDB's own `azure`/`delta`/`iceberg` extensions auto-install on first use (needs outbound internet) |
| `authentication.type: "local"` | none — fully offline |
| `connector: "files"` / `"python"` | none — fully offline (DuckDB reads CSV/Parquet/JSON natively) |

---

## Semantic search (`duckduck/semantic/`)

Natural-language questions → a validated logical plan → SQL executed on the DuckAPI virtualization layer above. Needs `pip install -e ".[semantic]"` (pydantic, PyYAML); never imported by `duckduck/__init__.py`, so the base package doesn't need them. Architecture follows `semantic_search_jev_architecture.md` (Phases 1–2: catalog, decision engine, interpreter, relationship graph, planner, validator, DuckDB execution).

```
question ─► RuleBasedExtractor (values: time, IPs, domains, enums, free text)
         ─► LexicalRetriever (top-N sources, BM25)
         ─► SemanticInterpreter (independent decisions: entity, activity, value types, source relevance)
         ─► QueryPlanner (+ RelationshipGraph paths, field/relationship confirmations)
         ─► QueryValidator ─► compiler (per-source CTEs) ─► PlanExecutor (per-source push-down) ─► DataFrame
```

| Module | Role |
|---|---|
| `catalog.py` | Pydantic models + YAML/JSON loader; cross-reference checks run at load time. Sources bind to a DuckAPI table (`table:` + `args:`) or a native DuckDB `relation:` |
| `decisions.py` | `DecisionEngine` protocol; `JEVAdapter` (wraps a `JEVBackend` with retries/timeout/normalization/logging); `LexicalDecisionEngine` (offline baseline) |
| `extraction.py` | Deterministic value extraction — the decision engine never generates text |
| `interpreter.py` | NL → `SemanticIntent`, every judgment recorded as a `DecisionRecord` with probability + threshold |
| `graph.py` | Joinable relationships as edges; Dijkstra over `-log(confidence)` + hop penalty |
| `planner.py` | Picks the primary source, types filters to fields, joins in whatever the question needs from other sources |
| `plan.py` / `validator.py` | Structural (Pydantic) + semantic (catalog) validation, authorization via `allowed_sources` |
| `compiler.py` / `executor.py` | SQL generation; per-source fetch with push-down |
| `engine.py` | `SemanticSearch` — the pipeline; `search()` / `plan()` → `SearchResult` |
| `evaluation.py` | Per-stage accuracy over a labeled dataset; threshold calibration |

### The three decisions: Systems / About / Answer

Every question is answered by deciding three things. The web app's chips show them, and every change must keep the chips, the pipeline and the pins describing the same three:

| Chip | Question it answers | Internally |
|---|---|---|
| **Systems** | Where to look? | source relevance (retrieval + one yes/no per candidate table), the planner's primary table, the joins (`_preview_joins`) |
| **About** | What is it about? | `intent.target_entity` (user, host…), or the field whose values the answer lists (`values_field`) |
| **Answer** | What kind of answer? | `intent.answer_shape` (`shapes.ANSWER_SHAPES`) |

Order in `interpret` → `plan`:
1. **Answer first only when the wording can't be wrong.** `small_talk` (nothing but a greeting), `browse` (names a table, unless the wording asks *about* it — "what columns does the alerts table have?") and a named field's `values` are decided before any engine call. **What the question is about is never the wording's call** (below).
2. **Subject + About + Systems in one engine batch**: the `subject` choice (data / this assistant / neither), entity, activity and source relevance. The entity is skipped when a named, non-entity field decides the answer.
3. **Planner**: primary table, entity/values field, joins, filters. Then validator → compiler (the only SQL writer) → executor.

Any decision under its threshold asks back instead of executing.

**The preview is the same decisions.** `SemanticSearch.preview` is step 2 run while typing: rule-based extraction, no LLM, nothing executed or recorded. A choice in a chip travels with the question as a pin and is taken as given, never asked again:

| Chip choice | Sent as | Pin |
|---|---|---|
| tables | `only_sources` | scope |
| entity | `entity` | `entity` |
| field | `values_field` | `values_field` + `source:<t>` |
| unticked join | `blocked_joins` | `join:<a>=<b>` False |

A new decision the user should see or override belongs in one of the three chips, with its own pin. All readers (below) feed these same three decisions — the `llm` reader only adds evidence and candidates and the engine still decides; `llm_decides` makes the LLM the engine. Keep the preview's and the search's rules shared (same helpers) so the chips predict the answer. They can still differ: the real search may use LLM extraction/translation and the preview doesn't, and the user's pinned choices win.

**Subject: the data, this assistant, or neither** (`SemanticInterpreter._subject_ask` / `_apply_subject` / `_subject_verdict`, `SUBJECT_OPTIONS`): one choice in the batch — it replaced the deterministic catalog rule (which read "…in the CISA KEV catalog" as a question about this assistant) and the `in_scope` yes/no. The wording rules (`_wording` = `shapes.candidates` with catalog; `_candidates` = the answer kinds *about the data*, catalog stripped) and `_data_signals` (time window, known values, typed values, entity words) are facts (`wording_about_this_assistant`, `points_at_the_data`, `tables_hold`); for the offline lexical engine they're the `default` (strong catalog wording → assistant; "could be either" wording → none, so it asks back; nothing at all → other; else data). Verdict: P(other) > 1 − `thresholds.out_of_scope` → out_of_scope reply; assistant/data ≥ `thresholds.subject` (0.6) → that; else P(assistant) ≥ `thresholds.subject_doubt` (0.25) → the about-me `answer_shape` follow-up (catalog / list); else data. Pins: `subject`, and implied by `answer_shape: catalog` (assistant), any other `answer_shape` or `in_scope: True` (data). The preview asks the same question in its batch. Every Jev fake in the tests answers `subject` (data unless the test is about it).

**Hypotheses: judged plans before asking back** (`hypotheses.py`, `SemanticSearch._explore`, `semantic.hypotheses` → `HypothesesConfig`, `SemanticSearch(hypotheses=True|dict|False)`): when `_search` ends in `needs_clarification` and the engine isn't the lexical one (`HypothesisJudge.active`), each option's pins is a reading — `readings()` plans them with `_search(execute=False)` on a thread pool (`submit_in_context`, so scope/engine/metering follow), expanding a reading that asks again up to `depth` levels and `max_hypotheses` total; `judge()` describes each plan in plain words (`describe`: shape head, fields with their descriptions, sources, filters, time range, joins; SQL as a fact) and asks **one batch**: a `reading` choice among them + a `check:<h>` yes/no per plan (`CRITERIA`). Winner = top choice ≥ `threshold` (0.6), leading by `margin` (0.15), check ≥ `check` (0.5) → re-run with its pins (executed), a `reading` DecisionRecord first (engine, alternatives with probabilities), the records its pins produced relabelled `decided_by="hypothesis"`, `result.pinned` = the user's pins only, `result.hypotheses` = every reading (label, pins, description, sql, probability, answers). Not sure → the original clarification, with the `reading` record appended and `hypotheses` filled. Page: "Readings considered" on the answer. Tests: `tests/test_hypotheses.py` (a barrier proves the readings plan concurrently).

**Decided but not shown as chips:**
- filters: enum values, typed literals (IP, email), time range. They appear in the SQL and in "How it was decided".
- the activity.
- **About is empty** for `lookup`/`locate` (the subject is the value, e.g. `10.0.0.196`) and `browse` (the subject is the table). Filling it there ("About: 10.0.0.196 (ip address)", "About: table owners") was proposed to the user and isn't built yet.

### Readers: `rules`, `llm`, `llm_decides` — and `auto`, which picks one

All three readers produce the same three decisions. What differs is who proposes them — and, for `llm_decides`, who decides.

- **`rules`** (`SemanticSearch(reader="rules")`, the default, `semantic.reader`): the configured `extractor`; wording rules propose; Jev settles ambiguity.
- **`llm`**: `interpreter.llm_reader` is an `LLMExtractor`, called with `extract(..., reading=True)`.
  - **The call.** One LLM call returns `LLMExtractionWithReading.reading` (`READING_INSTRUCTIONS`, prompt adds "Tables and their fields"). `LLMExtractor._reading` checks every name against the catalog (answer ∈ `ANSWER_SHAPES` minus `NOT_READ` = small_talk/out_of_scope — whether a question is about the data is the engine's `in_scope` call, and an LLM's "not about the data" fed to it as a fact would mislead it ("what are my departments"); the prompt also says "my/our/I have" mean this data; entity / `source.field` via `_field_ref`, which resolves a unique bare field name / table / a value present in the question) → `extraction.Reading`.
  - **The cache.** `extract` caches per (normalized question, reading) for `cache_ttl` (300s), fallbacks excluded, so the preview and the search share one call.
  - **How the reading is used.** Evidence, never a decision:
    - `intent.reading`; `intent.decision_state()` adds `facts.llm_reading` to every engine question;
    - `_with_reading` appends the reading's answer to the rules' candidates when they differ, so the engine chooses and a doubt asks back (the rules' `catalog` stays final; `browse` only with a table);
    - `_apply_shape` handles an engine-chosen catalog/browse (`interpret` returns early);
    - `_reading_defaults` gives the offline lexical engine the reading as its default;
    - `_reading_sources` adds its tables to retrieval;
    - `_values_of_named_field` accepts its non-entity field (entity not asked);
    - planner `_reading_field`: preferred primary for `FIELD_SHAPES`, `default` for the `values_field` classify, and it keeps "the different users" from short-circuiting when a field was read;
    - an `llm_reading` DecisionRecord (`decided_by="llm"`) goes first in the trail ("no reading" when the LLM failed; the extraction then falls back to rules with a warning).
- **`llm_decides`** (page: 🪽 Fly): the `llm` reading **and** the LLM as the decision engine. `SemanticSearch.llm_engine` = `JEVAdapter(LLMDecisionBackend(llm_reader.llm))` (or `llm_engine=`); `engine_for(reader)` picks it, and `search`/`preview` run inside `decisions.using_engine(...)`, a `ContextVar` read by the `engine` properties of the interpreter and planner (`engine_in_use`), so concurrent web requests never share an override; `Conversation._interpret` classifies replies with `engine_for(self.reader)`. `interpreter.LLM_READERS` = both LLM readers. Same invariants: thresholds, asking back, compiler-only SQL.
- **Metering** (`metering.py`): `search()` and `preview()` run in `metered()` (a `ContextVar` `Usage`); `JEVAdapter._call` → `record_engine` (calls, questions, seconds), `JevClient._log_usage` → `record_cost` (`usage.cost`), `llm._log_call` → `record_llm` (tokens, seconds) — every real client; fakes in tests call `record_llm` themselves. Thread pools submit with `submit_in_context` (engine calls, evidence probes). A cached LLM reading → `record_reuse` (`usage.reused`), never a second call: the preview that made it counted it. `SearchResult.reader` / `.usage` (in `to_dict`).
- **Stored and compared**: `searches.reader` / `searches.usage` (JSON; `ALTER TABLE … ADD COLUMN IF NOT EXISTS` migrates older files, old rows read as `rules`), `engine` = `engine_label_for(reader)`; `previews` table (`record_preview`, only when a preview called something). `store.searches(reader=)` adds reader/engine/elapsed + `engine_calls`/`llm_calls`/`llm_tokens`/`cost` (`_USAGE_COLUMNS`); `stats()["by_reader"]` = per mode: searches, rated, answer rate, asked-back rate, median ms, average calls/tokens, reported cost, plus `previews`/`typing_*` from the previews table. `evaluation.evaluate(reader=)`, `compare_readers(search, cases, readers)`; `EvaluationReport.metrics` adds asked_back_rate / mean calls / tokens / reported_cost. Server: `/api/searches?reader=`, `/api/stats.by_reader`, `/api/evaluate {readers}` → `readers: {mode: metrics}`. Page: 🪽 Fly in the switch (+ help column), `runPill` on answers, History Mode column + filter, Dashboard "Modes compared" (`modesTable`, fixed order Paddle/Dive/Fly), Evaluate "also compare the modes". Export: mode + engine per gap, `--reader` in the reproduce command. Tests: `tests/test_modes.py`, `tests/test_readers.py`.
- **`auto`** (page: 🧭 Auto; `router.py` — `ModeRouter`): not a reader, a choice of one per question. `SemanticSearch.route(q)` (template from `_preview_rules` literals + `question_template`) → `router.route(self.engine, q, template, readers minus auto)`: `store.runs()` (one row per conversation — `coalesce(conversation_id, id)`, ordered by `created_at` then `rowid`; first round's question/template/reader, last round's status, latest verdict; `success` 1, 0 for `not_answered`/`invalid_plan`, 0.5 for `partial` — **no negative feedback = positive**), cached per `store.version`; `similar()` cosine over content stems (template/question/English, best), ≥ `min_similarity`, top `max_runs`; `evidence()` per mode (weighted runs/successes, smoothed `(ok+1)/(runs+2)`, median ms, avg calls, asked-back rate, examples); one `engine.classify` (always the configured engine — Jev, even when it could pick the LLM's modes) with `facts.history_by_mode` + `note`, `default` = best smoothed rate (offline engine only). **Ties go to the cheaper mode** (`cheapest_of_tied`, `MODES` order rules < llm < llm_decides): modes within `tie_margin` (0.05) of the engine's top probability — and, for the offline default, of the best success rate — are tied; the record's probability is the chosen mode's and the subject says "tied … the cheaper". Below `Thresholds.router` (0.5), unknown choice, or an exception → `fallback` (`RouterConfig`: `min_similarity`, `max_runs`, `fallback`, `tie_margin`; `semantic.router`), never asked back. One mode available → deterministic. `search()` routes inside `metered()` (the route call is counted), prepends the `route` DecisionRecord, sets `result.route` {reader, evidence, similar[:5]}, `result.reader` = chosen, `result.requested_reader` = `auto` (stored: `searches.requested_reader`; `stats()["auto"]` = per chosen mode: searches, rated, answered, answer rate). `preview` routes too (cache keyed by the requested reader) → `seen["route"]` {reader, probability, why, evidence}. **One pick per question**: `SemanticSearch.route` caches the `Route` per normalized question for `route_ttl` (300s) in `_routes`, so the preview's "Auto picked" chip is the mode the search runs (before, each routed on its own and a stale preview could say Paddle while the search ran Fly); a cached auto preview whose route expired is recomputed; `evaluation._measuring` swaps `_routes` out and back. `Conversation` keeps `requested_reader` and, after the first round, the chosen `reader`. `evaluation._measuring` sets `router.store = None` (no leakage). Page: `READER='auto'`, "Auto picked" chip, History 🧭→, Dashboard "🧭 Auto's picks". Tests: `tests/test_router.py`.
- **Plumbing.** `from_config` builds the llm reader with `cfg.build_llm_reader`:
  - it reuses an `llm` extractor, else builds one on the extractor stage's LLM, else `None`;
  - a build failure only disables the reader (`search.llm_reader_error`) unless `reader: llm`.
- **Where to pick it.**
  - `SemanticSearch.readers`; `_check_reader` refuses unknown / unavailable readers;
  - `search`/`conversation`/`preview(reader=)` (the preview cache is keyed per reader); `Conversation.reader` holds every round;
  - server: `/api/preview` and `/api/ask` take `reader`; `/api/meta.readers` = {available, default, llm_unavailable};
  - CLI: `ask --reader` (`rules`/`llm`/`llm_decides`/`auto`).
- **The page.** A 🦆 Paddle (`rules`) | 🤿 Dive (`llm`) switch (`READER`, `READER_NAMES`, localStorage; only the page uses those names). While reading, the thinking chip rotates `QUIPS[READER]` (`startQuips`/`stopQuips`, every 2.2s; never opens with the last one) and Ask reads Paddling…/Diving…. The `?` (`#readerhelpbtn`) toggles `#readerhelp`: per mode how it reads / good at / limits (keep it in sync when a reader changes). Ask is disabled while the preview is pending or running (`busy`, `updateAsk`); Enter while busy sets `pendingSubmit`, which submits once the preview lands; a 60s abort keeps Ask from staying disabled. Tests: `tests/test_readers.py`.

### Invariants — don't break these

- **Nothing upstream writes SQL.** Only `compiler.py` emits SQL, only from a validated `LogicalQueryPlan`. Identifiers come from the catalog (pattern-constrained, and quoted anyway); values always go through `render_literal`.
- **Low confidence never executes.** Any decision under its `Thresholds` value raises `ClarificationNeeded` → `status="needs_clarification"`, with a `followup` (`intent.Clarification`: kind, question, `context`, options with `detail` — built by `clarify.ClarificationTexts` from templates (`DEFAULT_TEXTS`, overridable per key via `semantic.clarification_texts`, validated at load: known keys and placeholders only) filled with the catalog's descriptions (`short()` inside sentences, full first sentence in option labels; `source.field` names only in parentheses), never an LLM); each `ClarificationOption.pins` settles that decision when passed back as `search(question, pinned=...)` — the interpreter/planner take a pin as given (`decided_by="user"`, probability 1.0 or 0.0), never ask it again. `SemanticSearch.conversation()` → `Conversation` keeps the pins across `answer(reply)` calls (option number/value/label, else free text mapped by the engine's `classify`, kept open under `thresholds.reply`), `max_rounds`, `history`, `transcript`; `ask --interactive` drives it at the prompt. Pin keys: `entity`, `activity`, `value:<literal>`, `value_term`, `source:<name>`, `field:<ref>`, `join:<a>=<b>`; **A "no" routes around, it doesn't stop**: the planner collects `refused` fields (`field:<ref>` False) and `blocked` joins (`join:<a>=<b>` False → `RelationshipGraph.find_path(blocked=)`); `_choose_primary`/`_usable_fields` skip refused fields (a value whose only fields were refused → `_retry_value_type`: a `value_type` follow-up without that type), `_locate_enum` drops refused readings (none left → `ignore_term` follow-up; `term:<t>` = `"ignore"` drops the filter with a `value_filter` record by the user, `False` → unresolvable), `_find_entity_field` skips refused fields/blocked joins (none → `entity_reachable` follow-up listing only entities `_find_entity_field` can reach from the primary). Entity and source follow-ups end with a **none of these** option (`not_entity:<e>` / `source:<n>` False for every option shown); the interpreter drops ruled-out entities from the choice and refused sources from retrieval (fetching `top_k + refused` so the next ones come up). Join questions name the matched fields, so two alternatives between the same sources read differently. Nothing left to try → `kind="unresolvable"` (no options). That includes a value the interpreter can't type: it must *ask*, never silently drop the filter (that would answer a broader question than the one asked). `tests/test_semantic_search.py::test_untypeable_value_asks_instead_of_being_dropped` guards this.
- **Direct replies** (`tests/test_direct_replies.py`): `small_talk` / `out_of_scope` shapes. `SemanticInterpreter._small_talk` (after `_about_the_catalog`, before any ask) uses `AnswerShapes.small_talk_kind`: `DEFAULT_WORDING["small_talk"]` removed, leftover content stems minus `_SMALL_TALK_FILLER` must be empty → greeting/thanks/goodbye (`intent.small_talk`); `candidates()` ignores that wording. Otherwise `_in_scope_ask` adds an `in_scope` noul (`CRITERIA["in_scope"]`, subject `_scope_summary()`) to the same batch; the lexical engine reads `DecisionState.offline_prior` (0.05 when no terms/enums/time/typed literals/retrieval hit, else 0.9; never sent to Jev); `_out_of_scope` records it and, below `thresholds.out_of_scope`, sets `answer_shape="out_of_scope"`. Pin `in_scope: True` skips both (server: `/api/ask` `in_scope`). `SemanticSearch._reply` → `SearchResult.reply` (`reply.*` texts) + `suggestions` (`example_questions`, round-robin over source `examples`; only for greetings and out-of-scope), `topics()`; status ok.
- **Answer shapes are compiled, not computed elsewhere.** `shapes.ANSWER_SHAPES` (re-exported by `intent`): `list` / `count` / `values` / `count_values` / `count_by` / `lookup` / `locate` (`FIELD_SHAPES` = values, count_values, count_by; `ACROSS_SHAPES` = lookup, locate). Wording lives in `shapes.DEFAULT_WORDING` (`wording`; `count_by` also `maybe_wording`), extended per shape by `semantic.answer_shapes` (inline or a JSON/YAML path, `load_answer_shapes`; `replace`, `description`; validated at load) → `AnswerShapes`, shared by interpreter and planner. `SemanticInterpreter._candidates` adds `lookup` as an alternative to a wording-less `list` when the question carries a typed/shape value (the engine picks; `DecisionState.default="list"` is the lexical engine's no-evidence answer, never sent to Jev). `catalog` ("what kind of information do you have?") is settled first by `SemanticInterpreter._about_the_catalog` — before any other ask, so nothing else is decided or asked — with a topic (`shapes.catalog_topic` → `intent.catalog_topic`: systems > relationships > fields > activities > entities > tables; `systems` → `SemanticSearch._systems_answer`: per `service_of` system its `list_tables()` kind, registered/described table counts, examples; restricted to allowed tables when `allowed_sources` is set; taken-over tables left out). **About me or about the data**: `DEFAULT_WORDING["catalog"]["maybe_wording"]` (systems/sources/connectors… near connected/configured/available, EN+PT) → `candidates(q, about_me=)` returns `["catalog", "list"]` (the interpreter passes `about_me=False` when the question carries a typed value); the engine chooses, a doubt asks back with `answer_shape.context_about_me`; the offline in-scope rule counts it as in scope; `CRITERIA["in_scope"]` says the assistant's setup is in scope, answered by `SemanticSearch._catalog_answer` (allowed sources only; `fields` limited to tables named in the question — `_tables_named_in` — else all; `tables` = `_catalog_overview`; no data read). **Whose catalog**: the bare word counts only as *this* assistant's catalog after a determiner (`your/the/this/seu/no… [semantic] catalog`, not followed by *of/de*) — "the CISA KEV catalog" / "a product catalog" are the data's words; and `candidates(q, about_data=True)` (the interpreter passes it when the question carries a typed value, a time window or an enum match) drops catalog wording entirely (tests: `tests/test_cve_questions.py`). An LLM reading that disagrees with a catalog reading makes it the engine's choice too (`_with_reading` no longer keeps the rules' catalog final). Enumerated values are stored as the field's type (`QueryPlanner._stored`: `"true"`/`"yes"`… → bool on a boolean field, numbers on numeric ones — `in_kev: {values: {"true": [kev, known exploited]}}`). `AnswerShapes.candidates` combines it (catalog > locate > lookup > count_values > count_by > count(+maybe) > values > group-alone > list): one candidate → deterministic `answer_shape` record (none for a plain list); several → `_shape_ask` adds a choice to the interpreter's batch (`answer_shape` key), below `thresholds.answer_shape` → `answer_shape` follow-up; pin `answer_shape`. **browse** ("show me table owners"): `SemanticInterpreter._browse` (after small talk, before any ask; skipped when `answer_shape` is pinned to something else) → `shapes.browse_target(question, _table_names())` (source name / bound table / `_`→space; patterns: `table|tabela|dataset` + name, name + `table`, or a leading preview/browse/open/abra verb; `_ROW_COUNT` → `intent.row_limit`) → `intent.browse_source`; engine → `planner.plan_browse` (every field; enum `value_filters` of that source, typed literals on the first field of their type, time range if the source has a time field, `limit = row_limit or default_limit`); `preview` answers it without the engine. **Named field → values**: `SemanticInterpreter._candidates` turns a wording-less `list` into a deterministic `values` when `AnswerShapes.named_head` (openers `_HEAD_OPENERS`: what/which are the…, list/show the…, quais são as…, liste/mostre os…; phrase cut at `_HEAD_END`) names a field of an in-scope source (field-name stems ⊆ phrase stems) none of whose stems is an entity name/keyword (`_named_field`, `_entity_stems`) — "what are the severities of the events?" → severity's values; "list the owners" stays a list. Same for a looser `which/what <X> …` (`asked_head`) and for counts (`counted_head`: how many / number of / count / quantos → `count_values`), but there the field must be the phrase's head noun (`_field_at_head`), so "how many hosts have a rule?" still counts hosts. Pronouns (we, they…) are `STOPWORDS`, never values. For a `values`/`count_values` answer whose question names a non-entity field (`_values_of_named_field` → `_field_named_in`), `_entity_ask` returns None (no entity decision), `preview` returns `field` (`source.field`, entity None) + `field_options` (the relevant tables' non-time fields, named first; ≤25) → the page's *About* chip names the field and its panel picks another → `/api/ask` `values_field` (checked with `has_field`) → pins `values_field` + `source:<table>` True; `planner._choose_primary(preferred=)` ranks the pinned field's source first, then sources holding a named field for `FIELD_SHAPES`. `_apply_shape` runs before `_apply_entity`, which doesn't raise for `values`/`count_values` (the field decides). `extraction._SHAPE_WORDS` are consumed, never values or focus terms, and `_without_shape_words` trims matched wording (`AnswerShapes.matched_tokens`, so configured phrases too) off term literals. **lookup / locate** (`SemanticSearch._across`): `planner.plan_across` → one validated single-source plan per (value, table, field of the value's type) over every allowed source (enums/time range where the table has them; no time field + time range → skipped); lookup selects every column, locate counts; `SearchResult.sections` (`Section`: checked, matched_on, rows, sql, results) + `summary` (per table); `results` = `_rows_found` (lookup: every row, union by column name, `source` first) or the summary (locate); locate with no value → `_catalog_listing` (sources holding the target entity, or all), no data read; lookup with no value → list. `_apply_entity` and `_apply_sources` don't raise for these shapes. **Trivial list rule**: a `list` whose selected entity field is an `eq`-filtered field or equality-joined to one (`_equal_to` over the plan's paths) raises `TrivialAnswer` → the engine records a deterministic `answer_shape: lookup` and answers across; pin `answer_shape: list` to keep the list. Tests: `tests/test_lookup.py`. Planner `_shape_field`: pin `values_field` → a field the question names (field-name stems ⊆ question stems; `count_by` prefers the word after per/by — `_words_after`) → else, unless a non-row-level entity already makes the answer distinct (then `values`→list, `count_values`→count), `engine.classify` over the primary's fields, below `thresholds.field` → `values_field` follow-up (`values_field.*` / `count_by_field.*` texts; "none" pins `answer_shape: list`). Plan: `values` = select [f] distinct; `count_values` + `aggregate="count"`; `count_by` = `group_by=[f]`, `aggregate="count"` (entity distinct → `SELECT g, COUNT(*) FROM (SELECT DISTINCT g, e …) GROUP BY g`; records → `SELECT g, COUNT(*) … GROUP BY g`), ordered by count desc; `group_by` requires an aggregate. The executor never pushes LIMIT under an aggregate. Tests: `tests/test_answer_shapes.py`.
- **Streaming execution** (`PlanExecutor(stream=True)`, `SemanticSearch(stream=)`, `semantic.stream`): when a source's LIMIT wasn't pushed and `duck.streaming_function(table)` exists, `_stream` maps the conditions against the *generator's* signature, creates a typed temp table (`_DUCK_TYPES` from the catalog field types) with only the plan's columns, and for each page of `DuckAPI.fetch_pages` inserts `TRY_CAST`ed columns filtered by `source_conditions` (absent columns → NULL); a single-source plan without DISTINCT/aggregate/ORDER BY stops at `limit` rows (the generator is closed); a column no page had → `ExecutionError`; temp tables dropped after the query. `SourceFetch.streamed/pages/rows_scanned`. `DuckAPI.fetch()` stays for sources without a streaming function or with the limit pushed. Tests: `tests/test_streaming_execution.py`.
- **Execution does not go through `DuckAPI.sql()`.** Its push-down is global (one LIMIT/WHERE handed to every function in the query), wrong for joins. `PlanExecutor` calls `DuckAPI.fetch()` per source instead: `eq` filters → API kwargs when the function accepts them; `limit` only for a single-source, non-DISTINCT plan with **no residual filter and no ORDER BY** (an API's first N rows aren't the newest N). DuckDB re-applies every filter regardless.
- **Unavailable sources degrade, not crash.** A catalog source whose DuckAPI table isn't registered (e.g. its connector failed `auto_register(on_error="warn")`) is dropped with an always-shown `RuntimeWarning`; `strict=True` raises instead.

### Decision engines: lexical baseline, Jev, any chat model

`LexicalDecisionEngine` scores token overlap against catalog text (entity/activity keywords, source descriptions) and returns the caller's `prior` for structural confirmations (field/relationship relevance). It's what makes the MVP set in `examples/semantic/evaluation.json` pass offline, and it's deliberately crude — richer phrasing is what Jev is for. It's the engine whenever `semantic.decision_engine.ai_provider` is unset.

`decision_engine.ai_provider` names an `ai_providers` entry; `SemanticConfig.build_engine` wraps the backend in `JEVAdapter` (retries, timeout, normalization, `decide_many`) either way:

- **`api: "decisions"`** (`provider: "jev"`, or `provider: "openrouter"` + `api: "decisions"`, or any entry with `decisions_url`) → `jev.py`'s `JevClient`, a client for the typed **Decisions API** (`POST` `{model, state, questions}`, `Authorization: Bearer <key>`). TypeSafe's host is `{base_url}/api/v1/decisions` (key `JEV_API_KEY`); OpenRouter serves the same API at `JevClient.OPENROUTER_DECISIONS_URL` (`https://openrouter.ai/api/alpha/decisions`, model `typesafe/jev-1.13`, key `OPENROUTER_API_KEY` — `api_key_env`). `_post` accepts both envelopes: TypeSafe's `{code, data: {answers}}` and OpenRouter's top-level `{answers, usage}` (verified against the response OpenRouter's own Jev tutorial shows). yes/no → a `noul` question, pick-one → a `choice` question with option descriptions as `criteria`, and `decide_batch` sends several `noul` questions in one request so source relevance for every candidate is **one** round trip. The state sent is compact (`user_question`, `subject`/`candidates`, `facts`, `catalog_prior_probability`); lexical `terms` are never sent. `JevAPIError.retryable` distinguishes 429/5xx/network (retried) from bad key/bad request/unparseable answer (fails immediately). Answer parsing accepts a bare number or `{noul|probability}` for `noul`, and `{probabilities}` or `{choice, confidence}` for `choice`.
- **`api: "chat"`** → `llm_decisions.py`'s `LLMDecisionBackend`: the same `JEVBackend` contract over any `LLMClient`, the same compact state as JSON, answers through structured output only (`_YesNo`, `_Scores` — list-shaped for schema-friendliness); system prompt `DEFAULT_DECISION_PROMPT` or `decision_engine.system_prompt[_file]`; `decision_engine.timeout` defaults to 60s here (Jev: the entry's HTTP `timeout` + 5).

**Batching** (`decisions.Ask` + `ask_all`): the interpreter builds one batch per question — entity + activity (choice), the value-type choice for a lone untyped free-text literal (used only if the activity doesn't type it), and a relevance noul per retrieved source — then applies the answers in the old order (entity → activity → values → sources), so errors are the same; a second, small batch only for sources declaring the decided entity/activity that retrieval missed (`_declaring_sources`, up to `top_k`). The planner collects every field/join confirmation while laying out the plan (`_field_check`, `_merge_paths`) and asks them in one batch (`_run_checks`), checked in plan order. `JEVAdapter.ask_batch` → `backend.ask(state_with_items, questions)` (per-question subject/facts/prior under `state.items[key]`, `instructions` point at `items.<key>`); backends without `ask` get one decide/classify each; `LexicalDecisionEngine` has no batch (`ask_all` loops). Yes/no questions carry `CRITERIA` (source/field/relationship `true`/`false` definitions — source relevance explicitly counts "needed to connect"). `JevClient.ask` splits a body over 32 KiB (`_post_fitting`) and `_log_usage` logs model · questions · time · tokens · `usage.cost` + session `total_cost`. `JEVAdapter` caches answers (`cache_size`, key excludes lexical terms). A field confirmation is **settled deterministically** (`decided_by="deterministic"`, probability = the catalog prior, not asked) when the catalog leaves no choice — the only field of that semantic type in the source (for the entity: also counting `represents` links, minus the fields already used as filters) — and the prior clears the field threshold; it exists to catch a wrong pick among candidates, and the entity/value it serves was already decided. When there is a choice, the entity question spells the entity out (*"Does x.y hold the user the question asks for (user — A person)?"*). An enumerated-value match is confirmed as the exact reading — *"Does 'failed' in the question mean auth_logs.outcome = 'failure'?"* — and `Catalog.describe_field` (every field subject) lists the field's known values with their synonyms (first 20) plus its notes. `ClarificationNeeded.decisions` carries what was decided before it, and `SemanticSearch.search` keeps them in the result.

**Live evidence** (`evidence.EvidenceProber`, opt-in: `SemanticSearch(live_evidence=True|{max_probes, timeout})`, config `semantic.live_evidence`): one prober per `search()`; `found(ref, value)` runs `Condition(column, "ilike", "%value%")` + `limit=1` through `DuckAPI.fetch` **only if** `map_conditions` says the function consumes it (pushdown) and it takes `limit` — otherwise `None` ("couldn't check", with a note); budget `max_probes`, per-probe `timeout` (thread pool), errors → `None`, results cached per (ref, value lowercased), re-checked in pandas (substring, case-insensitive). The interpreter probes each non-dropped literal over up to 3 fields per candidate source (fields of its known/pinned type, else typed string fields) → `facts.live_check` on source relevance asks and `value_found_in_fields_of_type` on the value-type ask; the planner adds `{value, found}` to asked (not settled) field-filter checks. Probes are logged and returned in `SearchResult.evidence` (in `to_dict`).

**Calibration** (`evaluation.calibrate_thresholds`, `commands.calibrate`, CLI `calibrate DATASET`): plans every labeled question with all thresholds at 0 (restored after), collects `(probability, right?)` per kind (entity/activity against `expected_*`, source relevance against `expected_sources`), and per kind picks, among the current value, observed probabilities, midpoints and "just above the max", the threshold minimizing `cost_wrong`·accepted-wrong + `cost_ask`·rejected-right (ties → closest to current). `CalibrationReport.thresholds` is config-shaped. Field/relationship thresholds aren't calibrated (no labels).

`python -m duckduck.semantic jev-check` / `jev_check()` makes one real `classify` through whichever engine is configured (fails if it's the lexical one).

### LLM: catalog drafting and value extraction

`llm.py` — `LLMClient` protocol (`generate(system, prompt, output_model) -> output_model`); `ClaudeLLM` implements it with the Anthropic SDK's structured outputs (`messages.parse(output_format=Model)`), default model `claude-opus-5`, server-side refusal fallback on by default (`fallbacks="default"`; set `None` behind gateways that reject it). A client with no credentials at all (no `api_key`/`auth_token`/credentials provider after the SDK's own env/profile resolution) fails **at construction** with `MISSING_KEY_HELP` (env var, `authentication` block, `api_key=`), instead of the SDK's "Could not resolve authentication method" at the first request, after tables were already profiled; and `SemanticConfig.build_llm` rejects an `authentication` block that resolves without an `api_key` (suggests `"$secret.<key>"`) rather than silently falling back to the environment. **Providers** (`LLMConfig.provider`): `anthropic` (Claude API, or a gateway via `base_url` + `headers` — an `Authorization`/`x-api-key` header counts as the credential), `foundry` (`ClaudeLLM.on_foundry`: Claude on Microsoft Foundry through the SDK's `AnthropicFoundry`; `resource` or `endpoint`; refusal fallback off unless set explicitly), `azure_openai` (`AzureOpenAILLM`: `openai.AzureOpenAI` + `chat.completions.parse(response_format=Model)`, falling back to `beta.` on older SDKs; `LengthFinishReasonError`/`ContentFilterFinishReasonError` become `LLMError`). Both Azure flavours: API key (block → provider env var) else Entra ID via `azure_token_provider` (`DefaultAzureCredential`, tenant pinned the same way as ADX, scope `AZURE_AI_SCOPE`). In `build_llm`, every connection setting falls back from the block to the resolved `authentication` secret, and `headers` values `"$secret.<key>"` are read from it (`_resolve_headers`); `LLMConfig._fields_match_provider` rejects a setting of another provider (e.g. `deployment` with `foundry`). **`ai_providers` and per-stage LLMs**: every AI model is an `AIProviderConfig` (alias `LLMConfig`) declared by name in the config file's **top-level** `ai_providers` section (next to `services`, outside `semantic`); `SemanticConfig.from_file_data` fills `SemanticConfig.ai_providers` from it (`load` goes through it — build from a parsed file with it, not `model_validate(data["semantic"])`, or references won't resolve). `provider` ∈ anthropic/foundry/azure_openai/openrouter/jev; `api` (`effective_api`: `decisions` for jev, else `chat`) — a `decisions` entry can only be named by `decision_engine.ai_provider`, a `chat` entry by either role. `_fields_match_provider` rejects settings of another provider/api. `default_llm` (used by every stage that doesn't name its own) and `extractor.llm` / `catalog_generation.llm` / `catalog_generation.link_llm` are names only (`LLMRef = str`); `from_file_data` rejects, each with the exact block to write instead: an inline block where a name belongs, `ai_providers` inside `semantic`, the old `llms` section, the old `semantic.llm` key, and the old inline Jev `decision_engine` (`type: jev` / `model` / `authentication`). No inheritance between entries, deliberately. `llm_config(stage) -> (name, AIProviderConfig)` walks `LLM_STAGES` (first set ref wins; `catalog_link` = `link_llm`, then `catalog_generation.llm`), then `default_llm`. `_references_exist` rejects unknown names at load, listing the declared ones. `build_llm(duck, stage)` → `_build_client(name, entry, duck)` (also used by `build_engine` for chat entries); `CatalogGenerator(link_llm=)` handles the final vocabulary call, defaulting to `llm`. **OpenRouter chat**: `OpenRouterLLM` (`openai.OpenAI` at `https://openrouter.ai/api/v1`, `max_tokens`, `extra_body={"provider": {"require_parameters": true}}` so only providers honouring `response_format` serve it, `reasoning.effort`); it shares `_ChatCompletionsLLM.generate` with `AzureOpenAILLM`. Test fakes replacing `build_llm` must accept `stage`. Tests: `tests/test_semantic_llm_providers.py` (real SDK clients, no network). Output models passed to it must stay schema-friendly: lists of objects, no free-form `Dict` fields (that's why `generation.py` has its own `Gen*` shapes instead of reusing `Catalog`).

- **Time windows** (`timeparse.find_window(text, now)` → `(Window, start, end)`, used by `RuleBasedExtractor` step 1, which blanks the matched words so they're never values): EN+PT; periods = ISO date[time], slash date (day-first, month-first only if day-first is invalid), today/yesterday/tomorrow/anteontem, this/last week·month·year (+ PT forms), month (+year; needs a preposition or a year — "may"), weekday (needs a preposition); windows = between/entre/from…to/de…até (start of A → **end** of B), since/desde/a partir de (start), after/depois de (end of P), before/antes de (start of P), until/até (end of P), a period alone (the current one open-ended — "so far today"), and `_LAST_N` (last/past/últimos N units → `last_hours`, units up to years; EN "last week/month/year" stays rolling — "in the last year" = 8760 h —, PT "semana passada"/"ano passado" and "this year" are calendar). Tests: `tests/test_time_windows.py`. `_entity_ask` + `_records_of_activity`: a head noun that is an activity keyword and no entity ("the logins") → fact `head_noun_is_an_activity` + lexical default = the catalog's single row-level entity.
- **`llm_extraction.py` — `LLMExtractor`**: the extraction half of interpreting a question (values + their semantic types, enumerated values, time range). Judgments stay with the decision engine. Everything the LLM returns is checked against the catalog (enum must be a real field's stored value, semantic type must exist, timestamps must parse) and dropped otherwise; shape-recognized literals (IPs, domains) from the rule-based pass stay authoritative. On any LLM failure it falls back to `RuleBasedExtractor` with an always-shown `RuntimeWarning` (`on_error="raise"` to fail instead). **Translation** (`translate=True`, config `extractor.translate`): the same call returns `english_question` (`TRANSLATION_INSTRUCTIONS` appended to any system prompt lacking it); non-term rule literals (IP/email/domain/quoted) are masked as `⟦n⟧` (`_mask`/`_unmask`), extracted values must appear in the question as written, and `_english` discards a translation that lost a placeholder or a value (logger warning) or equals the question; the rules then run over the English reading. `Extraction.english_question` → `SemanticIntent.english_question`; `intent.working_question` feeds retrieval, shapes and planner stems, `intent.decision_state()` sends `query` = English + `original_query` (Jev: `original_question`); clarification texts use the original. Tests: `tests/test_translation.py`.
- **Accents and negation** (`text.fold`: NFKD, combining marks stripped, lowercased — `tokenize` folds, and so do `_NON_VALUE_WORDS`/`_SHAPE_WORDS`/`_SMALL_TALK_FILLER`): an enum match preceded by a word in `extraction._NEGATIONS` (not/never/without/isn't… não/sem/nunca/exceto — never "no", Portuguese "in the"), with only function words between (`_negation_at`), is `EnumMatch.negated` → the planner's filter is `neq` (`<>`, checked as *"Does not 'x' mean f <> 'v'?"*; also in `plan_across`/`plan_browse`); the negation word is consumed, never a value. `LLMEnumValue.negated` carries the same from the LLM. A container noun (`_CONTAINER_NOUNS`: catalog, list, database, registry, feed, catálogo, lista…) right after a matched value, or right before it with only function words between, belongs to it (`_consume_container`: "in the KEV catalog", "no catálogo do KEV") — never a leftover term. Tests: `tests/test_flags_and_negation.py`.
- **Boolean flags get words** (`generation._flag_values`, run in `_assemble` after `_apply_docs`): a `boolean` field's value keys are normalized to `"true"`/`"false"`, `"true"` always exists, and `_flag_words(name)` is appended to it — the humanized name plus its core without `is/has/was/can/in` prefixes and `flag/enabled/set` suffixes (`in_kev` → "in kev", "kev"). `DEFAULT_SOURCE_PROMPT` also asks the LLM for true-words (and false-words only when there's more than a negation). `planner._stored` turns `"true"` into `TRUE`.
- **What gets drafted** (`CatalogGenerator.plan_specs`, when `catalog_generation.tables` isn't given): every plain `table` (by `kinds.kind_of` — catalogs and raw queries are never described as data), plus every table **behind** a connector, discovered through its catalog: `@catalog(lists="table")` links a catalog to the sibling table function its rows feed (`GlueTable.tables` → `table`, `DataExplorer.tables` → `table`, `SQLDatabase.tables` → `table`); the catalog is fetched, each row's columns fill the table function's required params (`glue_table(database=, table_name=)`), and the source is named after the joined args (`security_proxy_logs`). `include`/`exclude` fnmatch the registered name or the joined args (`security.proxy_*`); `max_tables` (default 50) caps the total since each table is an LLM call; a failing catalog, an unregistered sibling, or a table function with no catalog to discover its args becomes a note in `GenerationResult.warnings`, never a crash.
- **Incremental maintenance** (`CatalogGenerator.generate(specs, existing=, force=)`, `commands.refresh_catalog`): generation reads and updates `SemanticConfig.catalog_target()` = `catalog_generation.output_path`, default **`catalog_path` itself**. `_align_with` maps a spec onto an existing source bound to the same `table`+`args` (keeps a hand-written source's name); **Selectors** (`force`/`only` entries, `CatalogGenerator._matches`): fnmatch, case-insensitive, against the spec's catalog name, registered table, joined args, any single arg, or its service — `DuckAPI.service_of` (registered name → the `auto_register` service, filled by `auto_register`) — and `service:table` (service pattern, then any of the table labels). `only` filters the specs before selection. `_select` picks what to draft — `force=<selectors>` → exactly the matches (hand-written included) and nothing else; otherwise new, `force=True` (generated only), or `generated_at` older than `max_age` (`parse_age`: `7d`/`12h`/`30m`/`2w`/seconds) — ordered forced → new → stale-oldest-first, capped at `max_tables` per run (rest in `deferred`). Nothing to draft → no LLM call, `result.changed` False, file untouched. The link pass sees drafted **and** kept sources plus the existing entity/activity names; `_assemble` merges: existing entities/activities win, existing relationships kept while their fields exist (new ones deduped), drafted sources replaced in place and stamped `generated_at`/`generated_by` (`SemanticConfig.llm_label`), the person's `notes` (source and field) and `critical` carried over. `SourceDef`/`FieldDef.notes` are human-only (`GenerationResult.to_yaml` writes an empty `notes: ''` slot right after every source's and field's `description` via `_with_note_slots`, so people see where theirs go; dumped with a no-aliases `SafeDumper` so a shared `generated_at` isn't written as `&id001`): never produced by the LLM, fed back to it in the profile (`owner_notes`, `field_notes`), a note on a field that vanished is reported. An invalid existing file is never overwritten. `auto_refresh` runs `refresh_catalog` inside `SemanticSearch.from_config` (`engine._auto_refresh`; failure with an existing catalog → always-shown `RuntimeWarning`), only allowed when generation maintains `catalog_path`. CLI: `generate-catalog --force [NAME ...]` (bare → `True`) `--only NAME ...`. `commands._apply_verbose` applies `verbose` (same values as `DuckAPI`) even when the caller passes its own `duck`; at DEBUG the generator logs each profile sent and each draft/vocabulary received (`_debug_json`, truncated). Tests: `tests/test_catalog_refresh.py`.
- **Data profiles** (`profiling.profile_frame`, deterministic pandas): generation reads up to `max(sample_rows, profile_rows)` rows; per column `distinct`, `null_ratio`, `min`/`max` (numbers, datetimes, ISO-date text), examples, and `values` for categorical columns (≤ `max_enum_values` distinct, ≥ 20 rows, each value repeating ≥ 4× on average, not numeric/datetime). `_apply_profile` stores `FieldDef.profile` (examples only with `sample_values`, never for `SENSITIVE_TYPES` unless `sample_sensitive`) and merges category values into `values` (not for `IDENTIFIER_TYPES` or `match: contains`; LLM synonyms kept); `_source_profile` stores `SourceDef.profile` (`rows_sampled`, time field `time_min`/`time_max`). The LLM gets `column_stats` without examples, and without value lists when `sample_rows=0`. `describe_field`/`describe_source` surface examples, ranges, "mostly empty", the sampled time span. A sample isn't the table: min/max/time span describe the sampled rows only. `profiling.SHAPES` (url/email/ip_address/domain, ≥90% of values) → `FieldProfile.shape`, and `_apply_profile` fills an empty `semantic_type` from it (the LLM's typing wins).
- **Vocabulary tidy** (`generation._tidy_entities`, end of `_assemble`, over drafted + kept): an entity no field holds (no field of its semantic_type, no `represents` link, not `row_level`) is merged into `_merge_target` — scored 2× per mention of another held entity's name/keyword in its description+keywords, +1 per field of that type in sources claiming it; its name + keywords become the target's keywords, sources' `entities` remapped, warning recorded (no target → kept, warned). Every entity gets its holding fields' names (humanized) as keywords; `_add_keywords` dedupes case-insensitively, caps at 25. Prompts also tell the LLM to claim only held entities. Tests: `tests/test_entity_vocabulary.py`.
- **API docs** (`apidocs.py`, `catalog_generation.api_docs: {selector: location | {location | content, operation}}`, `api_docs_max_chars`; `CatalogGenerator(api_docs=, docs_base_dir=, docs_max_chars=)`): selectors as `force`/`only`, `service:table` preferred over a bare one. `ApiDocs.load` (file relative to the config dir, or URL; ≤30 MB) / `from_content` (inline dict or text) classify as OpenAPI/Swagger (`openapi`/`swagger` + `paths`), hand-written field docs (`fields` or `tables`), or text (HTML → text with `#` headings); a JSON that's neither is an error. `for_table(names, columns, args, operation, max_chars) -> (context, note)`: OpenAPI picks the operation (`operation` = "METHOD /path", a path, or an operationId; else `_score` over the table/method/arg names — last path segment ×3, operationId ×2, summary/tags ×1, GET +1, item-by-id −1, path-param count vs args), sends summary/description/query+path params and the response fields matched to columns (`_row_schema` unwraps arrays and paged envelopes, `_merged` resolves `$ref`/`allOf`, `_flatten` joins nested objects with `_`, `_match_columns`: exact, case-insensitive, unique `_`-suffix); field docs pick the `tables` entry named like the table; text sends the scoring sections within budget. The context goes into the profile as `api_docs`; `_apply_docs` merges documented `enum`/`values` (with synonyms) into value lists (same exclusions as profile categories) and fills an empty description; `SourceDef.api_docs` records `operation (location)`. Failures → one warning per document, table drafted without docs. `_matches` also matches a table label with its `{service}_` prefix stripped (`insightvm:assets`). Tests: `tests/test_api_docs.py`.
- **`generation.py` — `CatalogGenerator`**: pass 1 profiles each table through `DuckAPI.fetch` (columns, dtypes, `sample_rows` rows, connector description, owner `notes`) and asks for a `GenSource`; pass 2 shows all drafts and asks for the shared `GenVocabulary` (entities, activities, relationships). `_assemble` then sanitizes deterministically — fields must be real columns, relationships must join real fields of different sources, claimed entities/activities must be defined — logging every drop in `GenerationResult.warnings` (also written as a YAML header comment). Output is a draft for human review; `sample_rows=0` sends no data values to the LLM.

System prompts for all three LLM calls are configurable (inline or `*_file`); the defaults are the module constants, also copied to `examples/semantic/prompts/` as a starting point.

### Feedback, case memory, suggestions, web app

- **`feedback.py` — `FeedbackStore`** (DuckDB file, thread-safe, `version` bumped per write): `searches` (question, english, `template` = `memory.question_template(working_question, literals)`, status, answer_shape, entity, activity, `sources` = `answer_sources` (plan's, or lookup/locate sections with rows), decisions/pinned JSON, SQL, row count — **never rows** —, catalog_version (`catalog_version()` sha1), engine), `feedback` (verdict ∈ `VERDICTS`, `CATEGORIES`, reason, `expected` ⊆ sources/answer_shape/entity/activity/synonym{field,value,word}, validated), `reviews` (suggestion accepted/dismissed). `stats()` (overall, by_answer_shape, by_source via unnest, by_category, by_day), `cases()` (latest per template: `confirmed` = answered+ok; `corrected` = not answered with a reading in `expected` (`_READING`); a later non-answer without one retires it), `to_evaluation()` (evaluation.json shape + `expected_answer_shape`). NULLs → None (`_nulls`).
- **`SemanticSearch(feedback=store, memory=True|{max_cases, min_similarity}|CaseMemory)`**: `search()` records every result (`result.search_id`, `conversation_id`; a recording error only logs), `feedback(result|id, ...)`. `Conversation.id` ties rounds. On `ClarificationNeeded` the interpreter attaches the partial `intent` (`exc.intent`) so failed searches still have templates. `from_config` builds the store from `semantic.feedback` (`FeedbackConfig`: enabled, path, memory, max_cases, min_similarity, learned_answer_shapes, min_support) unless `feedback=` is passed.
- **`memory.py` — `CaseMemory`**: cosine over content stems of templates, cached per `store.version`. Interpreter: `intent.similar_cases`; case sources added to `retrieved` (still judged); `_remembered()` puts `similar_confirmed_questions` (`as_fact`) in entity/activity/shape ask facts and a `default` (similarity ≥ 0.8) for the lexical engine; source asks get `used_in_similar_confirmed_questions`. `evaluation._measuring` disables recording + memory during `evaluate`/`calibrate_thresholds` (no leakage, no pollution).
- **Giving feedback on the page** (`feedbackWidget(saved, {verdicts, status, more?}, opts)` — one per search, used by the Ask page (`feedbackForm`/`wireFeedback`, `FB`) and every History row (`HISTORY_FB`)): no send button — a verdict tap saves at once (`w.save`, chained so the first save's id is known), category chips (`.fbchip`, toggles) and the reason/expected fields save as they change (text debounced 700ms, flushed on blur); state lives in the widget (categories/reason/expected), the "more" part is class-based (`fbMoreHtml`) and attached lazily (`attachMore`, prefilled from what was saved); every save after the first passes `feedback_id` → `FeedbackStore.record_feedback(feedback_id=)` replaces that row (one row per answer; unknown id → 404). `verdict_of(search_id)`. **History**: `store.searches()` returns `sql` (`feedback.sql_of`: the result's SQL, or each lookup/locate section's as `-- table` blocks), `clarification`, and the latest feedback's `feedback_id`/`expected`; each row has compact 👍🤏👎 (`fbVerdicts(true)`), the question (`.hopen`) opens a detail row with the SQL (*Open in the SQL tab*) and the feedback details; 🤏/👎 opens it. **Take over = yes**: `SemanticSearch.take_over(..., user=)` records `answered` with `takeover.TAKEOVER_REASON` when the search is unrated (`TakenOver.feedback`, in `/api/takeover`'s response; never overrides a rating; a store error only logs) → `fbTakenOver` presses Yes on the page. Tests: `tests/test_takeover.py`.
- **`suggest.py` — `CatalogSuggester`** (`KINDS`: value_synonym, source_example, answer_wording (`_opening`: words before the first catalog-known word/value, ≥2, not already worded), entity_keyword/activity_keyword/source_note (unknown stems — not in `_catalog_texts` vocabulary, not stopwords/shape words/placeholders — in ≥ `min_support` failed searches; target by majority of corrections), unknown_word (no target, not applicable)); ids = sha1(kind|target|change); reviewed ones hidden unless `include_reviewed`. `apply_suggestion` edits the catalog file (validated with `Catalog.model_validate`, `.bak`, rewritten via `GenerationResult.to_yaml` or JSON) or appends wording to `learned_answer_shapes` (loaded by `SemanticConfig.answer_shape_overrides()` via `shapes.merge_answer_shapes`).
- **`server.py` — `create_app(factory, store, catalog_path, learned_shapes_path, min_support, token)`** (FastAPI; `webpage.PAGE` single HTML page: Ask / History / Dashboard / Suggestions, light+dark tokens): `/api/ask`, `/api/answer` (conversations in an LRU dict), `/api/feedback`, `/api/searches[/{id}]`, `/api/stats`, `/api/suggestions[?all=1]`, `/api/suggestions/{id}/accept|dismiss` (accept → apply, review, `factory()` reload), `/api/evaluate` (its own `factory()` instance), `/api/evaluation.json`, `/api/meta`. Rows capped at `MAX_ROWS` per table in responses. Responses are strict JSON (`_finite`: NaN/±Inf → null, `allow_nan=False`) — browsers reject `NaN`, which pandas makes of a NULL count; `tests/test_server.py::test_history_is_readable_by_a_browser_with_unanswered_questions` parses with `parse_constant` rejecting it. The page's `api()` surfaces an unreadable response instead of swallowing it. `token` → `X-Duckduck-Token` required on `/api/*` (`secrets.compare_digest`); `run()` binds 127.0.0.1 by default and warns without `DUCKDUCK_SERVER_TOKEN` elsewhere. `_json_default` turns numpy scalars into numbers.
- **`export.py` — `build_export(store, suggestions, since, limit, include_partial, redact, config_path)`** → `{markdown, gaps, counts}`: rated searches grouped by template; a gap = latest rating per template still not answered (partial optional); sorted by failure count then recency; per gap: what it did (status, shape, tables, rows, clarification), users' categories/reasons, `expected`, open suggestions whose evidence includes its searches, the decision table, pins, SQL, a reproduce command (`--config` when known); `redact` → templates, no SQL/English/reproduce. `commands.feedback_export` (→ `FeedbackExport`, writes `feedback_export.md`; `since` ISO date or `parse_age`), CLI `feedback-export`, `GET /api/export.md`.
- **Scope & preview** (`scope.py`): `search(only_sources=)` (validated: known, non-empty) runs `_search` inside `scope.only_sources(...)` (a `ContextVar`, so concurrent web requests don't leak); `planner._is_allowed` = `allowed_sources` ∧ `scope.in_scope` (covers primary choice, join paths, `plan_across`, catalog answers); the interpreter treats out-of-scope sources as refused for retrieval and skips them in pins/`_declaring_sources`. A `scope` DecisionRecord (by user) is prepended; `SearchResult.only_sources`; `Conversation(only_sources=, pinned=)` carries both every round. `SemanticSearch.preview(q)` → `interpreter.preview` (RuleBasedExtractor, one `ask_all` batch: entity, shape, source relevance for retrieved + memory tables; catalog questions return early, no engine call) + `_preview_joins` (sure, non-row-level entity: `planner._find_entity_field` from each relevant source; the path's tables become relevant + `joined`, its edges → `joins` [{left, right, type, confidence, for}]) + `_systems` (every allowed source grouped by `duck.service_of` / `list_tables()` label, relevant first), LRU-cached per normalized text. Server: `POST /api/preview` (errors → `{"error"}`, never a failure), `/api/ask` takes `only_sources` / `entity` / `blocked_joins` ([[a, b]] → pins `join:a=b` False, refs checked with `catalog.has_field`). Page: 600 ms debounce, AbortController, chips + panel ("only this" per system, a Joins list to untick, "Let it choose"); choices are tied to `pre.forQ` and reset when a new preview's question isn't `sameQuestion` (≥ half the words shared); submitting before the pause, or `askFresh` (suggested questions), clears stale chips and previews the asked question. Tests: `tests/test_scope.py`.
- **Admin** (`admin.py`): `SQLConsole(duck)` = a separate `DuckAPI` sharing `functions`/`service_of`, its connection `SET enable_external_access=false` + `lock_configuration=true`; `read_only_reason` (first keyword ∈ read statements, one statement — `_statements` splits outside quotes —, no `WITH … INSERT/UPDATE/DELETE`); `run` captures this thread's `duckduck` log at INFO (`_captured_log`), caps rows, interrupts after `timeout`. `source_kind(fn)` (`SOURCE_KINDS` by module) → `SemanticSearch.source_icon` (relation → duckdb) → preview `systems[].icon` / `sources[].icon`, `/api/meta.source_icons`, `/api/tables[].icon`. Config: `config_reference()` (registry connectors + constructor params, auth block, `AIProviderConfig`/`SemanticConfig` fields with `#:` docs via `_attr_docs`, nested models recursively), `mask`/`unmask` (`_is_secret_key`; `$secret.` refs kept; `***` without a saved value → error), `validate_config` (structure, connectors, auth, unknown options as warnings, `SemanticConfig.from_file_data`), `save_config` (unmask → validate → `.bak` → atomic replace). Each option carries `input` (`_input_of`: choice+`choices` / bool / int / float / text / list / object / scalar / json; connector hints resolved with `get_type_hints`, DI-only params `_INJECTED` dropped) and connector options `credential` (secret or `_CREDENTIAL_NAMES`) → the page's **Config form** (`renderForm`: `DRAFT` edited through `scope()` objects that appear on first key and vanish when empty; services/AI providers as cards, `authBlock` per `type`, `keyRows` for free keys; Form/JSON toggle, both edit `DRAFT`; unknown keys kept); `reference.secrets` = the `mask` rule for the page. Server: `create_app(console=, config_path=, allow_config_edit=, rebuild=)`; `/api/sql`, `/api/tables` (403 without console), `/api/config` GET (masked + reference) / `validate` / PUT (403 unless editable; `rebuild()` → new factory + console). `serve(allow_sql=True, allow_config_edit=False)`, CLI `serve --no-sql --edit-config`; the SQL tab sits next to Ask and is always shown (`#sqloff` explains when off). Tests: `tests/test_admin.py`.
- **Catalog generation from the page** (`catalog_jobs.py`): `CatalogJobs(run, on_changed)` — one background job at a time (`start` → `RuntimeError` while one runs → 409), `CatalogJob.to_dict(since=)` (state running/done/failed, log lines from `since`, `result_summary`: changed/drafted/kept/deferred/warnings/summary, `reloaded`/`reload_error`); the worker thread's lines are captured by `_JobLog` on the `duckduck` logger (thread filter) with `PROGRESS_LOGGERS` (generation, llm, apidocs) set to INFO for the job — a child's own level, so an SQL console capture changing the parent's level can't hide them. `serve()` passes `catalog_runner(force, only)` = `refresh_catalog` on the current duck (`current`, updated by `rebuild`) + `catalog_info()` (target path, `llm_label`, max_age, max_tables) → `create_app`; gated by `allow_config_edit` (`features.catalog_generation`). Endpoints: `GET /api/catalog` (sources: generated_at/by, fields, notes, args; `not_in_catalog` = plain registered tables no argless source binds, taken-over ones excluded; `off` reason; latest job), `POST /api/catalog/generate {force: bool|[names], only: [names]}` → 202, `GET /api/catalog/jobs/{id}?since=`. A job that changed the file → `state["search"] = factory()` + conversations cleared. Page: `#catcard` above the config editor, `loadCatalog` on every Config tab open, `watchJob`/`pollJob` (1s, `since`), progress from the log's `[i/n]`, Redraft (`force=[name]`) / Add (`only=[name]`) per row, `confirm()` only for *Redraft all generated*. Tests: `tests/test_catalog_jobs.py`.
- **More columns** (`columns.py`): `SemanticSearch.column_options(result)` → one entry per field of the plan's sources (`ref`, `selected`, `asked` = in the original select, `suggested` + `why` = filtered on / time filter / the source's time field; asked, then suggested first); empty unless `can_add_columns` (status ok, a plan, no aggregate, not browse/lookup/locate). `with_columns(result, columns)` / `Conversation.with_columns` (it becomes `conv.result`): `columns` is the complete extra set (`[]` = back); the original select (`plan.select` minus `result.added_columns`) + extras → `validator.validate` → `display_sql` → executor; a shallow copy keeps intent, decisions (+ a `columns` DecisionRecord by the user, replaced each time), `search_id` (not recorded again — same answer, same feedback) and conversation. Never re-interprets. Server: `payload()` adds `result.column_options`; `POST /api/columns {conversation_id, columns}` (400 bad field/not a list, 404 unknown conversation). Page: *＋ Columns* toggles `columnPanel` (`COLS_OPEN` per conversation; chips `.colchip`, "add all" for suggested, "Back to the original columns"), taps debounced 350 ms → one run; `wireFeedback` keeps the same widget element when the same `search_id` is drawn again (a saved verdict stays). Take over after it takes the widened rows. Tests: `tests/test_columns.py`.
- **Take over** (`takeover.py`): `SemanticSearch.take_over(result, name=None, full=True)` / `Conversation.take_over` → `TakenOver` (name, rows, columns, how, `sql`, `report()`); registers a snapshot `dataset(limit=None)` function (module `duckduck.semantic.takeover` → `list_tables()` "Taken over…", `SOURCE_KINDS` icon `dataset`, `service_of` = `SERVICE`) in `search.duck` (so the SQL console, which shares `functions`, sees it); `full` re-executes `query_plan` with `limit=MAX_LIMIT` when the answer hit `plan.limit`; names: `NAME_RE`, not a DuckDB reserved keyword (`duckdb_keywords()`), not a non-taken-over registered table (ours = `search.taken_over` ∪ `service_of == SERVICE`, replaced); default from the question's content words, suffixed `_2`… Server `POST /api/takeover {conversation_id, name, full}` (403 without console, 400 bad name/no rows); `takeover.proposal(search, result)` (suggested name, rows, columns, `capped`/`limit`, names already taken) → `POST /api/takeover/proposal`; page button *Take over from here* → a `<dialog class="modal">` (`takeOver`/`checkTakeoverName`: name validated as typed, replace notice, "fetch every row" only when capped, server errors inline, Esc/backdrop close) → SQL tab. Tests: `tests/test_takeover.py`.
- **Commands**: `serve(run=False → app)`, `feedback_report` → `FeedbackReport`, `feedback_to_eval` → `FeedbackEvaluation` (writes `feedback_evaluation.json`), `feedback_suggest(accept=, dismiss=)` → `SuggestionReport`; all read `semantic.feedback.path` whether or not `enabled`. Extra `[server]` (fastapi, uvicorn). Tests: `tests/test_feedback.py`, `tests/test_server.py`.

### Configuration (`semantic` section of `duckduck.json`) and CLI

`config.py` — `SemanticConfig` (Pydantic, `extra="forbid"`): `catalog_path`, `decision_engine` (`ai_provider` name, or lexical when unset; `retries`, `timeout`, `system_prompt[_file]` for chat engines), `default_llm` (a chat entry name from the file's top-level `ai_providers`), `extractor` (`rules` | `llm` + prompt), `catalog_generation` (prompts, `sample_rows`, `output_path` (default: `catalog_path`), `max_age`, `auto_refresh`, `tables` as `TableSpec`s with structural `args` — or, without `tables`, discovery via `discover`/`include`/`exclude`/`max_tables`), `thresholds`, `allowed_sources`. Credentials go through `DuckAPI.resolve_credentials` — the same `local`/`aws`/`azure` + `$secret.<key>` resolution as the connectors (an `ai_providers` entry's block must yield an `api_key`; an entry without `authentication` falls back to its provider's env var). Relative paths resolve against the config file's directory. `SemanticSearch.from_config(duck)` builds everything; see `duckduck.example.json` for a full `semantic` section.

```bash
python -m duckduck.semantic generate-catalog          # LLM drafts semantic_catalog.yaml
python -m duckduck.semantic ask "Which users accessed github in the last 24hrs?"
python -m duckduck.semantic jev-check                 # one real Jev call: key + network + parsing
```

**CLI ↔ Python parity**: every command is a public function in `commands.py` (`ask`, `generate_catalog`, `jev_check`, plus `connect` for the DuckAPI they start from), exported from `duckduck.semantic`; `__main__.py` only parses arguments and prints `SearchResult.report()` / `GenerationResult.summary()` — the same strings a script gets. Each subparser records its function as `python=` in `set_defaults`; `tests/test_commands.py` checks every subcommand has one, that the CLI and the function produce the same output, and that every `python -m duckduck.semantic <cmd>` in README.md has its Python equivalent documented. Adding a command: write the function in `commands.py` first, then the subparser, then both in the README table.

### Examples (`examples/`) — and what keeps them honest

`examples/README.md` is the index. `examples/local/` (virtualization only: `python` + `files` connectors, `run.py`), `examples/semantic/` (`catalog.yaml`, `sample_sources.py`, `evaluation.json`, `demo.py`; `duckduck.local.json` offline — lexical engine + rules; `duckduck.online.json` — Jev + Claude with keys from `JEV_API_KEY` / `ANTHROPIC_API_KEY`, drafting into the git-ignored `generated_catalog.yaml`, never over `catalog.yaml`; `prompts/` = the default prompts as editable files), and the root `duckduck.example.json` (reference for every real connector + a full `semantic` section; copied to the git-ignored `duckduck.json`). `demo.py` pins the sample data's clock (`kwargs: {"now": ...}` + `clock=`) so `evaluation.json`'s expected answers hold on any date; the `duckduck.*.json` configs generate data relative to the real clock instead.

`tests/test_examples.py` guards all of it: every config parses with known connectors and a valid `semantic` section; every `catalog_generation.tables[].table` in the reference is a name `auto_register` really registers (`{service}_{table}`); every referenced prompt file exists; `demo.py`/`run.py` run from any directory; every offline Python snippet in `examples/README.md` runs verbatim; the online config works with faked LLM/Jev and fails with an actionable message without keys. Tests also load `catalog.yaml`/`sample_sources.py`/`evaluation.json` via `tests/semantic_helpers.py`. Catalog field names are post-normalization column names (DuckAPI turns `.` into `_`); use `column:` when the logical name differs.

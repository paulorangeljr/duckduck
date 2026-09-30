---
name: add-connector
description: Add a new duckduck connector (an API, storage or database wrapper) — the table methods, push-down parameters, pagination, auto_register entry, tests, and the Spark strategy each table declares. Use whenever a new data source, wrapper or connector is being added to duckduck, or an existing connector gets a new table.
---

# Adding a connector to duckduck

Read CLAUDE.md's "Adding a new API wrapper" section first; this is the checklist that goes with it.
Every step ends in something a test checks.

## 1. Learn the API — write these facts down before any code

- **Endpoints → tables**: one method per table. Which arguments build the URL (structural, required) and which
  filter rows (column filters, optional)?
- **Filters the server applies**: equality, contains/starts-with (→ `col_ilike`/`col_like`), ranges (→ `col_gt`…),
  or arbitrary conditions (→ `where`). Superset, never subset (CLAUDE.md "Operator → parameter convention").
- **Pagination kind**: `page=N` · `offset=N` · cursor (`nextLink`, `after=`, a token) · none.
- **Does a response say the total** (rows or pages)? Which field/header?
- **Rate limit**: requests per window, per key? Parallel requests allowed?
- **Where the data lives**: behind the API only, or also in files/tables Spark could read (S3/ADLS/JDBC)?

If a doc host is unreachable, say so in the connector's docstring and in CLAUDE.md (as for Axonius), and keep
the shapes you couldn't verify in one place.

## 2. Write the connector

- `duckduck/<name>.py`, a class with `from_secret(cls, secret, **overrides)`.
- Table methods: structural args required and positional; column filters `Optional[X] = None`; `where` when the
  server takes arbitrary conditions (plus `pushdown_blocker` for what it can't; declare `WHERE_OPS` with `"in"` and
  an `IN_MAX` per request when it can take a list of values — that's what narrows it in a JOIN); `limit: Optional[int] = None`
  last. Return a `pd.DataFrame` (`pd.json_normalize(..., sep="_")`).
- Log HTTP with `instrument_session(self.session)`; report pages with `PageProgress`.
- **Page loop**: a page-numbered or offset API that reports a total pages through
  `duckduck.slicing.pages(fetch_page, page_size)` — `fetch_page(n) -> (rows, total_rows or None)` — never its own
  `while` loop. That is what lets Spark split the read. Cursor APIs keep their own loop.
- `@needs_arguments(...)` (`duckduck.kinds`) on a table whose optional arguments are really needed — one of a
  group (`("list_id", "list_name")`) or a single one (`"drive_id"`); leave out what the config can supply. SQL
  clients then see in its comment which arguments to put in the WHERE, and its usage example shows them.
- `@catalog` on listing methods (with `lists="<table fn>"` when its rows are that function's arguments),
  `@raw_query` on a passthrough of the source's own query language. **A table function (required args) needs
  such a catalog** whenever the source can list its tables: without it SQL clients (the PostgreSQL endpoint)
  only see the function (arguments in the WHERE), the SQL tab can't show them, and catalog generation can't discover them. `drafts=False` when the listing
  is mostly the platform's own tables (ServiceNow's `sys_db_object`), so they aren't all drafted.
- `iter_<table>` streaming methods yielding one DataFrame per page when the API pages.

## 3. Decide how Spark reads each table — `@spark_plan`

Answer CLAUDE.md "Spark: choosing the strategy" in order for **every** table method; the first "yes" wins:

1. Data already in files/tables Spark reads (Parquet/Delta/Iceberg/CSV on S3/ADLS, JDBC) → `native` + a
   `_spark_<table>(<structural args>) -> SparkSource` hook (it only says where; it never reads).
2. A catalog/listing → `driver`.
3. The rate limit forbids parallel requests → `driver` (name the limit in `why`).
4. Any page readable on its own **and** a total reported → `partitioned`, `by="pages"` (needs step 2's page loop).
5. The call splits into independent pieces (date windows, one id each) → `partitioned`, `by="<method>"` returning
   the kwargs of each piece — covering the call exactly once.
6. Otherwise (cursor, no total, one response) → `driver`.

```python
from .sparkplan import SparkSource, spark_plan

@spark_plan("partitioned", by="pages", max_parallel=4, why="page=N with totalResources; 4 concurrent calls allowed")
def assets(self, hostname: Optional[str] = None, limit: Optional[int] = None) -> pd.DataFrame: ...
```

`max_parallel` comes from the rate limit, not the cluster. `why` names the deciding fact. Keep the object
picklable (it travels to Spark executors for partitioned reads).

## 4. Register it

- `SERVICE_REGISTRY` in `duckduck/registry.py` (`factory`, `tables`, `streaming_tables`,
  `requires_authentication`, `path_options`).
- `_CONNECTOR_SOURCE_LABELS` in `duckduck/core.py`; `SOURCE_KINDS` in `duckduck/semantic/admin.py` (icon);
  the page's `CONNECTOR_INFO` blurb in `duckduck/semantic/webpage.py`.
- An entry in `duckduck.example.json` (a secret-manager `authentication` block, never real secrets).

## 5. Test it — `tests/test_<name>.py`

- Mock the HTTP session (a `requests` adapter mounted on `self.session`, or `MagicMock`), never the network.
- Push-down: the kwargs a `duck.sql(...)` query sends reach the method (and LIMIT only when safe).
- Pagination: every page read; a total stops it.
- Spark: `tests/test_spark.py` already fails if a table has no `@spark_plan`. For `by="pages"` tables add the
  connector to `test_paged_tables_really_go_through_the_shared_pager` (a probe must see the total); for
  `by=<method>` test that the pieces cover the call exactly once; for `native` test the `SparkSource`.
- Run `python -m pytest tests/ -q`.

## 6. Document it

- CLAUDE.md: a section like the other connectors (endpoints, push-down, pagination, auth, caveats) and its line
  in "Spark: choosing the strategy" → "Current decisions".
- README.md: how to configure and query it.

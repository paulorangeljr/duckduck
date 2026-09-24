"""
Azure Data Explorer (Kusto) wrapper for use with DuckAPI.

Queries run on the cluster in KQL through Microsoft's official
``azure-kusto-data`` SDK, and only the result comes back as a DataFrame.
DuckAPI's push-down reaches ``table()`` through the ``where`` parameter
(see ``duckduck.pushdown``), and every simple ``WHERE`` condition is
translated into a KQL ``where`` on the cluster:

==========================  =========================================
SQL                         KQL
==========================  =========================================
``col = v``                 ``col == v``
``col LIKE '%x%'``          ``col contains_cs "x"`` (``startswith_cs`` /
                            ``endswith_cs``, or ``==`` without ``%``)
``col ILIKE '%x%'``         ``col contains "x"`` (``startswith`` /
                            ``endswith``, or ``=~``)
other LIKE patterns         ``col matches regex "^...$"`` (``%`` → ``.*``,
                            ``_`` → ``.``) — exact, not approximated
``col >= v`` (etc.)         ``col >= v``
``LIMIT n``                 ``| take n``
==========================  =========================================

Values never reach KQL raw. Each one becomes an escaped string literal
converted to the column's real type (``todatetime("...")``,
``tolong("...")``, ...), using the table's schema from ``getschema``
(cached). That way a SQL ``WHERE ts >= '2026-09-23 12:00:00'`` compares
as a datetime on the cluster. Conditions on columns the table doesn't
have are skipped.

Usage
-----
::

    from duckduck import DuckAPI, DataExplorer

    # App registration (client id + secret)
    adx = DataExplorer.from_app("mycluster.westeurope", "SecurityLogs",
                                tenant_id="...", client_id="...", client_secret="...")
    # ...or managed identity / az login / env vars
    adx = DataExplorer.from_default_credential("mycluster.westeurope", "SecurityLogs")

    duck = DuckAPI()
    duck.register_api_function("adx_table", adx.table)
    duck.register_api_function("adx_tables", adx.tables)
    duck.sql(
        "SELECT * FROM adx_table(table_name='ProxyLogs')"
        " WHERE Timestamp >= '2026-09-23' AND Url LIKE '%github%' LIMIT 100"
    ).df()
"""

from __future__ import annotations

import re
import time
from typing import Any, Dict, Iterator, List, Optional

import pandas as pd

from .kinds import catalog, raw_query
from .logs import get_logger
from .pushdown import Condition, parse_like

logger = get_logger("adx")

try:
    from azure.kusto.data import ClientRequestProperties, KustoClient, KustoConnectionStringBuilder
    from azure.kusto.data.helpers import dataframe_from_result_table
except ImportError:  # optional dependency
    KustoClient = None

#: Table names are spliced into KQL as ``['name']``, so they're restricted
#: to characters that can't break out of that quoting.
_TABLE_NAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_ .\-]*$")

#: KQL ``getschema`` ColumnType → the conversion function for a literal.
_CONVERTERS = {
    "string": "tostring", "datetime": "todatetime", "timespan": "totimespan",
    "long": "tolong", "int": "toint", "real": "todouble", "decimal": "todecimal",
    "bool": "tobool", "guid": "toguid", "dynamic": "todynamic",
}

#: ``(case_sensitive, pattern kind)`` → KQL string operator.
_LIKE_OPERATORS = {
    (True, "contains"): "contains_cs", (True, "startswith"): "startswith_cs",
    (True, "endswith"): "endswith_cs", (True, "equals"): "==",
    (False, "contains"): "contains", (False, "startswith"): "startswith",
    (False, "endswith"): "endswith", (False, "equals"): "=~",
}
_COMPARISONS = {"eq": "==", "gt": ">", "gte": ">=", "lt": "<", "lte": "<="}

#: ``.show tables details`` column → friendlier name in ``tables()``.
_TABLE_DETAIL_COLUMNS = {
    "TableName": "table_name", "Folder": "folder", "DocString": "description",
    "TotalRowCount": "row_count", "TotalOriginalSize": "original_size_bytes",
    "TotalExtentSize": "extent_size_bytes", "MinExtentsCreationTime": "oldest_data",
    "MaxExtentsCreationTime": "newest_data",
}


def kql_string(value: Any) -> str:
    """A double-quoted KQL string literal, with ``\\`` ``"`` and control characters escaped."""
    text = str(value)
    out = []
    for ch in text:
        if ch == "\\":
            out.append("\\\\")
        elif ch == '"':
            out.append('\\"')
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\r":
            out.append("\\r")
        elif ch == "\t":
            out.append("\\t")
        elif ord(ch) < 0x20:
            raise ValueError(f"control character {ch!r} can't be sent in a KQL literal")
        else:
            out.append(ch)
    return '"' + "".join(out) + '"'


def kql_ident(name: str) -> str:
    """A bracket-quoted KQL identifier (``['name']``)."""
    if "'" in name or "\\" in name or "]" in name:
        raise ValueError(f"unsupported character in KQL identifier {name!r}")
    return f"['{name}']"


def like_to_regex(pattern: str) -> str:
    """A SQL LIKE pattern as an anchored RE2 regex (``%`` → ``.*``, ``_`` → ``.``)."""
    parts = []
    for ch in pattern:
        if ch == "%":
            parts.append(".*")
        elif ch == "_":
            parts.append(".")
        else:
            parts.append(re.escape(ch))
    return "(?s)^" + "".join(parts) + "$"


def normalize_cluster(cluster: str) -> str:
    """``mycluster.westeurope`` → ``https://mycluster.westeurope.kusto.windows.net``; full URLs as-is."""
    cluster = cluster.strip().rstrip("/")
    if "://" in cluster:
        return cluster
    if ".kusto." in cluster or cluster.endswith(".microsoft.com"):
        return f"https://{cluster}"
    return f"https://{cluster}.kusto.windows.net"


class DataExplorer:
    """
    One ADX database on one cluster.

    Parameters
    ----------
    cluster : str
        ``mycluster.westeurope`` or a full URL
        (``https://mycluster.westeurope.kusto.windows.net``).
    database : str
        Default database for every query.
    kcsb : KustoConnectionStringBuilder, optional
        Built by ``from_app`` / ``from_default_credential``. Pass your own
        for any other auth mode the SDK supports.
    client : KustoClient, optional
        For tests / dependency injection.
    notruncation : bool
        ADX truncates results over its default limits (500,000 rows /
        64 MB) and fails the query. ``True`` lifts that for queries that
        really need the whole result. Prefer ``WHERE``/``LIMIT``, which
        run on the cluster anyway.
    """

    def __init__(
        self,
        cluster: str,
        database: str,
        kcsb: Any = None,
        client: Any = None,
        notruncation: bool = False,
    ):
        self.base_url = normalize_cluster(cluster)
        self.database = database
        self.notruncation = notruncation
        if client is None:
            if KustoClient is None:
                raise ImportError(
                    "azure-kusto-data is required for the Azure Data Explorer connector.\n"
                    'Install with:  pip install "duckduck[adx]"'
                )
            client = KustoClient(kcsb)
        self.client = client
        self._schema_cache: Dict[str, Dict[str, str]] = {}

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------

    @classmethod
    def from_app(
        cls, cluster: str, database: str, tenant_id: str, client_id: str, client_secret: str, **kwargs
    ) -> "DataExplorer":
        """App registration (service principal) with a client secret."""
        if KustoClient is None:
            return cls(cluster, database, **kwargs)  # raises the ImportError
        kcsb = KustoConnectionStringBuilder.with_aad_application_key_authentication(
            normalize_cluster(cluster), client_id, client_secret, tenant_id
        )
        return cls(cluster, database, kcsb=kcsb, **kwargs)

    @classmethod
    def from_default_credential(
        cls, cluster: str, database: str, tenant_id: Optional[str] = None, **kwargs
    ) -> "DataExplorer":
        """
        ``DefaultAzureCredential``: managed identity, ``az login``, the
        ``AZURE_*`` environment variables, VS Code... — whichever is
        available, in Azure's own order. ``tenant_id`` pins the tenant
        when you have access to more than one.
        """
        if KustoClient is None:
            return cls(cluster, database, **kwargs)  # raises the ImportError
        from azure.identity import DefaultAzureCredential

        credential = (
            DefaultAzureCredential(interactive_browser_tenant_id=tenant_id, additionally_allowed_tenants=[tenant_id])
            if tenant_id else DefaultAzureCredential()
        )
        kcsb = KustoConnectionStringBuilder.with_azure_token_credential(normalize_cluster(cluster), credential)
        return cls(cluster, database, kcsb=kcsb, **kwargs)

    @classmethod
    def from_secret(cls, secret: Dict[str, Any], **overrides) -> "DataExplorer":
        """
        Picks the auth mode from the keys present: ``client_id`` +
        ``client_secret`` (+ ``tenant_id``) → app registration; otherwise
        ``DefaultAzureCredential`` (optionally pinned by ``tenant_id``).
        ``cluster`` and ``database`` can come from the secret or from the
        service config.
        """
        merged = {**secret, **overrides}
        cluster, database = merged.pop("cluster", None), merged.pop("database", None)
        if not cluster or not database:
            raise ValueError("Azure Data Explorer needs 'cluster' and 'database'.")
        client_id, client_secret = merged.pop("client_id", None), merged.pop("client_secret", None)
        tenant_id = merged.pop("tenant_id", None)
        if client_id and client_secret:
            if not tenant_id:
                raise ValueError("App-registration auth needs 'tenant_id' too.")
            return cls.from_app(cluster, database, tenant_id, client_id, client_secret, **merged)
        return cls.from_default_credential(cluster, database, tenant_id=tenant_id, **merged)

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    def _properties(self):
        if KustoClient is None:
            return None
        props = ClientRequestProperties()
        if self.notruncation:
            props.set_option("notruncation", True)
        return props

    def _run(self, kql: str) -> pd.DataFrame:
        # execute_query (never execute): a query that starts with "." would
        # otherwise run as a control command (.drop, .set ...).
        logger.info("KQL on %s/%s: %s", self.base_url, self.database, " ".join(kql.split()))
        started = time.perf_counter()
        response = self.client.execute_query(self.database, kql, self._properties())
        df = dataframe_from_result_table(response.primary_results[0])
        logger.info("    → %s rows in %.2fs", f"{len(df):,}", time.perf_counter() - started)
        return df

    def _run_mgmt(self, command: str) -> pd.DataFrame:
        logger.info("command on %s/%s: %s", self.base_url, self.database, command)
        response = self.client.execute_mgmt(self.database, command, self._properties())
        return dataframe_from_result_table(response.primary_results[0])

    def _schema(self, table_name: str) -> Dict[str, str]:
        """``{column_name: kql_type}`` for a table, via ``getschema`` (cached)."""
        if table_name not in self._schema_cache:
            df = self._run(f"{self._table_ref(table_name)} | getschema")
            self._schema_cache[table_name] = dict(zip(df["ColumnName"], df["ColumnType"]))
        return self._schema_cache[table_name]

    @staticmethod
    def _table_ref(table_name: str) -> str:
        if not _TABLE_NAME_RE.match(table_name or ""):
            raise ValueError(f"invalid ADX table name {table_name!r}")
        return kql_ident(table_name)

    # ------------------------------------------------------------------
    # Push-down translation
    # ------------------------------------------------------------------

    def _where_kql(self, table_name: str, where: Optional[List[Condition]]) -> List[str]:
        if not where:
            return []
        schema = self._schema(table_name)
        by_lower = {name.lower(): name for name in schema}
        clauses = []
        for cond in where:
            column = by_lower.get(cond.column.lower())
            if column is None:
                continue  # not this table's column (DuckDB would reject it anyway)
            clauses.append(self._condition_kql(kql_ident(column), schema[column], cond))
        return clauses

    @staticmethod
    def _literal(value: Any, kql_type: str) -> str:
        """A value as a KQL expression of the column's type."""
        if isinstance(value, bool):
            text = "true" if value else "false"
            return text if kql_type == "bool" else f"{_CONVERTERS.get(kql_type, 'tostring')}({kql_string(text)})"
        if isinstance(value, (int, float)) and kql_type in ("long", "int", "real", "decimal"):
            return repr(value)
        literal = kql_string(value)
        return literal if kql_type == "string" else f"{_CONVERTERS.get(kql_type, 'tostring')}({literal})"

    def _condition_kql(self, col: str, kql_type: str, cond: Condition) -> str:
        if cond.op in ("like", "ilike"):
            text_col = col if kql_type == "string" else f"tostring({col})"
            case_sensitive = cond.op == "like"
            pattern = parse_like(cond.value)
            if pattern is not None:
                operator = _LIKE_OPERATORS[(case_sensitive, pattern.kind)]
                return f"{text_col} {operator} {kql_string(pattern.text)}"
            regex = like_to_regex(str(cond.value))
            if not case_sensitive:
                regex = "(?i)" + regex
            return f"{text_col} matches regex {kql_string(regex)}"
        if cond.op not in _COMPARISONS:
            raise ValueError(f"unsupported push-down operator {cond.op!r}")
        return f"{col} {_COMPARISONS[cond.op]} {self._literal(cond.value, kql_type)}"

    def _table_kql(self, table_name: str, where: Optional[List[Condition]], limit: Optional[int]) -> str:
        kql = self._table_ref(table_name)
        clauses = self._where_kql(table_name, where)
        if clauses:
            kql += "\n| where " + "\n    and ".join(clauses)
        if limit is not None:
            kql += f"\n| take {int(limit)}"
        return kql

    # ------------------------------------------------------------------
    # Tables
    # ------------------------------------------------------------------

    def table(
        self,
        table_name: str,
        where: Optional[List[Condition]] = None,
        limit: Optional[int] = None,
    ) -> pd.DataFrame:
        """
        Reads a table, filtering on the cluster.

        Parameters
        ----------
        table_name : str
            Structural — the ADX table (inline: ``adx_table(table_name='ProxyLogs')``).
        where : list[Condition], optional
            Filled by DuckAPI's push-down; translated to a KQL ``where``.
        limit : int, optional
            ``| take n`` on the cluster.
        """
        return self._run(self._table_kql(table_name, where, limit))

    @raw_query
    def query(self, kql: str, limit: Optional[int] = None) -> pd.DataFrame:
        """
        Runs a KQL query as-is (read-only: control commands are refused).

        Parameters
        ----------
        kql : str
            Structural — the query (inline: ``adx_query(kql='ProxyLogs | summarize count() by Host')``).
        limit : int, optional
            Appended as ``| take n``.
        """
        if kql.lstrip().startswith("."):
            raise ValueError("adx_query runs queries only; control commands (starting with '.') are refused.")
        if limit is not None:
            kql = f"{kql.rstrip().rstrip(';')}\n| take {int(limit)}"
        return self._run(kql)

    @catalog
    def tables(self, folder: Optional[str] = None, limit: Optional[int] = None) -> pd.DataFrame:
        """
        Lists the database's tables: name, folder, description, row count,
        size and data time range (``.show tables details``). Falls back to
        the bare ``.show tables`` if the details need more permissions than
        you have.
        """
        try:
            raw = self._run_mgmt(".show tables details")
        except Exception:
            raw = self._run_mgmt(".show tables")
        keep = [c for c in _TABLE_DETAIL_COLUMNS if c in raw.columns]
        df = raw[keep].rename(columns=_TABLE_DETAIL_COLUMNS)
        df.insert(1, "database", self.database)
        if folder is not None and "folder" in df.columns:
            df = df[df["folder"] == folder]
        df = df.sort_values("table_name").reset_index(drop=True)
        return df.head(limit) if limit is not None else df

    @catalog
    def columns(self, table_name: str) -> pd.DataFrame:
        """A table's columns and their KQL types (structural ``table_name``)."""
        schema = self._schema(table_name)
        return pd.DataFrame(
            [{"table_name": table_name, "column_name": c, "data_type": t} for c, t in schema.items()]
        )

    # ------------------------------------------------------------------
    # Streaming
    # ------------------------------------------------------------------

    def iter_table(
        self,
        table_name: str,
        where: Optional[List[Condition]] = None,
        chunksize: int = 10_000,
    ) -> Iterator[pd.DataFrame]:
        """
        Yields up to ``chunksize`` rows at a time. ADX returns the (already
        filtered) result in one response; this splits it — same trade-off
        as ``GlueTable.iter_table``.
        """
        df = self.table(table_name, where=where)
        for start in range(0, len(df), chunksize):
            chunk = df.iloc[start:start + chunksize]
            if not chunk.empty:
                yield chunk

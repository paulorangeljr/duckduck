"""
DuckAPI — query external APIs with SQL.

Supports push-down of SQL predicates to registered functions:

- LIMIT  → passed as ``limit=N`` if the function accepts that parameter
- WHERE  → simple conditions (=, LIKE, >, <, >=, <=) are passed as
           kwargs if the function accepts that parameter name

Registered functions receive the predicates they know about and ignore
the rest; DuckDB applies the remainder normally on top of the returned
DataFrame.
"""

import ast
import inspect
import json
import logging
import os
import re
import time
import warnings
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import duckdb
import pandas as pd
import sqlglot
import sqlglot.expressions as exp

from .logs import get_logger, set_verbose, short, verbose_from_env
from .pushdown import Condition, assign_conditions, map_conditions, parse_like

logger = get_logger("core")


# ---------------------------------------------------------------------------
# Push-down context
# ---------------------------------------------------------------------------


@dataclass
class PushDownContext:
    """
    Predicates extracted from the SQL that can be sent to the API.

    Attributes
    ----------
    limit : int | None
        The query's LIMIT value, if present.
    filters : dict[str, Any]
        Simple WHERE conditions: ``{column_name: value}``.
        Operators supported for push-down: ``=``, ``LIKE``,
        ``>``, ``<``, ``>=``, ``<=``.

    Examples
    --------
    For the query::

        SELECT * FROM assets(hostname='web') WHERE severity = 'critical' LIMIT 50

    the context will be::

        PushDownContext(limit=50, filters={"severity": "critical"})

    Note that ``hostname='web'`` was already passed explicitly in the
    function call and does **not** show up in ``filters``.
    """

    limit: Optional[int] = None
    #: Equality conditions only, ``{column: value}`` (kept for callers that
    #: read it directly; ``conditions`` is the full, operator-aware list).
    filters: Dict[str, Any] = field(default_factory=dict)
    #: Every simple condition extracted from the WHERE clause.
    conditions: List[Condition] = field(default_factory=list)
    #: False when the WHERE clause has anything that couldn't be extracted
    #: (OR, NOT, IN, functions...) — some filtering is left to DuckDB alone.
    complete: bool = True
    #: Whether the query's shape allows LIMIT to reach the source at all:
    #: one relation, no JOIN / GROUP BY / DISTINCT / ORDER BY / aggregate /
    #: window / subquery / set operation. Otherwise capping rows at the
    #: source would change the answer.
    limit_safe: bool = True
    #: Why ``limit_safe`` is False (e.g. ``"ORDER BY"``), for verbose output.
    limit_blocker: Optional[str] = None


# ---------------------------------------------------------------------------
# SQL extraction helpers
# ---------------------------------------------------------------------------

_CONDITION_OPS = {
    exp.EQ: "eq", exp.Like: "like", exp.ILike: "ilike",
    exp.GT: "gt", exp.GTE: "gte", exp.LT: "lt", exp.LTE: "lte",
}


def _literal_value(node: exp.Expression) -> Any:
    """Converts a sqlglot Literal node into a Python value."""
    if isinstance(node, exp.Literal):
        if node.is_number:
            try:
                return int(node.this)
            except ValueError:
                return float(node.this)
        return node.this  # unquoted string
    if isinstance(node, exp.Boolean):
        return node.this
    return None


def _extract_filters(node: exp.Expression, ctx: "PushDownContext") -> None:
    """
    Walks the WHERE tree and extracts simple conditions into ``ctx``.

    Supports: ``col (= | LIKE | ILIKE | > | < | >= | <=) literal``, ANDed.
    Anything else (OR, NOT, IN, functions, column-to-column...) is left
    to DuckDB and marks the context incomplete.
    """
    if node is None:
        return

    op = _CONDITION_OPS.get(type(node))
    if op is not None:
        left, right = node.left, node.right
        val = _literal_value(right) if isinstance(left, exp.Column) else None
        if val is None:
            ctx.complete = False
            return
        column = left.name.lower()
        table = left.table.lower() if left.table else None
        ctx.conditions.append(Condition(column=column, op=op, value=val, table=table))
        if op == "eq":
            ctx.filters[column] = val
        return

    if isinstance(node, (exp.And, exp.Where)):
        for child in node.args.values():
            if isinstance(child, exp.Expression):
                _extract_filters(child, ctx)
        return

    ctx.complete = False


def _limit_blocker(parsed: exp.Expression) -> Optional[str]:
    """
    Why a source-side LIMIT could change the query's answer, or None when
    it can't (see PushDownContext.limit_safe).
    """
    if not isinstance(parsed, exp.Select):
        return "set operation (UNION/EXCEPT/INTERSECT)"
    for arg, reason in (
        ("joins", "JOIN"), ("group", "GROUP BY"), ("distinct", "DISTINCT"),
        ("order", "ORDER BY"), ("having", "HAVING"), ("with", "WITH/CTE"),
    ):
        if parsed.args.get(arg):
            return reason
    for node, reason in ((exp.AggFunc, "aggregate"), (exp.Window, "window function"), (exp.Subquery, "subquery")):
        if parsed.find(node):
            return reason
    return None


_OP_SQL = {"eq": "=", "like": "LIKE", "ilike": "ILIKE", "gt": ">", "gte": ">=", "lt": "<", "lte": "<="}


def _describe_condition(c: Condition) -> str:
    column = f"{c.table}.{c.column}" if c.table else c.column
    return f"{column} {_OP_SQL.get(c.op, c.op)} {c.value!r}"


def _why_not_pushed(c: Condition, accepted: set) -> str:
    if c.op in ("like", "ilike"):
        has_param = f"{c.column}_ilike" in accepted or (c.op == "like" and f"{c.column}_like" in accepted)
        if has_param and parse_like(c.value) is None:
            return "pattern not translatable ('_' wildcard / inner '%'), DuckDB filters"
        if c.op == "ilike" and f"{c.column}_like" in accepted:
            return f"{c.column}_like is case-sensitive, ILIKE needs {c.column}_ilike; DuckDB filters"
        return f"no {c.column}_like/_ilike parameter, DuckDB filters"
    if c.op == "eq":
        return f"no '{c.column}' parameter, DuckDB filters"
    return f"no {c.column}_{c.op} parameter, DuckDB filters"


# ---------------------------------------------------------------------------
# DuckAPI
# ---------------------------------------------------------------------------


class DuckAPI:
    """
    SQL engine that lets you query Python functions as if they were tables.

    Basic usage
    -----------
    ::

        duck = DuckAPI()

        duck.register_api_function("assets", insightvm.assets)
        duck.register_api_function("vulns",  insightvm.vulnerabilities)

        # LIMIT push-down
        duck.sql("SELECT * FROM assets LIMIT 10").df()

        # Column filter via WHERE → automatic push-down
        duck.sql("SELECT * FROM assets WHERE hostname = 'web' LIMIT 50").df()

        # Simple WHERE push-down
        duck.sql("SELECT * FROM vulns WHERE severity = 'critical'").df()

        # Structural parameter (builds the URL) inline + column filter in WHERE
        duck.sql('''
            SELECT a.ip, v.title
            FROM assets AS a
            JOIN asset_vulns(asset_id=42) AS v ON true
            WHERE a.hostname = 'web' AND v.severity = 'critical'
        ''').df()

    Convention: inline vs WHERE
    ---------------------------
    The ``func(param=val)`` syntax should be used **only** for structural
    parameters that don't correspond to columns in the result (e.g.
    ``asset_id``, which determines the ``/assets/{id}/vulns`` endpoint).

    Regular column filters belong in the ``WHERE`` clause and are
    injected automatically as kwargs when the function accepts that
    parameter.

    DuckDB applies the filter on the result either way, guaranteeing
    correctness even when the API returns extra data.
    """

    def __init__(self, database: str = ":memory:", verbose=None):
        """
        Parameters
        ----------
        database : str
            DuckDB database (default in-memory).
        verbose : bool or str, optional
            ``True``/``"info"``: log, per query, how each table was called,
            which WHERE/LIMIT went to the source (and why not, when not),
            every HTTP request, pagination progress with elapsed/remaining
            time. ``"debug"`` adds request bodies and query parameters.
            Defaults to the ``DUCKDUCK_VERBOSE`` environment variable; off
            when neither is set. See ``duckduck.logs``.
        """
        if verbose is None:
            verbose = verbose_from_env()
        if verbose is not None:
            set_verbose(verbose)
        self.conn = duckdb.connect(database)
        self.functions: Dict[str, Any] = {}
        self._streaming_functions: Dict[str, Any] = {}
        self._table_counter = 0

    # ------------------------------------------------------------------
    # Function registration
    # ------------------------------------------------------------------

    def register_api_function(self, name: str, fetch_function) -> None:
        """
        Registers a Python function as a SQL "table".

        Parameters
        ----------
        name : str
            Table name in SQL (case-insensitive).
        fetch_function : callable
            Function that returns list[dict], dict, or pd.DataFrame.
            Parameters with the same names as result columns / ``limit``
            get automatic push-down.
        """
        self.functions[name.lower()] = fetch_function

    def register_streaming_function(self, name: str, iter_function) -> None:
        """
        Registers a generator function for use with ``stream()``.

        Parameters
        ----------
        name : str
            Same name used in ``register_api_function``.
        iter_function : callable
            Generator that accepts the same filter kwargs as the regular
            function and ``yield``s one ``pd.DataFrame`` per page.
            Doesn't need to accept ``limit`` — stream iterates every page.
        """
        self._streaming_functions[name.lower()] = iter_function

    # ------------------------------------------------------------------
    # Auto-registration of known wrappers (SharePoint, InsightVM, ...)
    # ------------------------------------------------------------------

    #: Default JSON config file name, looked up in the current directory
    #: when auto_register() is called with no ``services`` and no
    #: ``DUCKDUCK_CONFIG`` environment variable is set.
    DEFAULT_CONFIG_PATH = "duckduck.json"

    #: Prefix marking a field in an ``authentication`` block as a
    #: reference into the fetched secret, e.g. ``"$secret.client_secret"``.
    _SECRET_REF_PREFIX = "$secret."

    def auto_register(
        self,
        services: Optional[Dict[str, Dict[str, Any]]] = None,
        secrets: Optional[Dict[str, Any]] = None,
        config_path: Optional[str] = None,
        on_error: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Instantiates and registers known API wrappers automatically
        (see ``duckduck.registry.SERVICE_REGISTRY``), without having to
        call ``register_api_function`` by hand for every method.

        Can be called as just ``duck.auto_register()``: when ``services``
        is omitted, the config is loaded from a JSON file instead (see
        "Loading from a JSON file" below).

        Parameters
        ----------
        services : dict, optional
            ``{name: config}``. ``name`` becomes the prefix of the
            registered tables (``{name}_{table}``) — allowing multiple
            instances of the same wrapper (e.g. ``insightvm_prod`` and
            ``insightvm_dev``).

            ``config`` accepts:

            - ``connector`` : str, optional
                Key in ``SERVICE_REGISTRY`` (``"sharepoint"``,
                ``"insightvm"``) — which wrapper this is. Default:
                ``name`` itself.
            - ``authentication`` : dict, required
                How to build the credentials dict passed to the
                connector's ``from_secret``. Always has a ``type``:

                - ``"local"``: every other field in the block is used
                  exactly as written — hardcoded, no secret store
                  involved. E.g. ``{"type": "local", "username": "a",
                  "password": "b"}``.
                - ``"aws"``: fetches a JSON secret from AWS Secrets
                  Manager. Requires ``secret_id`` (plus optional
                  ``region_name``). The secret's keys become the base
                  credentials; any other field in the block overrides or
                  adds to that, either as a literal value or, written as
                  ``"$secret.<key>"``, pulled from that key in the fetched
                  secret instead of being duplicated by hand.
                - ``"azure"``: same idea via Azure Key Vault. Requires
                  ``secret_id`` (the secret's name) and ``vault_url``.
                  The secret's value must be a JSON object, same shape as
                  the AWS case.
            - any other keys
                Extra kwargs passed through to the connector's
                constructor (e.g. ``hostname``, ``site_path``,
                ``default_page_size``) — anything that isn't a
                credential.

            When omitted, loaded from a JSON file — see below.
        secrets : dict, optional
            Advanced override: ``{"aws": <SecretsManager instance>,
            "azure": <AzureKeyVaultSecrets instance>}``. When a service's
            ``authentication.type`` has a matching entry here, that
            instance is used as-is instead of building one from
            ``region_name``/``vault_url`` — handy for tests, or to reuse
            one client across many ``auto_register()`` calls. Backends
            not overridden this way are still built and cached
            automatically per distinct ``region_name``/``vault_url``.
        config_path : str, optional
            Path to the JSON config file, used only when ``services`` is
            omitted. Defaults to the ``DUCKDUCK_CONFIG`` environment
            variable, or ``"duckduck.json"`` in the current directory.
        on_error : {"raise", "warn"}, optional
            What to do when a single service fails to initialize (bad
            credentials, a missing optional dependency, an unreachable
            host, a config mistake specific to that service, ...):

            - ``"raise"`` (the default): the exception propagates and
              ``auto_register()`` stops immediately — nothing gets
              registered from that call.
            - ``"warn"``: the exception is caught, turned into a
              ``RuntimeWarning`` naming the service and what went wrong
              (always displayed, even on a repeat of the exact same
              failure — Python's own default filter would otherwise
              silently show it only once per process), and that service
              is skipped — every other service is still registered
              normally, so one dead connector doesn't take down the
              rest. Check the return value's keys (or
              ``list_tables()``) to see what actually made it.

            When ``services`` is loaded from a JSON file, a top-level
            ``"on_error"`` key in that file is used as the default —
            still overridden by explicitly passing this parameter.

        Returns
        -------
        dict
            ``{name: instance}`` — to access wrapper methods that didn't
            become a table (e.g. ``instances["sharepoint"].site_by_path``).
            Only successfully-initialized services are present; with
            ``on_error="warn"``, that may be a subset of ``services``.

        Loading from a JSON file
        -------------------------
        ``duck.auto_register()`` with no arguments reads a JSON file
        shaped like::

            {
                "services": {
                    "sharepoint": {
                        "connector": "sharepoint",
                        "hostname": "company.sharepoint.com",
                        "site_path": "/teams/myteam",
                        "authentication": {
                            "type": "aws",
                            "region_name": "us-east-1",
                            "secret_id": "prod/sharepoint"
                        }
                    },
                    "insightvm_dev": {
                        "connector": "insightvm",
                        "authentication": {
                            "type": "local",
                            "host": "dev.local",
                            "username": "a",
                            "password": "b"
                        }
                    }
                }
            }

        The file is looked up, in order: ``config_path`` argument →
        ``DUCKDUCK_CONFIG`` environment variable → ``duckduck.json`` in
        the current directory. See ``duckduck.example.json`` in the repo
        root for a fuller example covering all three ``authentication``
        types.

        Examples
        --------
        Inline, from Python code, pulling from AWS Secrets Manager::

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
                "insightvm": {
                    "authentication": {  # local — fully hardcoded, offline
                        "type": "local",
                        "host": "console.local",
                        "username": "a",
                        "password": "b",
                    },
                },
            })

            duck.sql("SELECT * FROM sharepoint_list_items WHERE list_name = 'Tasks'")
            duck.sql("SELECT * FROM insightvm_assets WHERE hostname = 'web-prod'")

        From a JSON file (``duckduck.json`` in the current directory, or
        ``$DUCKDUCK_CONFIG``)::

            duck = DuckAPI()
            duck.auto_register()
        """
        from .registry import SERVICE_REGISTRY

        file_on_error = None
        if services is None:
            services, file_on_error = self._load_auto_register_config(config_path)

        if on_error is None:
            on_error = file_on_error or "raise"
        if on_error not in ("raise", "warn"):
            raise ValueError(f"on_error must be 'raise' or 'warn' (got '{on_error}').")

        overrides = dict(secrets) if secrets else {}
        backend_cache: Dict[Any, Any] = {}
        instances: Dict[str, Any] = {}

        for name, raw_config in services.items():
            try:
                config = dict(raw_config)
                connector = config.pop("connector", name)
                spec = SERVICE_REGISTRY.get(connector)
                if spec is None:
                    raise ValueError(
                        f"Service '{name}' (connector='{connector}') is not recognized. "
                        f"Available: {', '.join(SERVICE_REGISTRY)}"
                    )

                auth = config.pop("authentication", None)
                if not auth:
                    raise ValueError(f"'{name}': missing 'authentication' block.")
                credentials = self._resolve_authentication(name, auth, overrides, backend_cache)

                instance = spec.factory(credentials, **config)

                for table_name, method_name in spec.tables.items():
                    self.register_api_function(
                        f"{name}_{table_name}", getattr(instance, method_name)
                    )
                for table_name, method_name in spec.streaming_tables.items():
                    self.register_streaming_function(
                        f"{name}_{table_name}", getattr(instance, method_name)
                    )
            except Exception as exc:
                if on_error == "warn":
                    # Every warning here shares the same call site (this line), so
                    # Python's default filter — "show the first occurrence per
                    # (message, category, module, lineno)" — would silently
                    # swallow a repeat of the *same* failure on a later call (e.g.
                    # retrying a script/notebook cell against the same bad
                    # connector). Force this one to always display regardless of
                    # what's already in __warningregistry__ or the caller's own
                    # filters.
                    with warnings.catch_warnings():
                        warnings.simplefilter("always", RuntimeWarning)
                        warnings.warn(
                            f"auto_register: service '{name}' failed to initialize "
                            f"({exc.__class__.__name__}: {exc}) — skipping.",
                            RuntimeWarning,
                            stacklevel=2,
                        )
                    continue
                raise
            instances[name] = instance

        return instances

    def resolve_credentials(self, auth: Dict[str, Any], name: str = "credentials") -> Dict[str, Any]:
        """
        Resolves a standalone ``authentication`` block (same ``local`` /
        ``aws`` / ``azure`` shapes as ``auto_register``) into a plain
        credentials dict — for things configured next to the connectors
        that aren't connectors themselves (e.g. ``duckduck.semantic``'s
        Jev and LLM API keys).
        """
        return self._resolve_authentication(name, auth, {}, {})

    def _resolve_authentication(
        self,
        name: str,
        auth: Dict[str, Any],
        overrides: Dict[str, Any],
        backend_cache: Dict[Any, Any],
    ) -> Dict[str, Any]:
        """
        Turns a service's ``authentication`` block into the credentials
        dict passed to the connector's ``from_secret``. See
        ``auto_register``'s docstring for the shape of each ``type``.
        """
        auth = dict(auth)
        auth_type = auth.pop("type", "local")

        if auth_type == "local":
            return auth

        if auth_type not in ("aws", "azure"):
            raise ValueError(
                f"'{name}': authentication.type must be 'local', 'aws' or "
                f"'azure' (got '{auth_type}')."
            )

        secret_id = auth.pop("secret_id", None)
        if not secret_id:
            raise ValueError(
                f"'{name}': authentication.type='{auth_type}' requires 'secret_id'."
            )

        if auth_type == "aws":
            # profile_name selects a specific AWS account (~/.aws/credentials)
            # when more than one is configured locally.
            backend_key = ("aws", auth.pop("region_name", None), auth.pop("profile_name", None))
        else:
            vault_url = auth.pop("vault_url", None)
            if not vault_url:
                raise ValueError(
                    f"'{name}': authentication.type='azure' requires 'vault_url'."
                )
            # tenant_id pins DefaultAzureCredential to a specific Azure AD
            # tenant when the caller has access to more than one.
            backend_key = ("azure", vault_url, auth.pop("tenant_id", None))

        backend = self._get_secrets_backend(auth_type, backend_key, overrides, backend_cache)
        secret = backend.get_secret(secret_id)

        credentials = dict(secret)
        for key, val in auth.items():
            if isinstance(val, str) and val.startswith(self._SECRET_REF_PREFIX):
                secret_key = val[len(self._SECRET_REF_PREFIX):]
                if secret_key not in secret:
                    raise ValueError(
                        f"'{name}': '{val}' references missing key "
                        f"'{secret_key}' in secret '{secret_id}'."
                    )
                credentials[key] = secret[secret_key]
            else:
                credentials[key] = val
        return credentials

    def _get_secrets_backend(
        self,
        auth_type: str,
        backend_key: tuple,
        overrides: Dict[str, Any],
        backend_cache: Dict[Any, Any],
    ) -> Any:
        """
        Returns the secrets backend for ``auth_type``: an explicit
        override from ``auto_register(secrets=...)`` if one was given for
        this ``auth_type``, otherwise a cached instance built from
        ``backend_key`` — ``("aws", region_name, profile_name)`` or
        ``("azure", vault_url, tenant_id)`` — one instance per distinct
        combination, so services in different regions/vaults/accounts
        don't share a client.
        """
        if auth_type in overrides:
            return overrides[auth_type]

        if backend_key not in backend_cache:
            if auth_type == "aws":
                from .secrets import SecretsManager

                _, region_name, profile_name = backend_key
                backend_cache[backend_key] = SecretsManager(
                    region_name=region_name, profile_name=profile_name
                )
            else:
                from .azure_secrets import AzureKeyVaultSecrets

                _, vault_url, tenant_id = backend_key
                backend_cache[backend_key] = AzureKeyVaultSecrets(
                    vault_url=vault_url, tenant_id=tenant_id
                )

        return backend_cache[backend_key]

    def _find_default_config_file(self) -> Optional[str]:
        """
        Walks from the current directory up to the filesystem root
        looking for ``DEFAULT_CONFIG_PATH`` ("duckduck.json") — so
        ``auto_register()`` finds the project's config even when called
        from a subdirectory of the project, not just its root.
        """
        directory = os.path.abspath(os.getcwd())
        while True:
            candidate = os.path.join(directory, self.DEFAULT_CONFIG_PATH)
            if os.path.isfile(candidate):
                return candidate
            parent = os.path.dirname(directory)
            if parent == directory:
                return None
            directory = parent

    def _load_auto_register_config(
        self,
        config_path: Optional[str],
    ) -> Tuple[Dict[str, Dict[str, Any]], Optional[str]]:
        """
        Resolves the ``services`` dict (and an optional default
        ``on_error``) for ``auto_register()`` from a JSON file when no
        ``services`` dict was passed in code.

        Path lookup order: ``config_path`` argument → ``DUCKDUCK_CONFIG``
        env var → ``DEFAULT_CONFIG_PATH`` ("duckduck.json"), searched from
        the current directory upward through its parents (see
        ``_find_default_config_file``) — so it's found regardless of
        which subdirectory of the project ``auto_register()`` is called
        from.
        """
        if config_path:
            path = config_path
        else:
            path = os.environ.get("DUCKDUCK_CONFIG") or self._find_default_config_file()

        if not path or not os.path.isfile(path):
            raise ValueError(
                f"auto_register() got no 'services' dict and found no config "
                f"file{f' at {path!r}' if path else ''}. Pass services=..., "
                f"pass config_path=..., set the DUCKDUCK_CONFIG environment "
                f"variable, or create '{self.DEFAULT_CONFIG_PATH}' in the "
                f"project directory."
            )

        with open(path, "r", encoding="utf-8") as f:
            config = json.load(f)

        file_services = config.get("services")
        if not file_services:
            raise ValueError(f"Config file '{path}' has no 'services' key.")

        return file_services, config.get("on_error")

    # ------------------------------------------------------------------
    # Inline kwargs parsing:  func(x=1, y="a")
    # ------------------------------------------------------------------

    def _parse_kwargs(self, text: str) -> Dict[str, Any]:
        """
        Converts the inline argument string into a dict.

        ``pr_id=123, limit=100``  →  ``{"pr_id": 123, "limit": 100}``
        """
        if not text.strip():
            return {}

        expr = ast.parse(f"_f_({text})", mode="eval")
        call = expr.body

        if call.args:
            raise ValueError(
                "Use only named parameters. "
                "Example: assets(hostname='web', limit=50)"
            )

        kwargs: Dict[str, Any] = {}
        for kw in call.keywords:
            if kw.arg is None:
                raise ValueError("**kwargs expansion is not allowed in SQL")
            kwargs[kw.arg] = ast.literal_eval(kw.value)

        return kwargs

    # ------------------------------------------------------------------
    # Push-down extraction from SQL
    # ------------------------------------------------------------------

    def _extract_pushdown(self, query: str) -> PushDownContext:
        """Parses the SQL with sqlglot and extracts LIMIT and simple WHERE conditions."""
        ctx = PushDownContext()

        try:
            parsed = sqlglot.parse_one(query, dialect="duckdb")
        except Exception:
            return ctx

        limit_node = parsed.find(exp.Limit)
        if limit_node is not None:
            try:
                ctx.limit = int(limit_node.expression.this)
            except (ValueError, AttributeError, TypeError):
                pass

        where_node = parsed.find(exp.Where)
        if where_node is not None:
            _extract_filters(where_node, ctx)

        ctx.limit_blocker = _limit_blocker(parsed)
        ctx.limit_safe = ctx.limit_blocker is None
        return ctx

    # ------------------------------------------------------------------
    # Merging explicit kwargs + push-down
    # ------------------------------------------------------------------

    def _merge_kwargs(
        self,
        fetch_function,
        pushdown: PushDownContext,
        explicit: Dict[str, Any],
        names: Optional[set] = None,
        allow_limit: bool = True,
    ) -> Dict[str, Any]:
        """
        Builds the final kwargs dict for the function call.

        Priority (highest → lowest):
        1. Explicit kwargs from the SQL call  ``func(x=1)``
        2. WHERE/LIMIT push-down from the SQL (see ``duckduck.pushdown``
           for how each operator maps onto parameters)

        ``names`` — the function's name and its alias in the query: a
        qualified condition (``a.col = 1``) only reaches the function it
        qualifies. LIMIT is pushed only when it can't change the answer:
        the query shape allows it (``limit_safe``), every WHERE condition
        was extractable, and the function consumes all of them.
        """
        return self._plan_call(fetch_function, pushdown, explicit, names, allow_limit)[0]

    def _plan_call(
        self,
        fetch_function,
        pushdown: PushDownContext,
        explicit: Dict[str, Any],
        names: Optional[set] = None,
        allow_limit: bool = True,
    ) -> Tuple[Dict[str, Any], List[str]]:
        """``_merge_kwargs`` plus a human-readable push-down report (one line per decision)."""
        accepted = set(inspect.signature(fetch_function).parameters.keys())
        applicable = [
            c for c in pushdown.conditions
            if c.table is None or names is None or c.table in names
        ]
        merged, consumed = map_conditions(accepted, applicable)
        targets = assign_conditions(accepted, applicable)

        report: List[str] = []
        for c in applicable:
            if c in targets:
                report.append(f"✓ {_describe_condition(c)} → {targets[c]}")
            else:
                report.append(f"✗ {_describe_condition(c)} — {_why_not_pushed(c, accepted)}")
        if not pushdown.complete:
            report.append("✗ part of the WHERE (OR / NOT / IN / functions...) — DuckDB only")

        if pushdown.limit is not None:
            blocker = None
            if not allow_limit:
                blocker = "stream() reads every page"
            elif "limit" not in accepted:
                blocker = "function has no limit parameter"
            elif not pushdown.limit_safe:
                blocker = f"{pushdown.limit_blocker} in the query"
            elif not pushdown.complete:
                blocker = "WHERE has conditions DuckDB must apply first"
            elif len(consumed) != len(pushdown.conditions):
                blocker = "not every WHERE condition reached the source"
            if blocker is None:
                merged["limit"] = pushdown.limit
                report.append(f"✓ LIMIT {pushdown.limit} → limit")
            else:
                report.append(f"✗ LIMIT {pushdown.limit} — {blocker}")

        merged.update(explicit)
        return merged, report

    def _log_call(self, fn_name: str, kwargs: Dict[str, Any], report: List[str]) -> None:
        if not logger.isEnabledFor(logging.INFO):
            return
        args = ", ".join(
            f"{k}=[{len(v)} conditions]" if k == "where" and isinstance(v, list) else f"{k}={short(v, 60)}"
            for k, v in kwargs.items()
        )
        logger.info("▶ %s(%s)", fn_name, args)
        for line in report:
            logger.info("    %s", line)

    #: Words that can follow a table reference but are never its alias.
    _NOT_ALIASES = {
        "where", "join", "inner", "left", "right", "full", "cross", "outer", "on", "using",
        "group", "order", "limit", "offset", "union", "except", "intersect", "having",
        "natural", "window", "qualify", "lateral", "positional", "asof", "anti", "semi",
    }

    def _alias_at(self, text: str, pos: int) -> Optional[str]:
        """The alias written right after a table reference ending at ``pos``, if any."""
        m = re.match(r"\s+(?:AS\s+)?([A-Za-z_][A-Za-z0-9_]*)", text[pos:], re.IGNORECASE)
        if m and m.group(1).lower() not in self._NOT_ALIASES:
            return m.group(1).lower()
        return None

    # ------------------------------------------------------------------
    # Signature validation
    # ------------------------------------------------------------------

    def _validate_arguments(
        self,
        function_name: str,
        fetch_function,
        kwargs: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Validates kwargs against the function's actual signature."""
        sig = inspect.signature(fetch_function)
        try:
            bound = sig.bind(**kwargs)
        except TypeError as err:
            raise ValueError(
                f"Invalid call to '{function_name}': {err}. "
                f"Expected signature: {function_name}{sig}"
            ) from err
        bound.apply_defaults()
        return bound.arguments

    # ------------------------------------------------------------------
    # Conversion to DataFrame
    # ------------------------------------------------------------------

    def _to_dataframe(
        self, data: Any, function_name: str, allow_empty: bool = False
    ) -> pd.DataFrame:
        """
        Normalizes the function's return value into a DataFrame.

        Accepts:
        - list[dict]
        - dict with a ``resources``, ``items``, ``data`` or ``results`` key
        - pd.DataFrame
        - None  (treated as empty, raises an error unless ``allow_empty``)
        """
        if data is None:
            data = []

        if isinstance(data, dict):
            for key in ("resources", "items", "data", "results"):
                if key in data:
                    data = data[key]
                    break
            else:
                data = [data]

        if isinstance(data, pd.DataFrame):
            df = data
        else:
            df = pd.json_normalize(data)

        if (df.empty or len(df.columns) == 0) and not allow_empty:
            raise ValueError(
                f"Table '{function_name}' returned no data. "
                "Cannot determine the columns."
            )

        df.columns = [c.replace(".", "_") for c in df.columns]
        return df

    # ------------------------------------------------------------------
    # Materialization
    # ------------------------------------------------------------------

    def _materialize(
        self,
        function_name: str,
        fetch_function,
        kwargs: Dict[str, Any],
    ) -> tuple:
        """
        Calls the function, converts the result to a DataFrame and
        registers it in DuckDB.

        Returns
        -------
        (table_name, df_columns) : (str, list[str])
        """
        validated = self._validate_arguments(function_name, fetch_function, kwargs)
        started = time.perf_counter()
        data = fetch_function(**validated)
        df = self._to_dataframe(data, function_name)
        logger.info("  %s: %s rows × %s columns in %.2fs", function_name, f"{len(df):,}", len(df.columns),
                    time.perf_counter() - started)

        self._table_counter += 1
        table_name = f"_api_{function_name}_{self._table_counter}"
        self.conn.register(table_name, df)
        return table_name, list(df.columns)

    def fetch(self, name: str, **kwargs) -> pd.DataFrame:
        """
        Calls a registered table's function directly with explicit
        ``kwargs`` — no SQL parsing, no push-down inference — and returns
        its result as a DataFrame (column names normalized the same way
        ``sql()`` does, ``.`` → ``_``).

        Unlike ``sql()``, an empty result is not an error: it comes back
        as an empty DataFrame (with no columns when the function gave no
        way to infer them). Meant for callers that do their own push-down
        planning per source, e.g. ``duckduck.semantic``'s executor.
        """
        fn = self.functions.get(name.lower())
        if fn is None:
            raise KeyError(f"No table registered as '{name}'.")
        validated = self._validate_arguments(name, fn, kwargs)
        started = time.perf_counter()
        df = self._to_dataframe(fn(**validated), name, allow_empty=True)
        logger.info("  %s: %s rows in %.2fs", name, f"{len(df):,}", time.perf_counter() - started)
        return df

    def _strip_where_conditions(self, query: str, keys: set) -> str:
        """
        Removes WHERE conditions that reference columns in ``keys``.

        Used to discard push-down filters that were consumed by the
        function but don't exist as columns in the resulting
        DataFrame — typically structural parameters like ``site_name``,
        ``list_name``.
        """
        if not keys:
            return query
        try:
            tree = sqlglot.parse_one(query, dialect="duckdb")
        except Exception:
            return query

        where = tree.find(exp.Where)
        if not where:
            return query

        def _should_keep(node: exp.Expression) -> bool:
            if isinstance(node, (exp.EQ, exp.Like, exp.GT, exp.LT, exp.GTE, exp.LTE)):
                if isinstance(node.this, exp.Column):
                    if node.this.name.lower() in keys:
                        return False
            return True

        def _rebuild(node: exp.Expression):
            if isinstance(node, exp.And):
                left = _rebuild(node.this)
                right = _rebuild(node.expression)
                if left is None:
                    return right
                if right is None:
                    return left
                return exp.And(this=left, expression=right)
            return node if _should_keep(node) else None

        new_condition = _rebuild(where.this)
        if new_condition is None:
            where.pop()
        else:
            where.set("this", new_condition)

        return tree.sql(dialect="duckdb")

    # ------------------------------------------------------------------
    # Main SQL entry point
    # ------------------------------------------------------------------

    #: Matches "SHOW TABLES", "LIST TABLES", "SHOW ALL TABLES", "LIST ALL
    #: TABLES" (any case, optional trailing ";") — the shortcut ``sql()``
    #: recognizes for ``list_tables()``.
    _LIST_TABLES_RE = re.compile(r"^\s*(SHOW|LIST)(\s+ALL)?\s+TABLES\s*;?\s*$", re.IGNORECASE)

    #: Maps a bundled connector's module name to a human-readable label,
    #: used by ``list_tables()``'s ``source`` column. A function registered
    #: from outside these modules (a hand-rolled wrapper, a lambda in a
    #: notebook, ...) falls back to its own ``__module__``.
    _CONNECTOR_SOURCE_LABELS = {
        "duckduck.sharepoint": "SharePoint (HTTP API)",
        "duckduck.rapid7": "InsightVM (HTTP API)",
        "duckduck.servicenow": "ServiceNow (HTTP API)",
        "duckduck.axonius": "Axonius (HTTP API)",
        "duckduck.database": "SQL database",
        "duckduck.glue": "S3 / Glue Data Catalog",
        "duckduck.blob_storage": "Azure Blob Storage",
        "duckduck.adx": "Azure Data Explorer (KQL)",
    }

    @classmethod
    def _describe_source(cls, fn: Any) -> str:
        """Human-readable label for what kind of thing a registered function is."""
        module = getattr(fn, "__module__", None) or ""
        for prefix, label in cls._CONNECTOR_SOURCE_LABELS.items():
            if module == prefix or module.startswith(prefix + "."):
                return label
        return module or "custom function"

    @staticmethod
    def _describe_endpoint(fn: Any) -> Optional[str]:
        """
        Best-effort "where does this actually point at" for a registered
        function, by inspecting the bound instance behind it (``fn.__self__``
        for a bound method) for the attributes the bundled connectors
        expose. Returns ``None`` when nothing recognizable is found —
        never raises, since this is purely informational.
        """
        instance = getattr(fn, "__self__", None)
        if instance is None:
            return None

        base_url = getattr(instance, "base_url", None)
        if base_url:
            return str(base_url)

        engine = getattr(instance, "engine", None)
        url = getattr(engine, "url", None) if engine is not None else None
        if url is not None:
            render = getattr(url, "render_as_string", None)
            return render(hide_password=True) if render else str(url)

        return None

    @staticmethod
    def _describe_function(fn: Any) -> str:
        """First line of the function's docstring, or "" if it has none."""
        doc = inspect.getdoc(fn)
        if not doc:
            return ""
        return doc.strip().splitlines()[0].strip()

    def list_tables(self) -> pd.DataFrame:
        """
        Lists every table currently registered via
        ``register_api_function()`` / ``auto_register()``.

        Useful to check what's available without digging through the code
        that set up the ``DuckAPI`` instance — especially after
        ``auto_register()``, which can register many tables at once.

        Returns
        -------
        pd.DataFrame
            One row per table, with columns:

            - ``table_name``: name used in ``sql()``/``stream()`` queries.
            - ``source``: what kind of thing this is — e.g. ``"SharePoint
              (HTTP API)"``, ``"SQL database"``, ``"S3 / Glue Data
              Catalog"`` for a bundled connector; the function's own
              ``__module__`` for anything else (a hand-rolled wrapper, a
              notebook lambda, ...).
            - ``endpoint``: best-effort "where this actually points at" —
              e.g. a SharePoint/ServiceNow/InsightVM/Axonius wrapper's
              ``base_url``, or a ``database`` connector's connection URL
              (password redacted). ``None`` when nothing recognizable
              could be found (most custom functions, and the ``glue``/
              ``blob_storage`` connectors, which don't have one fixed
              endpoint to show).
            - ``streaming``: whether ``stream()`` also works for this
              table (i.e. a matching ``register_streaming_function()``
              call was made).
            - ``signature``: the registered function's signature, showing
              which parameters are available for inline calls
              (``func(param=val)``) or ``WHERE`` push-down.
            - ``description``: first line of the function's docstring —
              every bundled connector method documents what it does and
              which filters push down, so this is usually a real
              one-line summary, not just a repeat of the name.

        Examples
        --------
        ::

            duck.auto_register({"sharepoint": {"secret_id": "..."}}, secrets=secrets)
            duck.list_tables().df()  # or: duck.sql("SHOW TABLES").df()
        """
        rows = [
            {
                "table_name": name,
                "source": self._describe_source(fn),
                "endpoint": self._describe_endpoint(fn),
                "streaming": name in self._streaming_functions,
                "signature": str(inspect.signature(fn)),
                "description": self._describe_function(fn),
            }
            for name, fn in self.functions.items()
        ]
        columns = ["table_name", "source", "endpoint", "streaming", "signature", "description"]
        return pd.DataFrame(rows, columns=columns)

    def sql(self, query: str):
        """
        Executes a SQL query, replacing references to registered
        functions with the corresponding DataFrames.

        Supports:
        - ``FROM func``                → automatic WHERE/LIMIT push-down
        - ``FROM func(struct_id=1)``   → structural parameter (builds URL/path)
                                         + WHERE/LIMIT push-down
        - ``JOIN func(struct_id=1)``   → same as above
        - Multiple references to the same function or different functions
        - ``SHOW TABLES`` / ``LIST TABLES`` (optionally ``ALL``) → shortcut
          for ``list_tables()``, listing every registered table

        Structural parameters (``site_name``, ``list_name``, etc.) can
        appear either inline or in the ``WHERE`` clause. When they're in
        the WHERE clause and aren't columns of the result, they're
        automatically removed from the query before DuckDB executes it.

        Returns
        -------
        duckdb.DuckDBPyRelation
            DuckDB relation. Use ``.df()`` to get a DataFrame.
        """
        if self._LIST_TABLES_RE.match(query):
            self.conn.register("_duckduck_tables", self.list_tables())
            return self.conn.sql("SELECT * FROM _duckduck_tables")

        pushdown = self._extract_pushdown(query)
        rewritten = query
        structural_used: set = set()  # WHERE filters consumed that aren't columns
        started = time.perf_counter()
        sources = 0

        for fn_name, fn in self.functions.items():

            # ---- 1. func(args) ----------------------------------------
            with_args_pat = re.compile(
                rf"\b{re.escape(fn_name)}\s*\((.*?)\)",
                flags=re.IGNORECASE | re.DOTALL,
            )

            while m := with_args_pat.search(rewritten):
                explicit = self._parse_kwargs(m.group(1))
                names = {fn_name, self._alias_at(rewritten, m.end())} - {None}
                kwargs, report = self._plan_call(fn, pushdown, explicit, names)
                self._log_call(fn_name, kwargs, report)
                tname, df_cols = self._materialize(fn_name, fn, kwargs)
                sources += 1
                # WHERE filters that reached the function but aren't result columns
                structural_used.update(
                    k for k in pushdown.filters
                    if k in kwargs and k not in df_cols
                )
                rewritten = rewritten[: m.start()] + tname + rewritten[m.end() :]

            # ---- 2. FROM/JOIN func  (no parentheses) ------------------
            bare_pat = re.compile(
                rf"\b(FROM|JOIN)\s+{re.escape(fn_name)}\b(?!\s*\()",
                flags=re.IGNORECASE,
            )

            while m := bare_pat.search(rewritten):
                names = {fn_name, self._alias_at(rewritten, m.end())} - {None}
                kwargs, report = self._plan_call(fn, pushdown, {}, names)
                self._log_call(fn_name, kwargs, report)
                tname, df_cols = self._materialize(fn_name, fn, kwargs)
                sources += 1
                structural_used.update(
                    k for k in pushdown.filters
                    if k in kwargs and k not in df_cols
                )
                op = m.group(1)
                rewritten = rewritten[: m.start()] + f"{op} {tname}" + rewritten[m.end() :]

        if structural_used:
            rewritten = self._strip_where_conditions(rewritten, structural_used)

        if sources:
            logger.info("%d source(s) fetched in %.2fs — DuckDB runs the rest of the query", sources,
                        time.perf_counter() - started)
            logger.debug("    %s", " ".join(rewritten.split()))
        return self.conn.sql(rewritten)

    # ------------------------------------------------------------------
    # Streaming (incremental pagination)
    # ------------------------------------------------------------------

    def stream(self, query: str):
        """
        Executes the query page by page, ``yield``ing a ``pd.DataFrame``
        per page as each request completes.

        Unlike ``sql()``, it doesn't wait for all the data before
        returning the first result — useful for large datasets or for
        showing progress in Jupyter.

        Requires that the function has also been registered via
        ``register_streaming_function()``.

        Limitations
        -----------
        - Supports only one table per query (no JOINs between functions).
        - ``LIMIT N`` and ``WHERE`` are applied **per page** (not globally).
          For a global LIMIT, use ``sql()`` with the desired LIMIT.
        - ``ORDER BY`` and aggregations operate per chunk, not over the total.

        Example
        -------
        ::

            duck.register_api_function("assets", r7.assets)
            duck.register_streaming_function("assets", r7.iter_assets)

            for chunk in duck.stream("SELECT * FROM assets WHERE severity = 'critical'"):
                display(chunk)   # shows up as each page arrives

        Yields
        ------
        pd.DataFrame
            Query result applied on top of each page from the API.
        """
        pushdown = self._extract_pushdown(query)

        for fn_name, iter_fn in self._streaming_functions.items():
            if not re.search(rf"\b{re.escape(fn_name)}\b", query, re.IGNORECASE):
                continue

            # Explicit kwargs from the inline call
            explicit: Dict[str, Any] = {}
            inline_pat = re.compile(
                rf"\b{re.escape(fn_name)}\s*\((.*?)\)",
                flags=re.IGNORECASE | re.DOTALL,
            )
            if m := inline_pat.search(query):
                explicit = self._parse_kwargs(m.group(1))

            # WHERE push-down onto the generator's own parameters (no limit:
            # stream iterates every page)
            kwargs, report = self._plan_call(iter_fn, pushdown, explicit, {fn_name}, allow_limit=False)
            self._log_call(fn_name, kwargs, report)

            # Rewrites the query, replacing func(...) / func with the chunk table name
            chunk_table = f"_stream_{fn_name}"
            chunk_query = inline_pat.sub(chunk_table, query)
            bare_pat = re.compile(
                rf"\b(FROM|JOIN)\s+{re.escape(fn_name)}\b(?!\s*\()",
                flags=re.IGNORECASE,
            )
            chunk_query = bare_pat.sub(rf"\1 {chunk_table}", chunk_query)

            started, chunks, rows_in, rows_out = time.perf_counter(), 0, 0, 0
            for chunk_df in iter_fn(**kwargs):
                if chunk_df.empty:
                    continue
                self.conn.register(chunk_table, chunk_df)
                result = self.conn.sql(chunk_query).df()
                chunks += 1
                rows_in += len(chunk_df)
                rows_out += len(result)
                logger.info("  %s: chunk %d · %s rows read · %s kept · %.1fs elapsed", fn_name, chunks,
                            f"{rows_in:,}", f"{rows_out:,}", time.perf_counter() - started)
                yield result

            return

        raise ValueError(
            f"No streaming function registered for this query.\n"
            f"Use register_streaming_function() to register a generator."
        )

    # ------------------------------------------------------------------

    def close(self) -> None:
        """Closes the DuckDB connection."""
        self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

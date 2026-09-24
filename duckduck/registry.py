"""
Central registry of the API wrappers supported by
``DuckAPI.auto_register`` (see ``core.py``).

Each entry maps a "type" (``"sharepoint"``, ``"insightvm"``, ``"database"``,
``"servicenow"``, ``"axonius"``, ``"glue"``, ``"blob_storage"``, ``"adx"``,
``"files"``, ``"python"``) to:

- ``factory``          : classmethod that builds the instance from a
                          credentials dict (``Wrapper.from_secret``)
- ``tables``            : ``{table_name: method_name}`` for ``sql()``
- ``streaming_tables``  : ``{table_name: method_name}`` for ``stream()``

When adding a new wrapper (see the "Adding a new API wrapper" section of
CLAUDE.md), also add a ``from_secret`` to it and an entry here so it
becomes available in ``DuckAPI.auto_register()``.
"""

from typing import Any, Callable, Dict, NamedTuple, Optional, Tuple

from .adx import DataExplorer
from .axonius import Axonius
from .blob_storage import BlobStorage
from .database import SQLDatabase
from .glue import GlueTable
from .local_files import LocalFiles
from .python_source import PythonSource
from .rapid7 import InsightVM
from .servicenow import ServiceNow
from .sharepoint import SharePoint


class ServiceSpec(NamedTuple):
    factory: Callable[..., Any]
    tables: Dict[str, str]
    streaming_tables: Dict[str, str]
    #: Method returning ``{table_name: callable}`` for connectors whose
    #: tables are only known at runtime (one per file, per module function).
    dynamic_tables: Optional[str] = None
    #: False for local sources, where an ``authentication`` block is optional.
    requires_authentication: bool = True
    #: Config options holding filesystem paths — resolved against the JSON
    #: config file's directory when the config comes from a file.
    path_options: Tuple[str, ...] = ()


SERVICE_REGISTRY: Dict[str, ServiceSpec] = {
    "sharepoint": ServiceSpec(
        factory=SharePoint.from_secret,
        tables={
            "sites": "sites",
            "lists": "lists",
            "list_columns": "list_columns",
            "list_items": "list_items",
            "drives": "drives",
            "drive_items": "drive_items",
            "search_files": "search_files",
            "file_versions": "file_versions",
        },
        streaming_tables={
            "sites": "iter_sites",
            "lists": "iter_lists",
            "list_items": "iter_list_items",
            "drive_items": "iter_drive_items",
        },
    ),
    "insightvm": ServiceSpec(
        factory=InsightVM.from_secret,
        tables={
            "assets": "assets",
            "vulnerabilities": "vulnerabilities",
            "asset_vulnerabilities": "asset_vulnerabilities",
            "sites": "sites",
            "scan_engines": "scan_engines",
            "scans": "scans",
            "report_templates": "report_templates",
            "reports": "reports",
            "tags": "tags",
            "asset_groups": "asset_groups",
            "users": "users",
            "policies": "policies",
            "policy_rules": "policy_rules",
            "remediation_projects": "remediation_projects",
        },
        streaming_tables={
            "assets": "iter_assets",
            "vulnerabilities": "iter_vulnerabilities",
            "asset_vulnerabilities": "iter_asset_vulnerabilities",
            "sites": "iter_sites",
            "scans": "iter_scans",
            "policy_rules": "iter_policy_rules",
        },
    ),
    "database": ServiceSpec(
        factory=SQLDatabase.from_secret,
        tables={
            "table": "table",
            "query": "query",
        },
        streaming_tables={
            "table": "iter_table",
            "query": "iter_query",
        },
    ),
    "servicenow": ServiceSpec(
        factory=ServiceNow.from_secret,
        tables={
            "table": "table",
            "incidents": "incidents",
            "problems": "problems",
            "change_requests": "change_requests",
            "users": "users",
            "cmdb_ci": "cmdb_ci",
        },
        streaming_tables={
            "table": "iter_table",
            "incidents": "iter_incidents",
            "problems": "iter_problems",
            "change_requests": "iter_change_requests",
            "users": "iter_users",
            "cmdb_ci": "iter_cmdb_ci",
        },
    ),
    "axonius": ServiceSpec(
        factory=Axonius.from_secret,
        tables={
            "devices": "devices",
            "users": "users",
        },
        streaming_tables={
            "devices": "iter_devices",
            "users": "iter_users",
        },
    ),
    "glue": ServiceSpec(
        factory=GlueTable.from_secret,
        tables={
            "table": "table",
            "path": "path",
            "databases": "databases",
            "tables": "tables",
            "columns": "columns",
        },
        streaming_tables={
            "table": "iter_table",
        },
    ),
    "adx": ServiceSpec(
        factory=DataExplorer.from_secret,
        tables={
            "table": "table",
            "query": "query",
            "tables": "tables",
            "columns": "columns",
        },
        streaming_tables={
            "table": "iter_table",
        },
    ),
    "files": ServiceSpec(
        factory=LocalFiles.from_secret,
        tables={"tables": "tables", "columns": "columns"},
        streaming_tables={},
        dynamic_tables="table_functions",
        requires_authentication=False,
        path_options=("path",),
    ),
    "python": ServiceSpec(
        factory=PythonSource.from_secret,
        tables={},
        streaming_tables={},
        dynamic_tables="table_functions",
        requires_authentication=False,
        path_options=("module",),
    ),
    "blob_storage": ServiceSpec(
        factory=BlobStorage.from_secret,
        tables={
            "table": "table",
        },
        streaming_tables={
            "table": "iter_table",
        },
    ),
}

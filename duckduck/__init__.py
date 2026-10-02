"""
duckduck — SQL over APIs, files and databases.

    core.py, addresses.py, views.py, cache.py, kql.py, system.py, spark.py   the engine (DuckAPI and around it)
    connectors/   api/ (HTTP APIs) · lake/ (files on S3 / Azure) · databases/ (SQL, ADX, Athena) · local/ · registry.py
    common/       what connectors and the engine are built with: push-down, pages, retries, progress, logs, kinds
    secrets/      credentials from AWS Secrets Manager / Azure Key Vault
    pipeline/     ingestion jobs and the lake;  pgserver/  PostgreSQL wire protocol;  semantic/  questions in words
"""

from ._moved import install as _install_moved_names

_install_moved_names()  # duckduck.servicenow, duckduck.pushdown… still import (see _moved.py)


def __getattr__(name):  # duckduck.pushdown as an attribute, as before the folders
    from ._moved import MOVED

    if f"duckduck.{name}" in MOVED:
        import importlib

        return importlib.import_module(f"duckduck.{name}")
    raise AttributeError(f"module 'duckduck' has no attribute {name!r}")

from .connectors.api.axonius import Axonius
from .connectors.api.insightvm import InsightVM
from .connectors.api.nvd import NVD
from .connectors.api.restcountries import RestCountries
from .connectors.api.servicenow import ServiceNow
from .connectors.api.sharepoint import SharePoint
from .connectors.databases.adx import DataExplorer
from .connectors.databases.sql import SQLDatabase
from .connectors.lake.blob_storage import BlobStorage
from .connectors.lake.glue import GlueTable
from .core import DuckAPI, PushDownContext
from .secrets.aws import SecretsManager
from .secrets.azure import AzureKeyVaultSecrets

__all__ = [
    "DataExplorer",
    "DuckAPI",
    "PushDownContext",
    "InsightVM",
    "SharePoint",
    "SQLDatabase",
    "ServiceNow",
    "Axonius",
    "GlueTable",
    "BlobStorage",
    "NVD",
    "RestCountries",
    "SecretsManager",
    "AzureKeyVaultSecrets",
]

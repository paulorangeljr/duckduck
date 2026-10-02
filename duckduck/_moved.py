"""
The old flat module names (``duckduck.servicenow``, ``duckduck.pushdown``…) still import: each one is the module
at its new place, the same object (``import duckduck.servicenow`` is ``duckduck.connectors.api.servicenow``), so
scripts, notebooks and configs written before the folders keep working. New code imports the new names.
"""

import importlib
import importlib.abc
import importlib.util
import sys

#: old name → where the module lives now
MOVED = {
    "duckduck.servicenow": "duckduck.connectors.api.servicenow",
    "duckduck.rapid7": "duckduck.connectors.api.insightvm",
    "duckduck.axonius": "duckduck.connectors.api.axonius",
    "duckduck.sharepoint": "duckduck.connectors.api.sharepoint",
    "duckduck.nvd": "duckduck.connectors.api.nvd",
    "duckduck.restcountries": "duckduck.connectors.api.restcountries",
    "duckduck.airflow": "duckduck.connectors.api.airflow",
    "duckduck.glue": "duckduck.connectors.lake.glue",
    "duckduck.blob_storage": "duckduck.connectors.lake.blob_storage",
    "duckduck.lakehouse": "duckduck.connectors.lake.lakehouse",
    "duckduck.s3layout": "duckduck.connectors.lake.s3layout",
    "duckduck.database": "duckduck.connectors.databases.sql",
    "duckduck.adx": "duckduck.connectors.databases.adx",
    "duckduck.athena": "duckduck.connectors.databases.athena",
    "duckduck.local_files": "duckduck.connectors.local.files",
    "duckduck.python_source": "duckduck.connectors.local.python_source",
    "duckduck.registry": "duckduck.connectors.registry",
    "duckduck.azure_secrets": "duckduck.secrets.azure",
    "duckduck.kinds": "duckduck.common.kinds",
    "duckduck.logs": "duckduck.common.logs",
    "duckduck.progress": "duckduck.common.progress",
    "duckduck.pushdown": "duckduck.common.pushdown",
    "duckduck.retry": "duckduck.common.retry",
    "duckduck.slicing": "duckduck.common.slicing",
    "duckduck.sparkplan": "duckduck.common.sparkplan",
}


class _Alias(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    def find_spec(self, fullname, path=None, target=None):
        if fullname in MOVED:
            return importlib.util.spec_from_loader(fullname, self)
        return None

    def create_module(self, spec):
        return importlib.import_module(MOVED[spec.name])  # the module itself, under its old name too

    def exec_module(self, module):
        pass  # already executed at its new place


def install() -> None:
    if not any(isinstance(f, _Alias) for f in sys.meta_path):
        sys.meta_path.append(_Alias())

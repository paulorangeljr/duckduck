"""The package's folders, and the flat names from before them still importing as the same modules."""

import importlib
import os

import duckduck
from duckduck._moved import MOVED

PACKAGE = os.path.dirname(duckduck.__file__)


def test_every_old_name_is_the_module_at_its_new_place():
    for old, new in MOVED.items():
        assert importlib.import_module(old) is importlib.import_module(new), old
        assert not os.path.exists(os.path.join(PACKAGE, old.split(".", 1)[1] + ".py")), f"{old} left behind"


def test_old_style_imports_and_attributes_keep_working():
    from duckduck.pushdown import Condition
    from duckduck.secrets import AzureKeyVaultSecrets, SecretsManager  # noqa: F401
    from duckduck.servicenow import ServiceNow

    assert Condition is duckduck.common.pushdown.Condition and ServiceNow is duckduck.ServiceNow
    assert duckduck.progress is importlib.import_module("duckduck.common.progress")


def test_connectors_sit_in_a_folder_by_kind():
    kinds = {k: sorted(f[:-3] for f in os.listdir(os.path.join(PACKAGE, "connectors", k))
                       if f.endswith(".py") and f != "__init__.py") for k in ("api", "lake", "databases", "local")}
    assert kinds == {
        "api": ["airflow", "axonius", "insightvm", "nvd", "restcountries", "servicenow", "sharepoint"],
        "lake": ["blob_storage", "glue", "lakehouse", "s3layout"],
        "databases": ["adx", "athena", "sql"],
        "local": ["files", "python_source"],
    }
    from duckduck.connectors.registry import SERVICE_REGISTRY

    for name, spec in SERVICE_REGISTRY.items():  # every registered connector lives under connectors/
        owner = getattr(spec.factory, "__self__", spec.factory)
        assert owner.__module__.startswith("duckduck.connectors."), (name, owner.__module__)

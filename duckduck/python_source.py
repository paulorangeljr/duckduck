"""
Tables from a Python module you write — for synthetic data, fixtures, or
simulating an API before its real connector exists.

The module exposes its tables either as

- a factory function (``factory`` in the config, default ``tables``)
  returning ``{table_name: callable}`` — called with the config's
  ``kwargs``; or
- a module-level ``TABLES = {table_name: callable}`` dict.

Each callable follows the normal DuckAPI contract: returns ``list[dict]``
or a ``DataFrame``, and any column-filter / ``*_ilike`` / ``where`` /
``limit`` parameter in its signature gets push-down exactly like a real
connector's — so the same SQL behaves the same once you switch the
config to the real source.

Via ``auto_register()``::

    "synthetic": {"connector": "python", "module": "./synthetic.py",
                  "kwargs": {"rows": 5000}, "table_prefix": ""}
"""

import importlib
import importlib.util
import os
import re
from typing import Any, Callable, Dict, Optional

_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def load_module(module: str):
    """A ``.py`` file path or a dotted module name."""
    if module.endswith(".py") or os.path.sep in module or os.path.isfile(module):
        path = os.path.abspath(module)
        if not os.path.isfile(path):
            raise ValueError(f"module file '{module}' not found")
        # Under duckduck.python_source.* so list_tables() labels its tables
        # "Python module"; never added to sys.modules, so no clash between
        # two files with the same stem.
        stem = re.sub(r"[^0-9a-zA-Z_]", "_", os.path.splitext(os.path.basename(path))[0])
        spec = importlib.util.spec_from_file_location(f"{__name__}.{stem}", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    return importlib.import_module(module)


class PythonSource:
    """
    Parameters
    ----------
    module : str
        ``.py`` file path (relative paths resolve against the current
        directory — or the JSON config file's directory under
        ``auto_register()``) or a dotted, importable module name.
    factory : str, optional
        Name of the function returning ``{table: callable}``. Default:
        ``tables`` if the module defines it, else the ``TABLES`` dict.
    kwargs : dict, optional
        Passed to the factory (row counts, seeds, a fixed ``now``...).
    """

    def __init__(self, module: str, factory: Optional[str] = None, kwargs: Optional[Dict[str, Any]] = None):
        mod = load_module(module)
        self.base_url = getattr(mod, "__file__", None) or module
        if factory is not None:
            if not callable(getattr(mod, factory, None)):
                raise ValueError(f"'{module}' has no function '{factory}'")
            result = getattr(mod, factory)(**(kwargs or {}))
        elif callable(getattr(mod, "tables", None)):
            result = mod.tables(**(kwargs or {}))
        elif isinstance(getattr(mod, "TABLES", None), dict):
            if kwargs:
                raise ValueError(f"'{module}' exposes a TABLES dict, which takes no kwargs — use a factory function")
            result = mod.TABLES
        else:
            raise ValueError(
                f"'{module}' must define a `tables(**kwargs)` function (or pass factory=...) "
                f"or a TABLES dict, returning {{table_name: callable}}"
            )
        if not isinstance(result, dict):
            raise ValueError(f"'{module}' factory must return a dict of {{table_name: callable}}, got {type(result).__name__}")
        for name, fn in result.items():
            if not isinstance(name, str) or not _IDENTIFIER.match(name):
                raise ValueError(f"'{module}': table name {name!r} isn't a valid SQL identifier")
            if not callable(fn):
                raise ValueError(f"'{module}': table '{name}' isn't callable")
        self._tables: Dict[str, Callable] = dict(result)
        for fn in self._tables.values():
            # list_tables()'s "endpoint" column: which file the table comes from
            if getattr(fn, "__self__", None) is None and not hasattr(fn, "base_url"):
                try:
                    fn.base_url = self.base_url
                except (AttributeError, TypeError):
                    pass

    @classmethod
    def from_secret(cls, secret: Dict, **overrides) -> "PythonSource":
        """No credentials involved — everything comes from the service config."""
        merged = {**secret, **overrides}
        if "module" not in merged:
            raise ValueError("The 'python' connector needs a 'module' (a .py path or a module name).")
        return cls(**merged)

    def table_functions(self) -> Dict[str, Callable]:
        """``{table_name: callable}`` — what ``auto_register()`` registers."""
        return dict(self._tables)

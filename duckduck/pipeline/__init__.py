"""
Declarative pipelines over duckduck: a JSON file says where the SQL is (a
notebook, a ``.sql`` file or the query itself), which engine runs it, the key,
the targets and the sip — the few rows followed through every step.
See ``spec`` (the file), ``analysis`` (views and the key's way), ``sip``,
``engines`` and ``runner``.
"""

from .analysis import Plan
from .domains import Domain, Pipelines, Runs
from .notebook import PipelineSession, notebook
from .runner import PipelineRun, plan_pipeline, run_pipeline
from .sip import read_sip
from .spec import PipelineError, PipelineSpec, load_spec

__all__ = ["Domain", "PipelineError", "PipelineRun", "PipelineSession", "PipelineSpec", "Pipelines", "Plan", "Runs",
           "load_spec", "notebook", "plan_pipeline", "read_sip", "run_pipeline"]

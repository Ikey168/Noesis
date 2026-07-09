"""
NeuroNews ML utilities.

Scope note (MLOps consolidation): the experiment tracker, model registry, and
training / data-manifest machinery live in **``services/mlops``** — the real,
MLflow-backed stack the RAG answerer, the ``ask`` API route, the Airflow MLflow
callbacks, and the RAG indexer actually use. The former in-memory duplicates
under ``src/ml`` (``mlops/``, ``registry/``, ``training/``) and the toy
metric / feature / monitoring shims were removed; use ``services.mlops`` for any
tracking, registry, or manifest need.

What remains here are self-contained utilities:

- ``preprocessing.text_cleaner`` - whitespace / HTML normalisation for text,
- ``validation.input_validator`` - request-shape validation for inference,
- ``fake_news_detection`` / ``models`` / ``inference`` - the veracity classifier
  (heavy model deps are imported lazily; see that module's docstring).
"""

__version__ = "2.0.0"

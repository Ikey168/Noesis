"""Noesis API route package.

Route modules are intentionally not imported here.  Importing this package is a
common operation for unit tests, CLIs, and MCP workers; eagerly importing every
router made those lightweight consumers require all optional API dependencies.
The application factory imports and mounts the routes it actually enables.
"""

__all__: list[str] = []

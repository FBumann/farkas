"""Opt-in linopy compatibility layer.

Importing this module patches ``linopy.Model`` with ``.from_yaml()`` and the
``.yaml`` accessor — the legacy eager path. It requires the ``[oracle]``
extra (linopy, xarray).

The eager builder is not a runtime product lane: it exists as the
compatibility surface for Python-built linopy models and as the correctness
oracle the streaming engine is differentially tested against
(ARCHITECTURE.md). New code should use the native API::

    import linopy_yaml as ly
    sol = ly.solve("model.yaml", sources={...})
"""

from linopy_yaml._patch import apply_patches

apply_patches()

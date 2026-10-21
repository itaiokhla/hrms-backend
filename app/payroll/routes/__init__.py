"""Payroll v2 routers.

Every module in this package that defines `router` (and optionally `public`, for unauthenticated
endpoints) is included automatically, in module-name order, ahead of the legacy payroll router
(app.routes.payroll owns /payroll/{payroll_id}, which would otherwise shadow /payroll/runs).
"""

import importlib
import pkgutil

ROUTERS = []

for _module in sorted(pkgutil.iter_modules(__path__), key=lambda m: m.name):
    if _module.name.startswith("_"):
        continue
    _mod = importlib.import_module(f"{__name__}.{_module.name}")
    for _name in ("public", "router"):
        _router = getattr(_mod, _name, None)
        if _router is not None:
            ROUTERS.append(_router)

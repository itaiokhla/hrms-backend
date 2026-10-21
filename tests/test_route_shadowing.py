"""Route shadowing contract (audit 04 §1, risk 1).

Starlette matches routes in registration order on the path pattern only; a FastAPI `int` annotation
does not narrow the match. A route registered earlier whose template also matches some path of a
later route with a common method makes that path of the later route unreachable (for example
legacy GET /api/v1/payroll/{payroll_id} registered before a v2 GET /api/v1/payroll/runs). Literal
before parameter is the intended order and is fine; the reverse, or an exact duplicate, fails.

A captured path counts only if the later route would have accepted it: where the earlier route has
a literal segment and the later one a parameter, the literal must pass the later parameter's
FastAPI validation (`approvals` is never a valid legacy `payroll_id: int`, so GET
/payroll/approvals/{request_id} hides no valid request of GET /payroll/{payroll_id}/slip).
"""

import re
from typing import List, Tuple

from fastapi import APIRouter, FastAPI, Path
from fastapi.routing import APIRoute
from starlette.convertors import CONVERTOR_TYPES
from starlette.routing import Route

_PARAM = re.compile(r"^\{(?P<name>[a-zA-Z_][a-zA-Z0-9_]*)(?::(?P<convertor>[a-zA-Z_][a-zA-Z0-9_]*))?\}$")

# (method, earlier path, later path) -> why the conflict is proven unreachable today.
# Every entry needs an explanation; none is needed today.
ALLOWED: dict = {}


def _segments(path: str) -> List[Tuple[str, str, str]]:
    """('literal', text, '') or ('param', convertor regex, name) per path segment."""
    parts = []
    for segment in path.strip("/").split("/"):
        match = _PARAM.match(segment)
        if match:
            convertor = CONVERTOR_TYPES[match.group("convertor") or "str"]
            parts.append(("param", convertor.regex, match.group("name")))
        else:
            parts.append(("literal", segment, ""))
    return parts


def _accepts(route: Route, name: str, regex: str, value: str) -> bool:
    """Whether `route` would accept `value` for path parameter `name` (convertor, then FastAPI validation)."""
    if not re.fullmatch(regex, value):
        return False
    if isinstance(route, APIRoute):
        for field in route.dependant.path_params:
            if field.name == name:
                _, errors = field.validate(value, {}, loc=("path", name))
                return not errors
    return True


def _shadows(earlier: Route, later: Route) -> bool:
    """True when some request path that `later` would accept is captured by `earlier`."""
    a, b = _segments(earlier.path), _segments(later.path)
    if any(kind == "param" and regex == ".*" for kind, regex, _ in a + b):
        return bool(earlier.path_regex.match(later.path))  # {name:path}: fall back to matching the template
    if len(a) != len(b):
        return False
    narrower_later = False
    for (kind_a, value_a, _), (kind_b, value_b, name_b) in zip(a, b):
        if kind_a == "literal" and kind_b == "literal":
            if value_a != value_b:
                return False
        elif kind_a == "param" and kind_b == "literal":
            if not re.fullmatch(value_a, value_b):
                return False
            narrower_later = True
        elif kind_a == "literal" and kind_b == "param":
            if not _accepts(later, name_b, value_b, value_a):
                return False
    if narrower_later:
        return True
    # Same shape: an exact duplicate is fully unreachable; a later route that is broader somewhere is not.
    return all(kind_a == kind_b for (kind_a, _, _), (kind_b, _, _) in zip(a, b))


def shadowing_pairs(routes) -> List[Tuple[str, str, str]]:
    plain = [r for r in routes if isinstance(r, Route)]
    found = []
    for i, earlier in enumerate(plain):
        for later in plain[i + 1:]:
            for method in sorted((earlier.methods or set()) & (later.methods or set())):
                if _shadows(earlier, later):
                    found.append((method, earlier.path, later.path))
    return found


def _app(*paths: Tuple[str, str]) -> FastAPI:
    application = FastAPI()
    for method, path in paths:
        application.add_api_route(path, lambda: None, methods=[method])
    return application


# ---------------------------------------------------------------------------
# The detector itself
# ---------------------------------------------------------------------------
def test_detector_flags_parameter_before_literal_and_duplicates():
    legacy_first = _app(("GET", "/api/v1/payroll/{payroll_id}"), ("GET", "/api/v1/payroll/runs"))
    assert ("GET", "/api/v1/payroll/{payroll_id}", "/api/v1/payroll/runs") in shadowing_pairs(legacy_first.routes)
    v2_param = _app(("GET", "/api/v1/payroll/{run_id}/slip.pdf"), ("GET", "/api/v1/payroll/{payroll_id}/slip.pdf"))
    assert shadowing_pairs(v2_param.routes)  # exact duplicate shape
    catch_all = _app(("POST", "/api/v1/legal-entities/{entity_id}/{action}"),
                     ("POST", "/api/v1/legal-entities/{entity_id}/funding-accounts"))
    assert shadowing_pairs(catch_all.routes)
    partial = _app(("GET", "/x/{a}/b"), ("GET", "/x/c/{d}"))  # /x/c/b reaches the first route only
    assert shadowing_pairs(partial.routes)
    wildcard = _app(("GET", "/files/{rest:path}"), ("GET", "/files/report.csv"))
    assert shadowing_pairs(wildcard.routes)


def test_detector_ignores_captured_paths_the_later_route_would_reject():
    """Legacy `payroll_id: int` never accepts `approvals`, so nothing valid is hidden; a `str` id would be."""
    typed = FastAPI()
    typed.add_api_route("/api/v1/payroll/approvals/{request_id}", lambda request_id: None, methods=["GET"])

    def legacy_slip(payroll_id: int):
        return None

    typed.add_api_route("/api/v1/payroll/{payroll_id}/slip", legacy_slip, methods=["GET"])
    assert not shadowing_pairs(typed.routes)

    def text_slip(payroll_id: str):
        return None

    loose = FastAPI()
    loose.add_api_route("/api/v1/payroll/approvals/{request_id}", lambda request_id: None, methods=["GET"])
    loose.add_api_route("/api/v1/payroll/{payroll_id}/slip", text_slip, methods=["GET"])
    assert shadowing_pairs(loose.routes) == [
        ("GET", "/api/v1/payroll/approvals/{request_id}", "/api/v1/payroll/{payroll_id}/slip")
    ]

    def positive(item_id: int = Path(ge=1)):
        return None

    def numeric(partial_literal: str) -> FastAPI:
        application = FastAPI()  # /items/x/<partial_literal> reaches the first route only
        application.add_api_route(f"/items/{{a}}/{partial_literal}", lambda a: None, methods=["GET"])
        application.add_api_route("/items/x/{item_id}", positive, methods=["GET"])
        return application

    assert not shadowing_pairs(numeric("0").routes)  # ge=1 rejects 0, so nothing valid is hidden
    assert shadowing_pairs(numeric("7").routes) == [("GET", "/items/{a}/7", "/items/x/{item_id}")]


def test_detector_accepts_literal_before_parameter_other_methods_and_typed_mismatches():
    assert not shadowing_pairs(_app(("GET", "/api/v1/payroll/runs"), ("GET", "/api/v1/payroll/{payroll_id}")).routes)
    assert not shadowing_pairs(_app(("GET", "/api/v1/payroll/{payroll_id}"), ("POST", "/api/v1/payroll/runs")).routes)
    assert not shadowing_pairs(_app(("GET", "/a/{id:int}"), ("GET", "/a/runs")).routes)
    assert not shadowing_pairs(_app(("GET", "/a/{id}"), ("GET", "/a/{id}/b")).routes)
    assert not shadowing_pairs(_app(("GET", "/a/b"), ("GET", "/a/c")).routes)
    assert not shadowing_pairs(_app(("GET", "/a/b/{x}"), ("GET", "/a/{y}/{x}")).routes)  # later is broader


# ---------------------------------------------------------------------------
# The real application
# ---------------------------------------------------------------------------
def test_no_registered_route_shadows_a_later_one(app):
    conflicts = [pair for pair in shadowing_pairs(app.routes) if pair not in ALLOWED]
    assert not conflicts, "Routes shadowed by an earlier route with the same method:\n" + "\n".join(
        f"  {method} {earlier}  captures  {later}" for method, earlier, later in conflicts
    )
    stale = [pair for pair in ALLOWED if pair not in shadowing_pairs(app.routes)]
    assert not stale, f"Allow-list entries that no longer conflict: {stale}"


def test_payroll_v2_routers_are_registered_before_the_legacy_payroll_router(app):
    from app.payroll.routes import ROUTERS as V2
    from app.routes import ROUTERS, payroll

    order = {id(router): index for index, router in enumerate(ROUTERS)}
    assert V2 and all(order[id(router)] < order[id(payroll.router)] for router in V2)
    paths = [r.path for r in app.routes if isinstance(r, Route)]
    assert paths.index("/api/v1/payroll/access-preview") < paths.index("/api/v1/payroll/{payroll_id}")


def test_legacy_payroll_routes_would_shadow_v2_if_registered_first(app):
    """Guards the ordering rule above: legacy first would make v2 payroll paths unreachable."""
    from app.payroll.routes import ROUTERS as V2
    from app.routes import payroll

    reordered = FastAPI()
    reordered.include_router(payroll.router)
    for router in V2:
        reordered.include_router(router)
    assert ("GET", "/api/v1/payroll/{payroll_id}", "/api/v1/payroll/access-preview") in shadowing_pairs(
        reordered.routes
    )


def test_v2_routes_never_put_a_parameter_right_after_payroll(app):
    """`/payroll/{x}` in v2 would capture legacy `/payroll/my` and `/payroll/summary/...` (audit 04 §1)."""
    from app.payroll.routes import ROUTERS as V2

    probe = APIRouter()
    for router in V2:
        probe.include_router(router)
    for route in probe.routes:
        segments = route.path.strip("/").split("/")
        if segments[:3] == ["api", "v1", "payroll"] and len(segments) > 3:
            assert not segments[3].startswith("{"), route.path

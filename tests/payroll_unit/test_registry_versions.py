"""BUG-AS-63: a catalogue change must come with a registry version bump (WI-AS-0.4; AS 13 §2.2 "Registry versions").

registry_versions.json pins each REGISTRY_VERSION of the permission registry (codes, scopes, sensitivity, implied
access, default roles) and of the settings registry (every definition, pack settings excluded) to a fingerprint of
its content. Changing a catalogue without bumping its version fails; bumping a version means adding the new pair to
the file (``python -m tests.payroll_unit.test_registry_versions`` prints the current pairs).
"""

import hashlib
import json
from pathlib import Path

PINS = Path(__file__).resolve().parent / "registry_versions.json"


def _sha(payload) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def permission_fingerprint() -> str:
    from app.core import permission_registry as pr

    perms = [[p.code, p.module, p.label, p.description, [s.value for s in p.scopes], p.sensitivity,
              None if p.full_access_scopes is None else [s.value for s in p.full_access_scopes], p.deprecated]
             for p in pr.PERMISSIONS]
    roles = [[r.system_key, r.name, r.description, sorted([c, s.value] for c, s in r.grants), r.is_protected,
              r.is_full_access] for r in pr.DEFAULT_ROLES]
    return _sha({"permissions": perms, "roles": roles})


def settings_fingerprint() -> str:
    from app.payroll import settings_registry as sr

    pack_keys = {k for k in sr.REGISTRY if sr._STATUTORY_KEY_RE.fullmatch(k)}  # registered by packs at start-up
    return _sha([sr.definition_json(d) for d in sorted(sr.REGISTRY.values(), key=lambda d: d.key)
                 if d.key not in pack_keys])


def current() -> dict:
    from app.core import permission_registry as pr
    from app.payroll import settings_registry as sr

    return {"permissions": [pr.REGISTRY_VERSION, permission_fingerprint()],
            "settings": [sr.REGISTRY_VERSION, settings_fingerprint()]}


def test_bug_as_63_permission_catalogue_change_needs_a_version_bump():
    pins = json.loads(PINS.read_text(encoding="utf-8"))
    version, fingerprint = current()["permissions"]
    assert version in pins["permissions"], f"permission REGISTRY_VERSION {version} is not pinned in {PINS.name}"
    assert pins["permissions"][version] == fingerprint, (
        f"the permission catalogue changed but REGISTRY_VERSION is still {version}: bump it and pin the new pair")


def test_bug_as_63_settings_catalogue_change_needs_a_version_bump():
    pins = json.loads(PINS.read_text(encoding="utf-8"))
    version, fingerprint = current()["settings"]
    assert version in pins["settings"], f"settings REGISTRY_VERSION {version} is not pinned in {PINS.name}"
    assert pins["settings"][version] == fingerprint, (
        f"the settings catalogue changed but REGISTRY_VERSION is still {version}: bump it and pin the new pair")


def test_bug_as_63_versions_are_never_reused():
    pins = json.loads(PINS.read_text(encoding="utf-8"))
    for name in ("permissions", "settings"):
        values = list(pins[name].values())
        assert len(values) == len(set(values)), f"two {name} versions pin the same content"


if __name__ == "__main__":
    print(json.dumps(current(), indent=2))

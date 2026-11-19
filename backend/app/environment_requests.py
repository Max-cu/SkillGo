"""Prefer catalog-tested versions when compatible with the Skill's declarations."""
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name


def preferred_requirements(requests, defaults):
    result=list(requests)
    by_name={}
    for value in requests:
        req=Requirement(value)
        by_name.setdefault(canonicalize_name(req.name),[]).append(req)
    for value in defaults:
        default=Requirement(value)
        existing=by_name.get(canonicalize_name(default.name),[])
        if not existing:
            result.append(value)
            continue
        pins=[s.version for s in default.specifier if s.operator=='==' and '*' not in s.version]
        if len(pins)==1 and all(r.specifier.contains(pins[0],prereleases=True) for r in existing):
            # Keep requested extras, plus the compatible catalog pin.
            result.append(value)
    return sorted(set(result))

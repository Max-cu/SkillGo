"""Describe prepared dependencies using the current sandbox's observed packages."""
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

from .python_dependencies import requirements_satisfied


def annotate_prepared_dependencies(environment, prepared):
    if prepared is None or prepared.status != 'ready' or environment.get('image_id') != prepared.image_id:
        return
    requirements = prepared.spec.get('python_requirements', [])
    if not requirements:
        return
    installed = {canonicalize_name(name): version for name, version in
                 environment.get('python_distributions', {}).items()}
    missing = [value for value in requirements if not requirements_satisfied([value], environment)]
    imports_verified = not prepared.spec.get('python_imports') or bool(
        (prepared.inventory or {}).get('python_imports_verified'))
    environment['prepared_dependencies'] = {
        'status': 'ready' if not missing and imports_verified else 'not_verified',
        'requirements': requirements,
        'packages': [
            {'name': Requirement(value).name, 'version': installed[canonicalize_name(Requirement(value).name)]}
            for value in requirements if canonicalize_name(Requirement(value).name) in installed
        ],
        'missing_requirements': missing,
        'imports_verified': imports_verified,
    }

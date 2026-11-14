"""Input adaptation for the installed Director planning owner.

Form validation is pure: it cannot dispatch a provider, grant policy, reserve a
budget or manufacture a capability snapshot. The existing creative-brief owner
defines the field contracts.
"""
from copy import deepcopy
from functools import lru_cache
import json

from director_contracts import duration_frames
from director_schema import definitions
from rig_errors import RigError, finite_json

FORM_FIELDS = ('original_prompt', 'duration', 'frame_rate', 'resolution', 'style', 'budget')
MAX_FORM_BYTES = 64 * 1024


@lru_cache(maxsize=1)
def _form_validator():
    from jsonschema import Draft202012Validator
    defs = definitions()
    properties = defs['creative-brief']['properties']
    return Draft202012Validator(dict(type='object', additionalProperties=False,
        required=list(FORM_FIELDS), properties={key: properties[key] for key in FORM_FIELDS},
        **{'$defs': defs}))


def validate_planning_form(value):
    """Return independent canonical values; timing is exact and never rounded."""
    finite_json(value)
    if len(json.dumps(value, ensure_ascii=True, allow_nan=False).encode()) > MAX_FORM_BYTES:
        raise RigError('DIRECTOR_FORM_LIMIT', 'Planning form exceeds its bounded metadata limit.', stage='director')
    if next(_form_validator().iter_errors(value), None) is not None:
        raise RigError('DIRECTOR_FORM_INPUT', 'Use the canonical creative-brief planning fields.', stage='director')
    duration_frames(value['duration'], value['frame_rate'])
    budget = value['budget']
    if (budget['max_amount'] is None) != (budget['currency'] is None):
        raise RigError('DIRECTOR_FORM_BUDGET', 'A monetary bound needs its explicit currency.', stage='director')
    return deepcopy(value)

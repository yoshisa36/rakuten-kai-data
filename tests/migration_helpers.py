"""Test-only comparison contract, not an operational migration or sync tool."""
import json


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate JSON key')
        result[key] = value
    return result


def reject_constant(value):
    raise ValueError('Non-standard JSON number')


def same_snapshot(left, right):
    """Ignore formatting/key order; preserve all fields, values and array order."""
    def canonical(raw):
        value = json.loads(raw, object_pairs_hook=unique_object, parse_constant=reject_constant)
        return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False)
    return canonical(left) == canonical(right)

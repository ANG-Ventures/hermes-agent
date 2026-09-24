"""Single source of truth for the review-coverage record.

``kanban_request_changes`` refuses a rework verdict unless the current review
run carries a ``review_coverage: {...}`` JSON comment covering every lens in
:data:`REQUIRED_REVIEW_LENSES`. The gate (``kanban_db._validate_review_coverage``),
the tool description and the worker prompt all read the lens list from here,
so revising the lens set is a one-line change to this module.
"""
from __future__ import annotations

# Lenses a rework verdict must cover. Each is ``done`` or
# ``n/a: <applicability reason>`` in the coverage record.
REQUIRED_REVIEW_LENSES: tuple[str, ...] = ("contract", "execution", "cross-vendor", "mutation")

# Coverage fields the gate requires besides ``lenses``.
REQUIRED_COVERAGE_FIELDS: tuple[str, ...] = ("findings", "items", "review_minutes", "batch_id")

# Coverage fields that may be omitted (or null). ``battery`` is optional: the
# per-card battery is being retired in favour of CI-owned suites. When
# present it must be a non-empty string (attachment name, ``seeded``, or
# ``n/a: <reason>``).
OPTIONAL_COVERAGE_FIELDS: tuple[str, ...] = ("battery",)


def lens_list_text() -> str:
    """Human-readable lens list, e.g. ``contract, execution and mutation``."""
    lenses = list(REQUIRED_REVIEW_LENSES)
    if len(lenses) == 1:
        return lenses[0]
    return ", ".join(lenses[:-1]) + " and " + lenses[-1]


def coverage_fields_text() -> str:
    """Field list for prompts/descriptions: required first, optional marked."""
    return ", ".join(
        ("lenses",) + REQUIRED_COVERAGE_FIELDS
        + tuple(f"optional {name}" for name in OPTIONAL_COVERAGE_FIELDS)
    )

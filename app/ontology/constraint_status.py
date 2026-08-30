from app.domain.models import (
    ConstraintStatus,
    GroundedFilter,
    GroundedQuery,
    GroundedRelation,
    GroundedSort,
    GroundedField,
    GroundingStatus,
    ParsedQuery,
    ResolutionStatus,
    SemanticConstraint,
)


def grounded_constraint_statuses(
    parsed: ParsedQuery,
    *,
    resolved_entities,
    filters: list[GroundedFilter],
    sorts: list[GroundedSort],
    requested_fields: list[GroundedField],
    relations: list[GroundedRelation],
) -> list[SemanticConstraint]:
    """Carry every parsed constraint forward and fail closed on grounding loss."""

    statuses: dict[str, tuple[ConstraintStatus, str | None]] = {}
    for item in filters:
        if item.raw_filter.constraint_id is not None:
            statuses[item.raw_filter.constraint_id] = _grounding_status(item.status)
    for item in sorts:
        if item.raw_sort.constraint_id is not None:
            statuses[item.raw_sort.constraint_id] = _grounding_status(item.status)
    for item in requested_fields:
        match = next(
            (
                constraint
                for constraint in parsed.semantic_constraints
                if constraint.raw_text == item.raw_text
                and constraint.semantic_type.value == "requested_field"
            ),
            None,
        )
        if match is not None:
            statuses[match.constraint_id] = _grounding_status(item.status)
    for item in relations:
        if item.constraint_id is not None:
            statuses[item.constraint_id] = _grounding_status(
                item.status,
                reason="unresolved_material_relation",
            )
    for entity in resolved_entities:
        if entity.constraint_id is None:
            continue
        if entity.resolution_status is ResolutionStatus.AMBIGUOUS:
            statuses[entity.constraint_id] = (ConstraintStatus.AMBIGUOUS, None)
        elif entity.resolution_status is ResolutionStatus.UNRESOLVED and (
            entity.entity_type != "product"
        ):
            statuses[entity.constraint_id] = (
                ConstraintStatus.UNSUPPORTED,
                "unresolved_relation_source_entity",
            )
        else:
            # Product-name BM25 fallback preserves an unresolved product mention.
            statuses[entity.constraint_id] = (ConstraintStatus.GROUNDED, None)

    result: list[SemanticConstraint] = []
    for constraint in parsed.semantic_constraints:
        if constraint.status is ConstraintStatus.UNSUPPORTED:
            result.append(constraint)
            continue
        status, reason = statuses.get(
            constraint.constraint_id,
            (ConstraintStatus.GROUNDED, None),
        )
        result.append(
            constraint.model_copy(
                update={
                    "status": status,
                    "unsupported_reason": reason,
                }
            )
        )
    return result


def _grounding_status(
    status: GroundingStatus,
    *,
    reason: str = "unresolved_structured_constraint",
) -> tuple[ConstraintStatus, str | None]:
    if status is GroundingStatus.RESOLVED:
        return ConstraintStatus.GROUNDED, None
    if status is GroundingStatus.AMBIGUOUS:
        return ConstraintStatus.AMBIGUOUS, None
    return ConstraintStatus.UNSUPPORTED, reason

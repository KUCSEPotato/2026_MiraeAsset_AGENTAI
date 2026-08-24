from app.domain.models import ParsedQuery, ResolutionStatus, ResolvedQuery
from app.entity.lookup import EntityLookup


class RegistryEntityResolver:
    def __init__(self, lookup: EntityLookup) -> None:
        self._lookup = lookup

    async def resolve(self, query: ParsedQuery) -> ResolvedQuery:
        resolved_entities = []
        for mention in query.entities:
            matches = await self._lookup.lookup(
                mention.raw_text,
                mention.entity_type,
            )
            if len(matches) == 1:
                match = matches[0]
                resolved_entities.append(
                    mention.model_copy(
                        update={
                            "canonical_id": match.entity.canonical_id,
                            "resolution_status": ResolutionStatus.RESOLVED,
                            "confidence": 1.0,
                            "matched_alias": match.matched_alias,
                            "identifier_type": match.identifier_type,
                            "candidate_ids": [match.entity.canonical_id],
                        }
                    )
                )
            elif len(matches) > 1:
                resolved_entities.append(
                    mention.model_copy(
                        update={
                            "canonical_id": None,
                            "resolution_status": ResolutionStatus.AMBIGUOUS,
                            "confidence": 0.0,
                            "candidate_ids": [
                                match.entity.canonical_id for match in matches
                            ],
                        }
                    )
                )
            else:
                resolved_entities.append(
                    mention.model_copy(
                        update={
                            "canonical_id": None,
                            "resolution_status": ResolutionStatus.UNRESOLVED,
                            "confidence": 0.0,
                            "candidate_ids": [],
                        }
                    )
                )
        return ResolvedQuery(
            parsed_query=query,
            resolved_entities=resolved_entities,
        )


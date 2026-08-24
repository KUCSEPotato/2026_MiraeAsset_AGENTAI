import json

from app.domain.models import EvidenceBundle, FindingSeverity, ValidationResult


def serialize_evidence_bundle(
    bundle: EvidenceBundle,
    validation: ValidationResult | None = None,
) -> str:
    sections: list[str] = []
    for index, evidence in enumerate(bundle.evidence, start=1):
        lines = [
            f"[Evidence {index}]",
            f"source_type={evidence.source_type}",
            f"source_id={evidence.source_id}",
        ]
        findings = (
            [
                finding
                for finding in validation.findings
                if evidence.source_id in finding.source_ids
            ]
            if validation is not None
            else []
        )
        if findings:
            lines.extend(
                [
                    "quality_status="
                    + (
                        "invalid"
                        if any(
                            item.severity is FindingSeverity.BLOCKING
                            for item in findings
                        )
                        else "warning"
                    ),
                    "quality_findings="
                    + ",".join(
                        dict.fromkeys(item.code.value for item in findings)
                    ),
                ]
            )
        optional_fields = {
            "step_id": evidence.step_id,
            "entity_id": evidence.entity_id,
            "field": evidence.field,
            "value": evidence.value,
            "text": evidence.text,
            "dataset_snapshot": evidence.dataset_snapshot,
            "observed_at": evidence.observed_at,
        }
        lines.extend(
            f"{name}={value}"
            for name, value in optional_fields.items()
            if value is not None
        )
        if evidence.metadata:
            lines.append(
                "metadata="
                + json.dumps(
                    evidence.metadata,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
        sections.append("\n".join(lines))
    return "\n\n".join(sections)

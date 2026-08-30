import json
from pathlib import Path

from app.ontology.loader import OntologyLoader
from app.retrieval.rdb import RDBFieldRegistry


def main() -> int:
    ontology_dir = Path(__file__).resolve().parents[2] / "ontology"
    loaded = OntologyLoader(
        ontology_dir,
        known_canonical_fields=RDBFieldRegistry().canonical_fields,
    ).load()
    print(
        json.dumps(
            {
                "status": "valid",
                "files": [path.name for path in loaded.files],
                "triple_count": len(loaded.graph),
                "class_count": len(loaded.index.classes),
                "object_property_count": len(
                    loaded.index.object_properties
                ),
                "datatype_property_count": len(
                    loaded.index.datatype_properties
                ),
                "canonical_field_mapping_count": len(
                    loaded.index.field_mappings
                ),
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Relation-centric Neo4j knowledge graph integration."""

from app.graph.config import GraphSettings
from app.graph.mapping import GraphMappingRegistry

__all__ = ["GraphMappingRegistry", "GraphSettings"]

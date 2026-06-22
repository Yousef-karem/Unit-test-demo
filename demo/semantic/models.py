from __future__ import annotations
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Any

@dataclass
class CollaboratorStrategy:
    type_name: str
    strategy: str  # "mock" | "real"
    details: str   # explanation or concrete type to use

@dataclass
class TestSpec:
    package_name: str
    class_name: str
    method_name: str | None
    signature: str
    test_class_name: str
    
    # Environment & Constraints
    java_version: str = "17"
    junit_version: str = "5"
    has_mockito: bool = True
    
    # High-level conclusions
    domain_kind: str = "General"  # Entity, Controller, Service, Repository, DTO, General
    testability_hints: Dict[str, bool] = field(default_factory=dict)
    dependencies_calls: List[str] = field(default_factory=list)
    uses_types: List[str] = field(default_factory=list)
    control_flow_characteristics: Dict[str, Any] = field(default_factory=dict)
    private_method_delegation: List[str] = field(default_factory=list)
    collaborator_strategy: List[CollaboratorStrategy] = field(default_factory=list)
    
    # Supporting Context / Snippets
    snippet: str = ""
    imports_context: str = ""
    constructor_sigs: List[str] = field(default_factory=list)
    related_sources: str = ""

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        # Ensure we serialize CollaboratorStrategy cleanly inside lists
        d["collaborator_strategy"] = [asdict(c) for c in self.collaborator_strategy]
        return d

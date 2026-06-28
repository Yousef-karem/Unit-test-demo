from __future__ import annotations
import hashlib
import re
from pathlib import Path
from typing import Dict, List, Any, Set

from demo.semantic.context_enricher import (
    enrich_imports_context,
    enrich_method_source,
    enrich_private_method_sources,
    extract_literal_outputs,
)
from demo.semantic.edge_cases import build_edge_case_guidance
from demo.semantic.models import TestSpec, CollaboratorStrategy

# List of common value/utility types that should never be mocked
FORBIDDEN_COLLABORATORS = {
    "int", "long", "short", "byte", "char", "boolean", "float", "double",
    "void", "int[]", "long[]", "boolean[]", "char[]", "double[]", "float[]",
    "String", "Integer", "Long", "Double", "Float", "Boolean", "Character", "Byte", "Short",
    "List", "Map", "Set", "Collection", "ArrayList", "HashMap", "HashSet",
    "Optional", "Iterator", "Object", "Class", "BigDecimal", "BigInteger",
    "Date", "LocalDate", "LocalDateTime", "Instant", "UUID", "StringBuilder",
    "java.lang.String", "java.lang.Integer", "java.lang.Long", "java.lang.Double",
    "java.lang.Float", "java.lang.Boolean", "java.lang.Object",
    "java.util.List", "java.util.Map", "java.util.Set", "java.util.Collection",
    "java.util.Optional", "java.util.UUID"
}

class SemanticExtractor:
    def __init__(self, analysis: Dict):
        self.analysis = analysis or {}
        self.classes = self.analysis.get("classes") or {}

    def extract_spec(
        self,
        target: Dict,
        java_version: str = "17",
        junit_version: str = "5",
        has_mockito: bool = True,
        related_sources: str = "",
        project_root: Path | None = None,
    ) -> TestSpec:
        package = target.get("package") or ""
        class_name = target.get("class_name") or ""
        method_name = target.get("method_name")
        signature = target.get("signature") or ""
        
        fqcn = f"{package}.{class_name}" if package else class_name
        class_info = self.classes.get(fqcn) or {}
        
        # 1. Deterministic Test Class Name
        test_class_name = target.get("test_class_name")
        if not test_class_name:
            suffix = self._stable_suffix_for_target(target)
            base = f"LLM_Generated{class_name}{method_name or ''}"
            base = re.sub(r"[^A-Za-z0-9]+", "", base)
            test_class_name = f"{base}_{suffix}Test"
            
        # 2. Domain Kind
        domain_kind = self._extract_domain_kind(class_name, class_info)
        
        # 3. Method-level info and testability hints
        sig_key = target.get("signature_key")
        method_info = {}
        if sig_key:
            method_info = class_info.get("methods", {}).get(sig_key, {})
        if not method_info and method_name:
            for k, v in class_info.get("methods", {}).items():
                if k.split("(")[0] == method_name:
                    method_info = v
                    break
                    
        ast_dict = method_info.get("ast", {})
        modifiers = ast_dict.get("modifiers") or []
        return_type = method_info.get("returnType") or "void"
        is_static = "static" in modifiers
        is_void = return_type == "void"
        
        # Testability Hints
        testability_hints = self._extract_testability_hints(domain_kind, ast_dict)
        
        # Dependencies (calls and used types)
        dependencies_calls = []
        uses_types = []
        if method_name is None:
            # Class-level aggregation
            calls_set = set()
            types_set = set()
            for m_info in class_info.get("methods", {}).values():
                m_ast = m_info.get("ast", {})
                calls_set.update(m_ast.get("dependencies", {}).get("calls", []))
                types_set.update(m_ast.get("dependencies", {}).get("usesTypes", []))
            dependencies_calls = sorted(list(calls_set))
            uses_types = sorted(list(types_set))
        else:
            dependencies_calls = ast_dict.get("dependencies", {}).get("calls", [])
            uses_types = ast_dict.get("dependencies", {}).get("usesTypes", [])
            
        # 4. Control Flow Characteristics
        cf = {}
        if method_name is None:
            cf_agg = {
                "hasIf": False, "hasSwitch": False, "hasLoop": False, "hasTryCatch": False,
                "ifCount": 0, "switchCount": 0, "loopCount": 0, "tryCatchCount": 0,
                "returnCount": 0, "breakCount": 0, "continueCount": 0
            }
            for m_info in class_info.get("methods", {}).values():
                m_cf = m_info.get("ast", {}).get("controlFlow", {})
                for k in cf_agg:
                    if k.startswith("has"):
                        cf_agg[k] = cf_agg[k] or m_cf.get(k, False)
                    else:
                        cf_agg[k] += m_cf.get(k, 0)
            cf = cf_agg
        else:
            cf = ast_dict.get("controlFlow", {})
            
        control_flow_characteristics = {
            "has_loops": cf.get("hasLoop", False) or cf.get("loopCount", 0) > 0,
            "has_conditionals": cf.get("hasIf", False) or cf.get("hasSwitch", False) or cf.get("ifCount", 0) > 0 or cf.get("switchCount", 0) > 0,
            "has_exceptions": cf.get("hasTryCatch", False) or cf.get("tryCatchCount", 0) > 0 or testability_hints.get("hasThrowStatements", False),
            "cyclomatic_complexity": ast_dict.get("metrics", {}).get("cyclomaticComplexity", 1) if method_name else 1
        }
        
        # 5. Private Method Delegation
        private_method_delegation = self._extract_private_delegation(fqcn, class_info, dependencies_calls)
        private_method_sources = enrich_private_method_sources(
            class_info,
            private_method_delegation,
            target.get("source_file"),
        )
        
        # 6. Collaborator Strategy
        collaborator_strategy = self._extract_collaborator_strategy(fqcn, class_info, uses_types, method_info)
        
        # 7. Class level details (constructors)
        constructor_sigs = []
        for ctor in class_info.get("constructors", []):
            sig = ctor.get("signature")
            if sig:
                constructor_sigs.append(sig)
                
        is_interface_or_abstract = class_info.get("kind") == "interface" or "abstract" in class_info.get("modifiers", [])
        
        # Find concrete implementations if target is interface/abstract
        concrete_implementations = []
        if is_interface_or_abstract:
            for c_fqcn, c_info in self.classes.items():
                impls = c_info.get("implementsList", [])
                extends = c_info.get("extendsClass")
                if fqcn in impls or class_name in impls or fqcn == extends or class_name == extends:
                    concrete_implementations.append(c_fqcn)
                    
        method_source = enrich_method_source(
            target,
            class_info,
            method_info,
            fqcn,
            sig_key or "",
        )
        literal_outputs = extract_literal_outputs(ast_dict, method_source)
        imports_context = enrich_imports_context(target)
        edge_case_guidance = build_edge_case_guidance(method_info, control_flow_characteristics)

        return TestSpec(
            package_name=package,
            class_name=class_name,
            method_name=method_name,
            signature=signature,
            test_class_name=test_class_name,
            java_version=java_version,
            junit_version=junit_version,
            has_mockito=has_mockito,
            return_type=return_type,
            is_static=is_static,
            is_void=is_void,
            domain_kind=domain_kind,
            testability_hints=testability_hints,
            dependencies_calls=dependencies_calls,
            uses_types=uses_types,
            control_flow_characteristics=control_flow_characteristics,
            private_method_delegation=private_method_delegation,
            collaborator_strategy=collaborator_strategy,
            snippet=target.get("snippet") or "",
            method_source=method_source,
            imports_context=imports_context,
            constructor_sigs=constructor_sigs,
            related_sources=related_sources,
            private_method_sources=private_method_sources,
            literal_outputs=literal_outputs,
            edge_case_guidance=edge_case_guidance,
        )

    def _stable_suffix_for_target(self, t: Dict) -> str:
        key = "|".join([
            str(t.get("source_file", "")),
            str(t.get("class_name", "")),
            str(t.get("method_name", "")),
            str(t.get("signature", "")),
        ])
        return hashlib.sha1(key.encode("utf-8", errors="ignore")).hexdigest()[:8]

    def _extract_domain_kind(self, class_name: str, class_info: Dict) -> str:
        domain = class_info.get("domainKind", "general").lower()
        annotations = [ann.split("(")[0].strip() for ann in class_info.get("annotations", [])]
        implements = class_info.get("implementsList", [])
        extends = class_info.get("extendsClass") or ""
        
        # Direct classifications
        if domain == "entity" or any(ann in annotations for ann in ["Entity", "Table", "Document"]):
            return "Entity"
        elif domain == "controller" or "Controller" in class_name or any(ann in annotations for ann in ["RestController", "Controller"]):
            return "Controller"
        elif domain == "service" or "Service" in class_name or any(ann in annotations for ann in ["Service"]):
            return "Service"
        elif domain == "repository" or "Repository" in class_name or "Dao" in class_name or any(ann in annotations for ann in ["Repository"]) or "Repository" in extends or any("Repository" in imp for imp in implements):
            return "Repository"
        elif domain == "dto" or "DTO" in class_name or class_name.endswith("Dto") or class_name.endswith("Request") or class_name.endswith("Response"):
            return "DTO"
        
        # Rule based backup
        lower_name = class_name.lower()
        if "controller" in lower_name or "resource" in lower_name:
            return "Controller"
        if "service" in lower_name or "manager" in lower_name:
            return "Service"
        if "repository" in lower_name or "dao" in lower_name:
            return "Repository"
        if "dto" in lower_name or "request" in lower_name or "response" in lower_name or "model" in lower_name:
            return "DTO"
            
        return "General"

    def _extract_testability_hints(self, domain_kind: str, ast_dict: Dict) -> Dict[str, bool]:
        hints_src = ast_dict.get("testabilityHints", {})
        hints = {
            "probablyPure": bool(hints_src.get("probablyPure", False)),
            "usesDB": bool(hints_src.get("usesDB", False)) or (domain_kind == "Repository"),
            "usesNetwork": bool(hints_src.get("usesNetwork", False)),
            "usesTime": bool(hints_src.get("usesTime", False)),
            "usesIO": bool(hints_src.get("usesIO", False)),
            "usesRandomness": bool(hints_src.get("usesRandomness", False)),
            "hasThrowStatements": bool(hints_src.get("hasThrowStatements", False))
        }
        return hints

    def _extract_private_delegation(self, fqcn: str, class_info: Dict, calls: List[str]) -> List[str]:
        private_calls = []
        for call in calls:
            # Format in calls is class.method(params) e.g., "ds.MyItem.myPrivateHelper(int)"
            if call.startswith(fqcn + "."):
                method_part = call[len(fqcn) + 1:]
                
                # Check for direct key match
                called_method_info = class_info.get("methods", {}).get(method_part)
                if called_method_info:
                    modifiers = called_method_info.get("ast", {}).get("modifiers", [])
                    if "private" in modifiers:
                        private_calls.append(method_part)
                else:
                    # Fallback on name match
                    called_name = method_part.split("(")[0]
                    for k, v in class_info.get("methods", {}).items():
                        if k.split("(")[0] == called_name:
                            modifiers = v.get("ast", {}).get("modifiers", [])
                            if "private" in modifiers:
                                private_calls.append(k)
                                break
        return sorted(list(set(private_calls)))

    def _extract_collaborator_strategy(self, fqcn: str, class_info: Dict, uses_types: List[str], method_info: Dict) -> List[CollaboratorStrategy]:
        collaborators = set()
        
        # 1. Add parameter types of the method
        for param in method_info.get("parameters", []):
            param_type = param.get("type", "")
            if param_type:
                collaborators.add(param_type)
                
        # 2. Add class field types
        for field_info in class_info.get("fields", []):
            ft = field_info.get("resolvedType") or field_info.get("type")
            if ft:
                collaborators.add(ft)
                
        # 3. Add other types extracted by static analyzer usesTypes
        collaborators.update(uses_types)
        
        strategies: List[CollaboratorStrategy] = []
        seen_types = set()
        
        for t in collaborators:
            # Strip generic types and arrays for lookup
            base_type = t.split("<")[0].strip()
            base_type = base_type.replace("[]", "").strip()
            
            if not base_type or base_type in FORBIDDEN_COLLABORATORS or base_type in seen_types:
                continue
            seen_types.add(base_type)
            
            # Resolve to FQCN if possible
            resolved_fqcn = base_type
            if "." not in base_type:
                for c_fqcn in self.classes.keys():
                    if c_fqcn.endswith("." + base_type) or c_fqcn == base_type:
                        resolved_fqcn = c_fqcn
                        break
                        
            dep_class_info = self.classes.get(resolved_fqcn)
            
            if dep_class_info:
                kind = dep_class_info.get("kind")
                if kind == "interface":
                    # Check for concrete implementations
                    concrete_impls = []
                    for c_fqcn, c_info in self.classes.items():
                        impls = c_info.get("implementsList", [])
                        if resolved_fqcn in impls or base_type in impls:
                            concrete_impls.append(c_fqcn)
                            
                    if concrete_impls:
                        impl_names = ", ".join([impl.rsplit(".", 1)[-1] for impl in concrete_impls])
                        strategies.append(CollaboratorStrategy(
                            type_name=base_type,
                            strategy="real",
                            details=f"Interface has concrete implementations in project: [{impl_names}]. Prefer using a real concrete instance over Mockito."
                        ))
                    else:
                        strategies.append(CollaboratorStrategy(
                            type_name=base_type,
                            strategy="mock",
                            details="Interface type with no project implementation found. Mock using Mockito."
                        ))
                else:
                    # Class type
                    dep_domain = self._extract_domain_kind(base_type, dep_class_info)
                    if dep_domain in ["Service", "Repository", "Controller"]:
                        strategies.append(CollaboratorStrategy(
                            type_name=base_type,
                            strategy="mock",
                            details=f"Collaborating {dep_domain} component. Mock using Mockito."
                        ))
                    else:
                        strategies.append(CollaboratorStrategy(
                            type_name=base_type,
                            strategy="real",
                            details=f"Value object/DTO/Entity domain class. Instantiate using a real constructor."
                        ))
            else:
                # External class
                lower_name = base_type.lower()
                is_mockable_external = any(kw in lower_name for kw in ["client", "service", "repository", "template", "producer", "consumer", "auth", "context", "manager"])
                if is_mockable_external:
                    strategies.append(CollaboratorStrategy(
                        type_name=base_type,
                        strategy="mock",
                        details="External service or infrastructure class. Mock using Mockito."
                    ))
                else:
                    strategies.append(CollaboratorStrategy(
                        type_name=base_type,
                        strategy="real",
                        details="External value or standard type. Instantiate using real constructors or factory methods."
                    ))
                    
        return strategies

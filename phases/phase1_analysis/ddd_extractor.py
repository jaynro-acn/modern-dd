"""Orchestrate LLM calls to extract a DDD model from parsed Java files."""
from __future__ import annotations

import json
import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn

from llm.classifier import FileClassifier
from llm.prompts import (
    DDD_EXTRACT_PROMPT,
    GAP_ANALYSIS_PROMPT,
    VALUE_STREAM_PROMPT,
)
from phases.phase1_analysis.java_parser import JavaFileInfo, JavaParser

console = Console()


@dataclass
class DDDModel:
    project_name: str
    value_streams: list[dict] = field(default_factory=list)
    capabilities: list[dict] = field(default_factory=list)
    domains: list[dict] = field(default_factory=list)
    subdomains: list[dict] = field(default_factory=list)
    bounded_contexts: list[dict] = field(default_factory=list)
    components: list[dict] = field(default_factory=list)
    contracts: list[dict] = field(default_factory=list)
    jms_queues: list[dict] = field(default_factory=list)
    interfaces: list[dict] = field(default_factory=list)
    gaps: list[dict] = field(default_factory=list)


class DDDExtractor:
    def __init__(self, llm_adapter, token_tracker, config: dict):
        self.llm = llm_adapter
        self.tracker = token_tracker
        self.config = config
        self.classifier = FileClassifier()
        self.batch_size = config.get("llm", {}).get("batch_size", 5)

    def extract(self, java_files: list[JavaFileInfo], project_info) -> DDDModel:
        console.print(f"\n[bold cyan]Phase 1 — DDD Extraction[/bold cyan]")
        console.print(f"  Classifying {len(java_files)} files...")

        classified = self._classify_files(java_files)
        batches = self._build_batches(classified, project_info)

        console.print(f"  Created {len(batches)} analysis batches")

        batch_results: list[dict] = []
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TaskProgressColumn(),
            console=console,
        ) as progress:
            task = progress.add_task("Extracting DDD concepts...", total=len(batches))
            for i, batch in enumerate(batches):
                result = self._process_batch(batch, i, len(batches))
                if result:
                    batch_results.append(result)
                progress.advance(task)

        console.print(f"  Merging {len(batch_results)} batch results...")
        model = self._merge_models(batch_results, project_info.name)

        # Extract value streams from full component list
        console.print("  Inferring value streams...")
        model = self._extract_value_streams(model, project_info)

        # Gap analysis
        console.print("  Running gap analysis...")
        model = self._extract_gaps(model, project_info)

        self._print_model_summary(model)
        return model

    # ── Classification ────────────────────────────────────────────────────────

    def _classify_files(self, java_files: list[JavaFileInfo]) -> list[tuple[JavaFileInfo, str, str]]:
        results = []
        for jf in java_files:
            info = {
                "class_name": jf.class_name,
                "is_interface": jf.is_interface,
                "is_enum": jf.is_enum,
                "annotations": jf.annotations,
                "extends": jf.extends,
                "implements": jf.implements,
                "method_count": jf.method_count,
                "field_count": jf.field_count,
                "line_count": jf.line_count,
                "filename": jf.filepath.name,
            }
            category, tier = self.classifier.classify(info)
            results.append((jf, category, tier))
        return results

    # ── Batching ──────────────────────────────────────────────────────────────

    def _build_batches(
        self,
        classified: list[tuple[JavaFileInfo, str, str]],
        project_info,
    ) -> list[dict]:
        # Group by package prefix (first 2 segments = likely module/domain)
        groups: dict[str, list[tuple]] = defaultdict(list)
        for jf, cat, tier in classified:
            pkg = jf.package or "unknown"
            parts = pkg.split(".")
            group_key = ".".join(parts[:3]) if len(parts) >= 3 else pkg
            groups[group_key].append((jf, cat, tier))

        batches = []
        for group_key, items in groups.items():
            # Sort: smart-tier first (more complex files first)
            items.sort(key=lambda x: (0 if x[2] == "smart" else 1, x[0].class_name))
            # Split into sub-batches of batch_size
            for i in range(0, len(items), self.batch_size):
                chunk = items[i : i + self.batch_size]
                batches.append({
                    "group": group_key,
                    "items": chunk,
                    "project_name": project_info.name,
                    "jms_broker": project_info.jms_broker,
                    "java_version": project_info.java_version,
                })
        return batches

    # ── LLM batch processing ──────────────────────────────────────────────────

    def _process_batch(self, batch: dict, batch_idx: int, total: int) -> Optional[dict]:
        items = batch["items"]
        # Determine model tier: if any item needs "smart", use smart
        tier = "smart" if any(t == "smart" for _, _, t in items) else "fast"

        summaries = [JavaParser.summarize_for_llm(jf) for jf, _, _ in items]
        combined = "\n\n---\n\n".join(summaries)

        prompt = DDD_EXTRACT_PROMPT.format(
            project_name=batch["project_name"],
            jms_broker=batch["jms_broker"] or "unknown",
            java_version=batch["java_version"],
            group_name=batch["group"],
            file_summaries=combined,
        )

        try:
            response = self.llm.complete(prompt, model_tier=tier)
            self.tracker.record(
                phase="phase1_ddd_extract",
                file=f"batch_{batch_idx}_{batch['group']}",
                model=response.model,
                provider=response.provider,
                prompt_tokens=response.prompt_tokens,
                completion_tokens=response.completion_tokens,
            )
            return self._parse_json_response(response.text)
        except Exception as e:
            console.print(f"  [yellow]⚠ Batch {batch_idx}/{total} failed: {e}[/yellow]")
            return None

    def _parse_json_response(self, text: str) -> Optional[dict]:
        # Strip markdown fences if present
        text = re.sub(r"^```(?:json)?\s*", "", text.strip(), flags=re.MULTILINE)
        text = re.sub(r"\s*```$", "", text.strip(), flags=re.MULTILINE)
        # Find first JSON object
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1:
            return None
        try:
            return json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            # Try extracting embedded JSON
            for m in re.finditer(r'\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}', text, re.DOTALL):
                try:
                    return json.loads(m.group(0))
                except json.JSONDecodeError:
                    continue
            return None

    # ── Merge ─────────────────────────────────────────────────────────────────

    def _merge_models(self, batch_results: list[dict], project_name: str) -> DDDModel:
        model = DDDModel(project_name=project_name)
        seen: dict[str, set] = defaultdict(set)

        for result in batch_results:
            if not result:
                continue

            for domain in result.get("domains", []):
                n = domain.get("name", "")
                if n and n not in seen["domains"]:
                    seen["domains"].add(n)
                    model.domains.append(domain)

            for sd in result.get("subdomains", []):
                n = sd.get("name", "")
                if n and n not in seen["subdomains"]:
                    seen["subdomains"].add(n)
                    model.subdomains.append(sd)

            for bc in result.get("bounded_contexts", []):
                n = bc.get("name", "")
                if n and n not in seen["bounded_contexts"]:
                    seen["bounded_contexts"].add(n)
                    model.bounded_contexts.append(bc)

            for cap in result.get("capabilities", []):
                n = cap.get("name", "")
                if n and n not in seen["capabilities"]:
                    seen["capabilities"].add(n)
                    model.capabilities.append(cap)

            for comp in result.get("components", []):
                n = comp.get("name", "")
                if n and n not in seen["components"]:
                    seen["components"].add(n)
                    model.components.append(comp)

            for ct in result.get("contracts", []):
                n = ct.get("name", "")
                if n and n not in seen["contracts"]:
                    seen["contracts"].add(n)
                    model.contracts.append(ct)

            for q in result.get("jms_queues", []):
                n = q.get("name", "")
                if n and n not in seen["jms_queues"]:
                    seen["jms_queues"].add(n)
                    model.jms_queues.append(q)

            for iface in result.get("interfaces", []):
                n = iface.get("name", "")
                if n and n not in seen["interfaces"]:
                    seen["interfaces"].add(n)
                    model.interfaces.append(iface)

        return model

    # ── Value streams ─────────────────────────────────────────────────────────

    def _extract_value_streams(self, model: DDDModel, project_info) -> DDDModel:
        component_summary = "\n".join(
            f"- {c['name']} ({c.get('type','?')}) in {c.get('context','?')}: {c.get('description','')}"
            for c in model.components[:40]
        )
        domain_summary = "\n".join(
            f"- Domain: {d['name']} — {d.get('description','')}" for d in model.domains
        )
        prompt = VALUE_STREAM_PROMPT.format(
            project_name=project_info.name,
            domains=domain_summary,
            components=component_summary,
            readme=project_info.readme_content[:2000],
        )
        try:
            response = self.llm.complete(prompt, model_tier="smart")
            self.tracker.record(
                phase="phase1_value_streams",
                file="value_streams",
                model=response.model,
                provider=response.provider,
                prompt_tokens=response.prompt_tokens,
                completion_tokens=response.completion_tokens,
            )
            data = self._parse_json_response(response.text)
            if data:
                model.value_streams = data.get("value_streams", [])
                model.capabilities = data.get("capabilities", model.capabilities)
        except Exception as e:
            console.print(f"  [yellow]⚠ Value stream extraction failed: {e}[/yellow]")

        # Fallback: create a single value stream from project name
        if not model.value_streams:
            model.value_streams = [{
                "name": project_info.name.replace("-", " ").title(),
                "description": f"Primary value stream inferred from {project_info.name}",
            }]
        return model

    # ── Gap analysis ──────────────────────────────────────────────────────────

    def _extract_gaps(self, model: DDDModel, project_info) -> DDDModel:
        model_summary = json.dumps({
            "domains": [{"name": d["name"], "subdomains": [
                sd["name"] for sd in model.subdomains if sd.get("domain") == d["name"]
            ]} for d in model.domains],
            "bounded_contexts": len(model.bounded_contexts),
            "components": len(model.components),
            "jms_queues": len(model.jms_queues),
            "missing_context_links": [
                c["name"] for c in model.components if not c.get("context")
            ][:10],
            "unlinked_jms_queues": [
                q["name"] for q in model.jms_queues
                if not q.get("producers") and not q.get("consumers")
            ][:10],
        }, indent=2)

        prompt = GAP_ANALYSIS_PROMPT.format(
            project_name=project_info.name,
            model_summary=model_summary,
            java_version=project_info.java_version,
            jms_broker=project_info.jms_broker or "unknown",
            readme=project_info.readme_content[:1500],
        )
        try:
            response = self.llm.complete(prompt, model_tier="smart")
            self.tracker.record(
                phase="phase1_gaps",
                file="gap_analysis",
                model=response.model,
                provider=response.provider,
                prompt_tokens=response.prompt_tokens,
                completion_tokens=response.completion_tokens,
            )
            data = self._parse_json_response(response.text)
            if data:
                model.gaps = data.get("gaps", [])
        except Exception as e:
            console.print(f"  [yellow]⚠ Gap analysis failed: {e}[/yellow]")

        # Always inject structural gaps that can be detected mechanically
        model.gaps = self._inject_structural_gaps(model) + model.gaps
        # Assign stable IDs
        for i, gap in enumerate(model.gaps, 1):
            gap["id"] = f"GAP-{i:03d}"
        return model

    def _inject_structural_gaps(self, model: DDDModel) -> list[dict]:
        gaps = []
        orphan_components = [c["name"] for c in model.components if not c.get("context")]
        if orphan_components:
            gaps.append({
                "description": f"{len(orphan_components)} component(s) could not be assigned to a bounded context",
                "hypothesis": "These components may belong to a shared infrastructure or cross-cutting domain",
                "evidence_needed": f"Review: {', '.join(orphan_components[:5])}",
                "confidence": 40,
            })

        unlinked_queues = [q["name"] for q in model.jms_queues if not q.get("producers") and not q.get("consumers")]
        if unlinked_queues:
            gaps.append({
                "description": f"{len(unlinked_queues)} JMS queue(s) have no identified producer or consumer",
                "hypothesis": "Producers or consumers may be in external systems or not yet analyzed",
                "evidence_needed": f"Check: {', '.join(unlinked_queues[:5])}",
                "confidence": 30,
            })

        if not model.domains:
            gaps.append({
                "description": "No domains could be inferred from the codebase",
                "hypothesis": "The application may be a monolith with flat package structure",
                "evidence_needed": "Review package naming conventions and module structure",
                "confidence": 20,
            })
        return gaps

    # ── Summary ───────────────────────────────────────────────────────────────

    def _print_model_summary(self, model: DDDModel):
        console.print("\n  [bold green]DDD Model extracted:[/bold green]")
        console.print(f"    Value Streams:    {len(model.value_streams)}")
        console.print(f"    Capabilities:     {len(model.capabilities)}")
        console.print(f"    Domains:          {len(model.domains)}")
        console.print(f"    Subdomains:       {len(model.subdomains)}")
        console.print(f"    Bounded Contexts: {len(model.bounded_contexts)}")
        console.print(f"    Components:       {len(model.components)}")
        console.print(f"    Contracts:        {len(model.contracts)}")
        console.print(f"    JMS Queues:       {len(model.jms_queues)}")
        console.print(f"    Interfaces:       {len(model.interfaces)}")
        console.print(f"    Gaps identified:  {len(model.gaps)}")

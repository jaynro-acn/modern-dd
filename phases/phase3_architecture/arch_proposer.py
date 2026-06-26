"""Architecture proposer — queries Neo4j + Qdrant to propose JMS→Kafka migration options."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from rich.console import Console
from rich.table import Table

if TYPE_CHECKING:
    from vector.collections import QdrantManager
    from phases.phase2_ingestion.qdrant_writer import QdrantIngester

console = Console()

_ARCH_PROPOSAL_PROMPT = """You are a senior enterprise architect specialising in Java modernisation.

Analyse the following DDD model and JMS topology of a legacy Java 8 JMS application and propose exactly 3 target architecture options for migrating to Java 17 with Apache Kafka.

DDD SUMMARY:
{ddd_summary}

JMS TOPOLOGY:
{jms_topology}

DOCUMENT CONTEXT (from project documentation):
{doc_context}

For each option consider: Azure (AKS + Azure Service Bus bridging Kafka), GCP (GKE + Confluent Kafka), and multi-cloud (self-managed Kafka on any K8s).

Respond ONLY with valid JSON in this exact structure:
{{
  "options": [
    {{
      "id": "A",
      "name": "string",
      "description": "string",
      "rationale": "why this fits this specific codebase",
      "trade_offs": "pros and cons",
      "migration_complexity": "Low|Medium|High",
      "estimated_phases": 3,
      "kafka_topic_mapping": {{"old_jms_queue_name": "new.kafka.topic.name"}},
      "cloud_services": ["service1", "service2"]
    }}
  ]
}}"""


@dataclass
class ArchOption:
    id: str
    name: str
    description: str
    rationale: str
    trade_offs: str
    migration_complexity: str
    estimated_phases: int
    kafka_topic_mapping: dict = field(default_factory=dict)
    cloud_services: list[str] = field(default_factory=list)


class ArchProposer:
    def __init__(self, llm_adapter, token_tracker, neo4j_driver,
                 qdrant_manager: "QdrantManager", qdrant_ingester: "QdrantIngester"):
        self.llm = llm_adapter
        self.tracker = token_tracker
        self.driver = neo4j_driver
        self.qdrant = qdrant_manager
        self.ingester = qdrant_ingester

    # ── Public ───────────────────────────────────────────────────────────────

    def propose(self, project_name: str, config: dict) -> list[ArchOption]:
        console.print("[cyan]Querying graph and knowledge base for architecture proposals...[/cyan]")

        from graph.queries import get_full_ddd_summary, get_jms_topology
        ddd_summary = get_full_ddd_summary(self.driver, project_name)
        jms_topology = get_jms_topology(self.driver, project_name)

        doc_context = self._semantic_search("migration strategy target architecture kafka modernisation", project_name)

        prompt = _ARCH_PROPOSAL_PROMPT.format(
            ddd_summary=json.dumps(ddd_summary, indent=2),
            jms_topology=json.dumps(jms_topology, indent=2),
            doc_context=doc_context,
        )

        response = self.llm.complete(prompt, model_tier="smart")
        self.tracker.record(
            phase="phase3",
            file="arch_proposals",
            model=response.model,
            provider=response.provider,
            prompt_tokens=response.prompt_tokens,
            completion_tokens=response.completion_tokens,
        )

        raw_options = self._parse_json(response.text).get("options", [])
        options = [self._to_arch_option(o) for o in raw_options]

        # Fall back to config defaults if LLM returned nothing useful
        if not options:
            console.print("[yellow]LLM returned no options — using config defaults[/yellow]")
            for idx, cfg_opt in enumerate(config.get("target", {}).get("cloud_options", [])):
                letter = chr(ord("A") + idx)
                options.append(ArchOption(
                    id=letter,
                    name=cfg_opt["name"],
                    description=cfg_opt["description"],
                    rationale="Based on project structure.",
                    trade_offs="See documentation.",
                    migration_complexity="Medium",
                    estimated_phases=3,
                ))

        console.print(f"[green]✓ {len(options)} architecture option(s) proposed[/green]")
        return options

    def write_arch_proposals(self, options: list[ArchOption], output_dir: Path) -> None:
        arch_dir = output_dir / "architecture"
        arch_dir.mkdir(parents=True, exist_ok=True)
        out = arch_dir / "proposals.md"

        lines = ["# Target Architecture Proposals\n",
                 "Review the options below, then run `python main.py continue --project <name>` to select one.\n",
                 "| Option | Name | Complexity | Est. Phases |",
                 "|---|---|---|---|"]
        for o in options:
            lines.append(f"| **{o.id}** | {o.name} | {o.migration_complexity} | {o.estimated_phases} |")

        lines.append("")
        for o in options:
            lines += [
                f"## Option {o.id} — {o.name}",
                f"\n{o.description}\n",
                f"**Rationale:** {o.rationale}\n",
                f"**Trade-offs:** {o.trade_offs}\n",
                f"**Migration complexity:** {o.migration_complexity}",
                f"**Estimated phases:** {o.estimated_phases}\n",
                "**Cloud services:**",
            ]
            for svc in o.cloud_services:
                lines.append(f"- {svc}")
            if o.kafka_topic_mapping:
                lines.append("\n**JMS → Kafka topic mapping:**")
                lines.append("| JMS Queue | Kafka Topic |")
                lines.append("|---|---|")
                for old, new in o.kafka_topic_mapping.items():
                    lines.append(f"| `{old}` | `{new}` |")
            lines.append("\n---")

        out.write_text("\n".join(lines), encoding="utf-8")
        console.print(f"[green]✓ Architecture proposals written to {out}[/green]")

    def select_interactively(self, options: list[ArchOption], rich_console: Console) -> ArchOption:
        table = Table(title="Target Architecture Options", show_lines=True)
        table.add_column("ID", style="bold cyan", width=4)
        table.add_column("Name", style="bold")
        table.add_column("Complexity", width=10)
        table.add_column("Est. Phases", width=12)
        table.add_column("Summary")

        for o in options:
            colour = {"Low": "green", "Medium": "yellow", "High": "red"}.get(o.migration_complexity, "white")
            table.add_row(
                o.id,
                o.name,
                f"[{colour}]{o.migration_complexity}[/{colour}]",
                str(o.estimated_phases),
                o.description[:80] + ("..." if len(o.description) > 80 else ""),
            )

        rich_console.print(table)

        valid_ids = {o.id.upper() for o in options} | {str(i + 1) for i in range(len(options))}
        while True:
            raw = rich_console.input("[bold cyan]Select architecture option (A/B/C or 1/2/3): [/bold cyan]").strip().upper()
            if raw in valid_ids:
                # Resolve numeric input to letter
                if raw.isdigit():
                    idx = int(raw) - 1
                    if 0 <= idx < len(options):
                        return options[idx]
                else:
                    for o in options:
                        if o.id.upper() == raw:
                            return o
            rich_console.print(f"[red]Invalid selection '{raw}'. Please enter one of: {', '.join(sorted(valid_ids))}[/red]")

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _semantic_search(self, query: str, project_name: str) -> str:
        try:
            vector = self.ingester.embed_query(query)
            results = self.qdrant.search(query_vector=vector, top_k=3, filter_project=project_name)
            excerpts = [r["payload"].get("text", "")[:400] for r in results]
            return "\n---\n".join(excerpts) if excerpts else "No document context available."
        except Exception:
            return "No document context available."

    @staticmethod
    def _to_arch_option(raw: dict) -> ArchOption:
        return ArchOption(
            id=raw.get("id", "?"),
            name=raw.get("name", "Unknown"),
            description=raw.get("description", ""),
            rationale=raw.get("rationale", ""),
            trade_offs=raw.get("trade_offs", ""),
            migration_complexity=raw.get("migration_complexity", "Medium"),
            estimated_phases=int(raw.get("estimated_phases", 3)),
            kafka_topic_mapping=raw.get("kafka_topic_mapping", {}),
            cloud_services=raw.get("cloud_services", []),
        )

    @staticmethod
    def _parse_json(text: str) -> dict:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group())
            except json.JSONDecodeError:
                pass
        return {}

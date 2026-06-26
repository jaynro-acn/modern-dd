"""Migration planner — generates phased migration plan, ADRs, and Kafka topic design."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from rich.console import Console

from phases.phase3_architecture.arch_proposer import ArchOption

console = Console()

_MIGRATION_PLAN_PROMPT = """You are a senior Java architect planning the migration of a legacy Java 8 JMS application to Java 17 with Apache Kafka.

SELECTED ARCHITECTURE:
{arch_option}

BOUNDED CONTEXTS TO MIGRATE:
{bounded_contexts}

JMS TOPOLOGY:
{jms_topology}

Generate a phased migration plan. Each phase should migrate one or more bounded contexts.
Also generate the key ADRs and the Kafka topic design.

Respond ONLY with valid JSON:
{{
  "phases": [
    {{
      "phase_num": 1,
      "name": "string",
      "description": "string",
      "bounded_contexts": ["name1", "name2"],
      "duration_weeks": 4
    }}
  ],
  "adrs": [
    {{
      "id": "ADR-0001",
      "title": "string",
      "status": "Accepted",
      "context": "string",
      "decision": "string",
      "consequences": "string"
    }}
  ],
  "open_items": [
    {{
      "id": "OI-001",
      "description": "string",
      "owner": "Tech Lead",
      "priority": "High|Medium|Low"
    }}
  ],
  "kafka_topics": [
    {{
      "name": "string",
      "key_schema": "string",
      "value_schema": "string",
      "partitions": 3,
      "consumers": ["service1"]
    }}
  ]
}}"""


@dataclass
class MigrationPlan:
    arch_option: ArchOption
    phases: list[dict] = field(default_factory=list)
    adrs: list[dict] = field(default_factory=list)
    open_items: list[dict] = field(default_factory=list)
    kafka_topics: list[dict] = field(default_factory=list)


class MigrationPlanner:
    def __init__(self, llm_adapter, token_tracker, neo4j_driver):
        self.llm = llm_adapter
        self.tracker = token_tracker
        self.driver = neo4j_driver

    # ── Public ───────────────────────────────────────────────────────────────

    def plan(self, selected_arch: ArchOption, project_name: str, output_dir: Path) -> MigrationPlan:
        console.print("[cyan]Generating migration plan...[/cyan]")

        from graph.queries import get_full_ddd_summary, get_jms_topology
        ddd = get_full_ddd_summary(self.driver, project_name)
        jms = get_jms_topology(self.driver, project_name)

        bounded_contexts = []
        for domain in ddd.get("domains", []):
            for sd in domain.get("subdomains", []):
                for bc in sd.get("bounded_contexts", []):
                    bounded_contexts.append(bc)

        arch_dict = {
            "id": selected_arch.id,
            "name": selected_arch.name,
            "description": selected_arch.description,
            "kafka_topic_mapping": selected_arch.kafka_topic_mapping,
            "cloud_services": selected_arch.cloud_services,
        }

        prompt = _MIGRATION_PLAN_PROMPT.format(
            arch_option=json.dumps(arch_dict, indent=2),
            bounded_contexts=json.dumps(bounded_contexts, indent=2),
            jms_topology=json.dumps(jms, indent=2),
        )

        response = self.llm.complete(prompt, model_tier="smart")
        self.tracker.record(
            phase="phase3",
            file="migration_plan",
            model=response.model,
            provider=response.provider,
            prompt_tokens=response.prompt_tokens,
            completion_tokens=response.completion_tokens,
        )

        parsed = self._parse_json(response.text)
        plan = MigrationPlan(
            arch_option=selected_arch,
            phases=parsed.get("phases", []),
            adrs=parsed.get("adrs", []),
            open_items=parsed.get("open_items", []),
            kafka_topics=parsed.get("kafka_topics", []),
        )

        console.print(
            f"[green]✓ Plan: {len(plan.phases)} phase(s), {len(plan.adrs)} ADR(s), "
            f"{len(plan.kafka_topics)} Kafka topic(s)[/green]"
        )
        return plan

    def write_outputs(self, plan: MigrationPlan, output_dir: Path) -> None:
        arch_dir = output_dir / "architecture"
        arch_dir.mkdir(parents=True, exist_ok=True)

        self._write_migration_plan(plan, arch_dir)
        self._write_adrs(plan.adrs, arch_dir)
        self._write_open_items(plan.open_items, arch_dir)
        self._write_kafka_topics(plan.kafka_topics, arch_dir)

    # ── Writers ──────────────────────────────────────────────────────────────

    def _write_migration_plan(self, plan: MigrationPlan, arch_dir: Path) -> None:
        lines = [
            f"# Migration Plan — {plan.arch_option.name}\n",
            f"**Selected architecture:** Option {plan.arch_option.id} — {plan.arch_option.name}",
            f"**Migration complexity:** {plan.arch_option.migration_complexity}",
            f"**Total estimated phases:** {len(plan.phases)}\n",
            "## Phases\n",
            "| Phase | Name | Bounded Contexts | Duration |",
            "|---|---|---|---|",
        ]
        for p in plan.phases:
            bcs = ", ".join(p.get("bounded_contexts", []))
            lines.append(f"| {p['phase_num']} | {p['name']} | {bcs} | {p.get('duration_weeks', '?')} weeks |")

        lines.append("")
        for p in plan.phases:
            lines += [
                f"## Phase {p['phase_num']} — {p['name']}\n",
                p.get("description", ""),
                f"\n**Bounded contexts:** {', '.join(p.get('bounded_contexts', []))}",
                f"**Duration:** {p.get('duration_weeks', '?')} weeks\n",
                "---",
            ]

        out = arch_dir / "migration-plan.md"
        out.write_text("\n".join(lines), encoding="utf-8")
        console.print(f"[green]  ✓ {out}[/green]")

    def _write_adrs(self, adrs: list[dict], arch_dir: Path) -> None:
        adr_dir = arch_dir / "adrs"
        adr_dir.mkdir(exist_ok=True)
        for adr in adrs:
            slug = re.sub(r"[^a-z0-9]+", "-", adr.get("title", "untitled").lower()).strip("-")
            filename = f"{adr['id'].lower()}-{slug}.md"
            content = "\n".join([
                f"# {adr['id']} — {adr.get('title', '')}",
                f"\n- **Status**: {adr.get('status', 'Accepted')}",
                f"- **Date**: {_today()}\n",
                "## Context",
                adr.get("context", ""),
                "\n## Decision",
                adr.get("decision", ""),
                "\n## Consequences",
                adr.get("consequences", ""),
            ])
            (adr_dir / filename).write_text(content, encoding="utf-8")
        console.print(f"[green]  ✓ {len(adrs)} ADR(s) written to {adr_dir}[/green]")

    def _write_open_items(self, items: list[dict], arch_dir: Path) -> None:
        lines = [
            "# Open Items\n",
            "| ID | Description | Owner | Priority |",
            "|---|---|---|---|",
        ]
        for item in items:
            priority = item.get("priority", "Medium")
            colour = {"High": "🔴", "Medium": "🟡", "Low": "🟢"}.get(priority, "")
            lines.append(f"| {item['id']} | {item['description']} | {item.get('owner', 'TBD')} | {colour} {priority} |")

        out = arch_dir / "open-items.md"
        out.write_text("\n".join(lines), encoding="utf-8")
        console.print(f"[green]  ✓ {out}[/green]")

    def _write_kafka_topics(self, topics: list[dict], arch_dir: Path) -> None:
        lines = [
            "# Kafka Topic Design\n",
            "| Topic | Key Schema | Value Schema | Partitions | Consumers |",
            "|---|---|---|---|---|",
        ]
        for t in topics:
            consumers = ", ".join(t.get("consumers", []))
            lines.append(
                f"| `{t['name']}` | {t.get('key_schema', '—')} | "
                f"{t.get('value_schema', '—')} | {t.get('partitions', 3)} | {consumers} |"
            )

        out = arch_dir / "kafka-topics.md"
        out.write_text("\n".join(lines), encoding="utf-8")
        console.print(f"[green]  ✓ {out}[/green]")

    # ── Helpers ──────────────────────────────────────────────────────────────

    @staticmethod
    def _parse_json(text: str) -> dict:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group())
            except json.JSONDecodeError:
                pass
        return {}


def _today() -> str:
    from datetime import date
    return date.today().isoformat()

"""Spec generator — produces UKP-format spec.md + plan.md per bounded context."""
from __future__ import annotations

import json
import re
from pathlib import Path

from rich.console import Console

console = Console()

_SPEC_PROMPT = """You are a software architect writing a spec for migrating a bounded context from Java 8 JMS to Java 17 Kafka.

BOUNDED CONTEXT: {context_name}
DESCRIPTION: {context_description}
COMPONENTS: {components}
JMS QUEUES (input/output): {jms_queues}
MIGRATION PHASE: Phase {phase_num} — {phase_name}
TARGET ARCHITECTURE: {arch_name}

Write a spec and plan for this migration. Follow this EXACT JSON structure:
{{
  "spec": {{
    "objective": "one paragraph describing the migration goal",
    "in_scope": ["item1", "item2"],
    "out_of_scope": ["item1", "item2"],
    "acceptance_criteria": [
      {{"id": "AC1", "text": "observable outcome", "test_mode": "TDD|goal-based|manual"}}
    ]
  }},
  "plan": {{
    "tasks": [
      {{
        "id": "T1",
        "name": "string",
        "depends_on": null,
        "tests": "description of what to test",
        "approach": "implementation notes"
      }}
    ]
  }}
}}"""


class SpecGenerator:
    def __init__(self, llm_adapter, token_tracker, neo4j_driver):
        self.llm = llm_adapter
        self.tracker = token_tracker
        self.driver = neo4j_driver

    # ── Public ───────────────────────────────────────────────────────────────

    def generate(self, plan, project_name: str, output_dir: Path) -> None:
        specs_dir = output_dir / "specs"
        specs_dir.mkdir(parents=True, exist_ok=True)

        from graph.queries import get_components_for_context
        total_specs = 0

        for phase in plan.phases:
            phase_num = phase.get("phase_num", 0)
            phase_name = phase.get("name", "")
            for context_name in phase.get("bounded_contexts", []):
                console.print(f"  [cyan]Generating spec for: {context_name}[/cyan]")
                try:
                    components = get_components_for_context(self.driver, project_name, context_name)
                    jms_queues = self._find_jms_queues(context_name, plan)

                    context_description = self._get_context_description(context_name, plan)

                    prompt = _SPEC_PROMPT.format(
                        context_name=context_name,
                        context_description=context_description,
                        components=json.dumps(components, indent=2),
                        jms_queues=json.dumps(jms_queues, indent=2),
                        phase_num=phase_num,
                        phase_name=phase_name,
                        arch_name=plan.arch_option.name,
                    )

                    response = self.llm.complete(prompt, model_tier="smart")
                    self.tracker.record(
                        phase="phase4",
                        file=f"spec_{context_name}",
                        model=response.model,
                        provider=response.provider,
                        prompt_tokens=response.prompt_tokens,
                        completion_tokens=response.completion_tokens,
                    )

                    parsed = self._parse_json(response.text)
                    slug = self._slugify(context_name)
                    context_dir = specs_dir / slug
                    context_dir.mkdir(exist_ok=True)

                    self._write_spec_md(
                        context_dir / "spec.md",
                        context_name,
                        phase_num,
                        plan.arch_option.name,
                        parsed.get("spec", {}),
                    )
                    self._write_plan_md(
                        context_dir / "plan.md",
                        context_name,
                        parsed.get("plan", {}),
                    )
                    total_specs += 1
                    console.print(f"  [green]  ✓ {context_name}[/green]")

                except Exception as exc:
                    console.print(f"  [red]  ✗ {context_name}: {exc}[/red]")

        console.print(f"[green]✓ {total_specs} spec(s) written to {specs_dir}[/green]")

    # ── Writers ──────────────────────────────────────────────────────────────

    def _write_spec_md(self, path: Path, context_name: str, phase_num: int,
                       arch_name: str, spec: dict) -> None:
        lines = [
            f"# Spec — {context_name}\n",
            f"- **Status**: Draft",
            f"- **Shape**: service",
            f"- **Brief**: Phase {phase_num} — {arch_name}\n",
            "## Objective\n",
            spec.get("objective", "_To be defined._"),
            "\n## Boundaries\n",
            "### In scope\n",
        ]
        for item in spec.get("in_scope", ["_To be defined._"]):
            lines.append(f"- {item}")
        lines += ["\n### Out of scope\n"]
        for item in spec.get("out_of_scope", ["_To be defined._"]):
            lines.append(f"- {item}")

        # Testing strategy table
        acs = spec.get("acceptance_criteria", [])
        lines += ["\n## Testing Strategy\n",
                  "| AC | Mode | Artifact |",
                  "|---|---|---|"]
        for ac in acs:
            lines.append(f"| {ac.get('id', '?')} | {ac.get('test_mode', 'TDD')} | tests/{self._slugify(context_name)}/{ac.get('id', 'ac').lower()}_test.java |")

        lines += ["\n## Acceptance Criteria\n"]
        for ac in acs:
            lines.append(f"- [ ] **{ac.get('id', '?')}**: {ac.get('text', '')}")

        path.write_text("\n".join(lines), encoding="utf-8")

    def _write_plan_md(self, path: Path, context_name: str, plan: dict) -> None:
        lines = [
            f"# Plan — {context_name}\n",
            f"- **Status**: Drafting",
            f"- **Spec**: spec.md\n",
            "## Tasks\n",
        ]
        for task in plan.get("tasks", []):
            depends = task.get("depends_on") or "—"
            lines += [
                f"### {task.get('id', 'T?')} — {task.get('name', 'Unnamed task')}\n",
                f"**Depends on**: {depends}",
                f"**Tests**: {task.get('tests', 'To be defined')}",
                f"**Approach**: {task.get('approach', 'To be defined')}\n",
            ]

        path.write_text("\n".join(lines), encoding="utf-8")

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _find_jms_queues(self, context_name: str, plan) -> list[str]:
        relevant = []
        for topic in plan.kafka_topics:
            # Heuristic: topic name contains context slug
            slug = self._slugify(context_name)
            if slug in topic.get("name", "").replace(".", "-").replace("_", "-").lower():
                relevant.append(topic.get("name", ""))
        return relevant or list(plan.arch_option.kafka_topic_mapping.values())[:3]

    @staticmethod
    def _get_context_description(context_name: str, plan) -> str:
        for p in plan.phases:
            if context_name in p.get("bounded_contexts", []):
                return p.get("description", "")
        return ""

    @staticmethod
    def _slugify(name: str) -> str:
        return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")

    @staticmethod
    def _parse_json(text: str) -> dict:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group())
            except json.JSONDecodeError:
                pass
        return {}

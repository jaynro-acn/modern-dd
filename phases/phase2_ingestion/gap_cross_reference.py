"""Gap cross-referencer — searches ingested documents for evidence that resolves analysis gaps."""
from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING

from rich.console import Console

if TYPE_CHECKING:
    from vector.collections import QdrantManager
    from phases.phase2_ingestion.qdrant_writer import QdrantIngester

console = Console()

EVIDENCE_SCORE_THRESHOLD = 0.6

_ASSESS_PROMPT = """You are reviewing evidence from documentation to determine if it resolves a gap in a DDD analysis.

GAP:
{gap_description}

HYPOTHESIS:
{hypothesis}

EVIDENCE NEEDED:
{evidence_needed}

RETRIEVED DOCUMENT EXCERPTS:
{evidence_text}

Does this evidence resolve or partially resolve the gap?
Respond in JSON:
{{
  "resolves": true/false,
  "confidence": 0-100,
  "reasoning": "one sentence explanation",
  "relevant_excerpt": "the most relevant sentence from the evidence"
}}"""


class GapCrossReferencer:
    def __init__(self, qdrant_manager: "QdrantManager", qdrant_ingester: "QdrantIngester",
                 llm_adapter, token_tracker):
        self.qdrant = qdrant_manager
        self.ingester = qdrant_ingester
        self.llm = llm_adapter
        self.tracker = token_tracker

    # ── Public ───────────────────────────────────────────────────────────────

    def cross_reference(self, gaps: list[dict], project_name: str, output_dir: Path) -> list[dict]:
        """Search Qdrant for evidence matching each gap. Updates gaps with document_evidence."""
        if not gaps:
            return gaps

        console.print(f"[cyan]Cross-referencing {len(gaps)} gap(s) against ingested documents...[/cyan]")
        updated = []

        for gap in gaps:
            gap = dict(gap)  # copy
            query = gap.get("evidence_needed", gap.get("description", ""))
            if not query:
                gap["document_evidence"] = []
                gap["auto_resolved"] = False
                updated.append(gap)
                continue

            try:
                query_vector = self.ingester.embed_query(query)
                results = self.qdrant.search(
                    query_vector=query_vector,
                    top_k=3,
                    filter_project=project_name,
                )

                strong_results = [r for r in results if r.get("score", 0) >= EVIDENCE_SCORE_THRESHOLD]

                if not strong_results:
                    gap["document_evidence"] = []
                    gap["auto_resolved"] = False
                    console.print(f"  [dim]{gap['id']}: no strong evidence found[/dim]")
                    updated.append(gap)
                    continue

                evidence_text = "\n---\n".join(
                    f"[{r['payload'].get('source_file', 'unknown')}]\n{r['payload'].get('text', '')[:400]}"
                    for r in strong_results
                )

                prompt = _ASSESS_PROMPT.format(
                    gap_description=gap.get("description", ""),
                    hypothesis=gap.get("hypothesis", ""),
                    evidence_needed=query,
                    evidence_text=evidence_text,
                )

                response = self.llm.complete(prompt, model_tier="fast")
                self.tracker.record(
                    phase="phase2",
                    file=gap["id"],
                    model=response.model,
                    provider=response.provider,
                    prompt_tokens=response.prompt_tokens,
                    completion_tokens=response.completion_tokens,
                )

                assessment = self._parse_json(response.text)
                gap["document_evidence"] = [
                    {
                        "source": r["payload"].get("source_file", "unknown"),
                        "excerpt": r["payload"].get("text", "")[:300],
                        "score": r.get("score", 0),
                    }
                    for r in strong_results
                ]
                gap["auto_resolved"] = assessment.get("resolves", False)
                gap["auto_resolve_confidence"] = assessment.get("confidence", 0)
                gap["auto_resolve_reasoning"] = assessment.get("reasoning", "")
                gap["relevant_excerpt"] = assessment.get("relevant_excerpt", "")

                status = "[green]auto-resolved[/green]" if gap["auto_resolved"] else "[yellow]evidence found[/yellow]"
                console.print(f"  {gap['id']}: {status}")

            except Exception as exc:
                console.print(f"  [red]{gap['id']}: cross-reference failed — {exc}[/red]")
                gap["document_evidence"] = []
                gap["auto_resolved"] = False

            updated.append(gap)

        auto_resolved = sum(1 for g in updated if g.get("auto_resolved"))
        console.print(f"[cyan]{auto_resolved}/{len(updated)} gap(s) have supporting document evidence[/cyan]")

        gaps_file = output_dir / "gaps" / "gaps.md"
        if gaps_file.exists():
            self.update_gaps_file(updated, gaps_file)

        return updated

    def update_gaps_file(self, updated_gaps: list[dict], gaps_file: Path) -> None:
        """Rewrite gaps.md, appending document evidence sections to matching gaps."""
        content = gaps_file.read_text(encoding="utf-8")

        for gap in updated_gaps:
            if not gap.get("document_evidence"):
                continue

            gap_id = gap["id"]
            evidence_section = self._format_evidence_section(gap)

            # Find the gap block and append evidence before the next --- or end of file
            pattern = rf"(## {re.escape(gap_id)} —.*?)(\n---|\Z)"
            replacement = rf"\1\n{evidence_section}\2"
            new_content = re.sub(pattern, replacement, content, flags=re.DOTALL)
            if new_content != content:
                content = new_content

        gaps_file.write_text(content, encoding="utf-8")
        console.print(f"[green]✓ Updated gaps file with document evidence[/green]")

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _format_evidence_section(self, gap: dict) -> str:
        lines = ["\n### Document Evidence"]
        if gap.get("auto_resolved"):
            lines.append(f"- **Auto-resolved**: Yes (confidence: {gap.get('auto_resolve_confidence', 0)}%)")
            lines.append(f"- **Reasoning**: {gap.get('auto_resolve_reasoning', '')}")
        if gap.get("relevant_excerpt"):
            lines.append(f"- **Key excerpt**: _{gap['relevant_excerpt']}_")
        lines.append("\n**Sources:**")
        for ev in gap.get("document_evidence", []):
            source = Path(ev["source"]).name
            lines.append(f"- `{source}` (score: {ev['score']:.2f})")
            if ev.get("excerpt"):
                lines.append(f"  > {ev['excerpt'][:200]}...")
        return "\n".join(lines)

    @staticmethod
    def _parse_json(text: str) -> dict:
        import json
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group())
            except json.JSONDecodeError:
                pass
        return {}

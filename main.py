"""Legacy Modernization Accelerator — CLI entry point.

Usage:
  python main.py analyze --repo PATH --docs PATH [--project-name NAME] [--dry-run]
  python main.py continue --project NAME
"""
from __future__ import annotations

import sys
from pathlib import Path

import click
import yaml
from dotenv import load_dotenv
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

console = Console()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _load_config() -> dict:
    config_path = Path(__file__).parent / "config.yaml"
    if config_path.exists():
        with open(config_path, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    return {}


def _output_dir(project_name: str, config: dict) -> Path:
    import os
    base = Path(os.getenv("OUTPUT_DIR", "outputs"))
    return base / project_name


def _print_banner(title: str, subtitle: str = "") -> None:
    console.print(Panel(
        f"[bold cyan]{title}[/bold cyan]\n[dim]{subtitle}[/dim]" if subtitle else f"[bold cyan]{title}[/bold cyan]",
        border_style="cyan",
        padding=(0, 2),
    ))


def _abort(msg: str) -> None:
    console.print(f"[bold red]✗ {msg}[/bold red]")
    sys.exit(1)


# ── CLI group ─────────────────────────────────────────────────────────────────

@click.group()
def cli() -> None:
    """Legacy Modernization Accelerator — reverse-engineer Java 8 JMS apps into DDD specs."""


# ── analyze command ───────────────────────────────────────────────────────────

@cli.command("analyze")
@click.option("--repo", required=True, type=click.Path(exists=True), help="Path to the legacy Java repo.")
@click.option("--docs", required=True, type=click.Path(), help="Path to the documents folder (PDFs, txt, md).")
@click.option("--project-name", default=None, help="Project name (defaults to repo folder name).")
@click.option("--skip-neo4j", is_flag=True, default=False, help="Skip Neo4j write (dry-run of graph).")
@click.option("--skip-qdrant", is_flag=True, default=False, help="Skip Qdrant ingestion.")
@click.option("--dry-run", is_flag=True, default=False, help="Scan and classify files without LLM calls or DB writes.")
def analyze(repo: str, docs: str, project_name: str | None, skip_neo4j: bool,
            skip_qdrant: bool, dry_run: bool) -> None:
    """Phase 1 + 2: analyse a legacy Java repo and ingest documents."""
    load_dotenv()
    config = _load_config()

    repo_path = Path(repo).resolve()
    docs_path = Path(docs).resolve()
    project_name = project_name or repo_path.name
    out_dir = _output_dir(project_name, config)
    out_dir.mkdir(parents=True, exist_ok=True)

    _print_banner(
        "Legacy Modernization Accelerator — Analyze",
        f"Project: {project_name} | Repo: {repo_path} | Docs: {docs_path}",
    )

    if dry_run:
        console.print("[yellow]DRY RUN — no LLM calls or DB writes[/yellow]")

    # ── Init services ─────────────────────────────────────────────────────────
    try:
        from llm.adapter import LLMAdapter
        from tracking.token_tracker import TokenTracker
        from graph.schema import setup_schema
        from vector.collections import QdrantManager

        llm = LLMAdapter()
        tracker = TokenTracker(project_name=project_name, output_dir=out_dir)
        qdrant = QdrantManager(
            collection_name=config.get("qdrant", {}).get("collection_name", "knowledge_base"),
            storage_path=str(Path("data") / "qdrant"),
        )

        neo4j_driver = None
        if not skip_neo4j and not dry_run:
            import os
            from neo4j import GraphDatabase
            uri = os.getenv("NEO4J_URI", "bolt://localhost:7687")
            user = os.getenv("NEO4J_USER", "neo4j")
            password = os.getenv("NEO4J_PASSWORD", "")
            neo4j_driver = GraphDatabase.driver(uri, auth=(user, password))
            setup_schema(neo4j_driver)
            console.print("[green]✓ Neo4j connected[/green]")

    except Exception as exc:
        _abort(f"Initialisation failed: {exc}")

    # ══════════════════════════════════════════════════════════════════════════
    # PHASE 1 — Code Analysis & DDD Extraction
    # ══════════════════════════════════════════════════════════════════════════
    console.rule("[bold cyan]Phase 1 — Code Analysis & DDD Extraction[/bold cyan]")

    try:
        from phases.phase1_analysis.project_parser import ProjectParser
        from phases.phase1_analysis.java_parser import JavaParser
        from llm.classifier import FileClassifier
        from phases.phase1_analysis.ddd_extractor import DDDExtractor
        from phases.phase1_analysis.neo4j_writer import Neo4jWriter

        # Project-level context
        project_info = ProjectParser().parse(repo_path)
        console.print(f"[green]✓ Project: {project_info.name} | Build: {project_info.build_tool} | Java: {project_info.java_version}[/green]")

        # Java file scanning
        java_files = JavaParser().scan_repo(repo_path)
        if not java_files:
            _abort("No Java source files found in the repository.")

        # Classification stats
        classifier = FileClassifier()
        fast_count = smart_count = 0
        for jf in java_files:
            info = {
                "class_name": jf.class_name, "is_interface": jf.is_interface,
                "is_enum": jf.is_enum, "annotations": jf.annotations,
                "extends": jf.extends, "implements": jf.implements,
                "method_count": jf.method_count, "field_count": jf.field_count,
                "line_count": jf.line_count, "filename": jf.filepath.name,
            }
            _, tier = classifier.classify(info)
            if tier == "fast":
                fast_count += 1
            else:
                smart_count += 1

        _print_classification_table(len(java_files), fast_count, smart_count)

        if dry_run:
            console.print(f"[yellow]DRY RUN complete. Would process {len(java_files)} file(s). Exiting.[/yellow]")
            return

        # DDD extraction
        extractor = DDDExtractor(llm_adapter=llm, token_tracker=tracker, config=config)
        model = extractor.extract(java_files=java_files, project_info=project_info)

        # Neo4j write + markdown artifacts
        writer = Neo4jWriter(driver=neo4j_driver, project_name=project_name)
        if neo4j_driver:
            writer.write(model)
        else:
            console.print("[yellow]Skipping Neo4j write (--skip-neo4j)[/yellow]")
        writer.write_markdown_artifacts(model, out_dir)
        writer.write_gaps_file(model, out_dir)

    except Exception as exc:
        console.print_exception()
        _abort(f"Phase 1 failed: {exc}")

    # ══════════════════════════════════════════════════════════════════════════
    # PHASE 2 — Knowledge Ingestion & Validation
    # ══════════════════════════════════════════════════════════════════════════
    console.rule("[bold cyan]Phase 2 — Knowledge Ingestion & Validation[/bold cyan]")

    try:
        from phases.phase2_ingestion.document_loader import DocumentLoader
        from phases.phase2_ingestion.qdrant_writer import QdrantIngester
        from phases.phase2_ingestion.gap_cross_reference import GapCrossReferencer

        embed_model = config.get("embedding", {}).get("model", "all-MiniLM-L6-v2")
        ingester = QdrantIngester(qdrant_manager=qdrant, embedding_model_name=embed_model)

        if not skip_qdrant and not dry_run:
            chunks = DocumentLoader().load_all(docs_path)
            if chunks:
                ingester.ingest(chunks, project_name)
            else:
                console.print("[yellow]No documents found — skipping Qdrant ingestion[/yellow]")

            referencer = GapCrossReferencer(
                qdrant_manager=qdrant,
                qdrant_ingester=ingester,
                llm_adapter=llm,
                token_tracker=tracker,
            )
            model.gaps = referencer.cross_reference(model.gaps, project_name, out_dir)
        else:
            console.print("[yellow]Skipping Qdrant ingestion (--skip-qdrant)[/yellow]")

    except Exception as exc:
        console.print_exception()
        console.print(f"[yellow]Phase 2 warning: {exc} — continuing[/yellow]")

    # ── Summary ───────────────────────────────────────────────────────────────
    console.rule("[bold green]Analysis Complete[/bold green]")
    tracker.print_summary(console)

    gaps_file = out_dir / "gaps" / "gaps.md"
    open_gaps = _count_open_gaps(gaps_file)

    console.print(Panel(
        f"[bold]Output directory:[/bold] {out_dir}\n"
        f"[bold]Open gaps:[/bold] [{'red' if open_gaps else 'green'}]{open_gaps}[/{'red' if open_gaps else 'green'}]\n\n"
        f"[dim]Next step:[/dim]\n"
        f"  1. Review [cyan]{gaps_file}[/cyan]\n"
        f"  2. Change [yellow]Status: OPEN[/yellow] to [green]RESOLVED[/green] or [green]ACCEPTED[/green]\n"
        f"  3. Run: [bold cyan]python main.py continue --project {project_name}[/bold cyan]",
        title="[bold green]Next Steps[/bold green]",
        border_style="green",
    ))

    if neo4j_driver:
        neo4j_driver.close()


# ── continue command ──────────────────────────────────────────────────────────

@cli.command("continue")
@click.option("--project", required=True, help="Project name (must match a folder in outputs/).")
def continue_cmd(project: str) -> None:
    """Phase 3 + 4: validate gaps, propose architecture, generate specs."""
    load_dotenv()
    config = _load_config()
    out_dir = _output_dir(project, config)

    _print_banner(
        "Legacy Modernization Accelerator — Continue",
        f"Project: {project}",
    )

    if not out_dir.exists():
        _abort(f"Project output directory not found: {out_dir}\nRun 'analyze' first.")

    # ── Gap validation gate ───────────────────────────────────────────────────
    console.rule("[bold cyan]Gap Validation[/bold cyan]")
    gaps_file = out_dir / "gaps" / "gaps.md"

    if not gaps_file.exists():
        _abort(f"Gaps file not found: {gaps_file}\nRun 'analyze' first.")

    open_gaps = _find_open_gaps(gaps_file)
    if open_gaps:
        table = Table(title="Open Gaps — must be resolved before continuing", border_style="red")
        table.add_column("Gap ID", style="bold red")
        table.add_column("Description")
        for gap_id, description in open_gaps:
            table.add_row(gap_id, description)
        console.print(table)
        console.print(Panel(
            f"[red]Found {len(open_gaps)} open gap(s).[/red]\n\n"
            f"Edit [cyan]{gaps_file}[/cyan] and change [yellow]**Status**: OPEN[/yellow] "
            f"to [green]**Status**: RESOLVED[/green] or [green]**Status**: ACCEPTED[/green].\n\n"
            "Then re-run this command.",
            title="[bold red]Gaps Not Resolved[/bold red]",
            border_style="red",
        ))
        sys.exit(1)

    console.print("[bold green]✓ All gaps resolved — proceeding to architecture selection[/bold green]")

    # ── Init services ─────────────────────────────────────────────────────────
    try:
        import os
        from neo4j import GraphDatabase
        from llm.adapter import LLMAdapter
        from tracking.token_tracker import TokenTracker
        from vector.collections import QdrantManager
        from phases.phase2_ingestion.qdrant_writer import QdrantIngester

        llm = LLMAdapter()
        tracker = TokenTracker(project_name=project, output_dir=out_dir)

        uri = os.getenv("NEO4J_URI", "bolt://localhost:7687")
        user = os.getenv("NEO4J_USER", "neo4j")
        password = os.getenv("NEO4J_PASSWORD", "")
        neo4j_driver = GraphDatabase.driver(uri, auth=(user, password))

        qdrant = QdrantManager(
            collection_name=config.get("qdrant", {}).get("collection_name", "knowledge_base"),
            storage_path=str(Path("data") / "qdrant"),
        )
        embed_model = config.get("embedding", {}).get("model", "all-MiniLM-L6-v2")
        ingester = QdrantIngester(qdrant_manager=qdrant, embedding_model_name=embed_model)

    except Exception as exc:
        _abort(f"Initialisation failed: {exc}")

    # ══════════════════════════════════════════════════════════════════════════
    # PHASE 3 — Target Architecture Proposal & Selection
    # ══════════════════════════════════════════════════════════════════════════
    console.rule("[bold cyan]Phase 3 — Target Architecture Proposal & Selection[/bold cyan]")

    try:
        from phases.phase3_architecture.arch_proposer import ArchProposer
        from phases.phase3_architecture.migration_planner import MigrationPlanner

        proposer = ArchProposer(
            llm_adapter=llm,
            token_tracker=tracker,
            neo4j_driver=neo4j_driver,
            qdrant_manager=qdrant,
            qdrant_ingester=ingester,
        )

        options = proposer.propose(project_name=project, config=config)
        proposer.write_arch_proposals(options, out_dir)
        selected = proposer.select_interactively(options, console)

        console.print(f"\n[bold green]✓ Selected: Option {selected.id} — {selected.name}[/bold green]")

        planner = MigrationPlanner(llm_adapter=llm, token_tracker=tracker, neo4j_driver=neo4j_driver)
        plan = planner.plan(selected_arch=selected, project_name=project, output_dir=out_dir)
        planner.write_outputs(plan, out_dir)

    except Exception as exc:
        console.print_exception()
        _abort(f"Phase 3 failed: {exc}")

    # ══════════════════════════════════════════════════════════════════════════
    # PHASE 4 — Spec & Requirements Generation
    # ══════════════════════════════════════════════════════════════════════════
    console.rule("[bold cyan]Phase 4 — Spec & Requirements Generation[/bold cyan]")

    try:
        from phases.phase4_specs.spec_generator import SpecGenerator
        from phases.phase4_specs.contract_generator import ContractGenerator
        from graph.queries import get_full_ddd_summary
        from phases.phase1_analysis.ddd_extractor import DDDModel

        # Reconstruct a minimal DDDModel from Neo4j for contract generation
        from graph.queries import get_jms_topology
        ddd_summary = get_full_ddd_summary(neo4j_driver, project)
        jms_topology = get_jms_topology(neo4j_driver, project)
        model = _reconstruct_model_from_summary(ddd_summary, jms_topology, project)

        spec_gen = SpecGenerator(llm_adapter=llm, token_tracker=tracker, neo4j_driver=neo4j_driver)
        spec_gen.generate(plan=plan, project_name=project, output_dir=out_dir)

        contract_gen = ContractGenerator(llm_adapter=llm, token_tracker=tracker)
        contract_gen.generate(plan=plan, model=model, output_dir=out_dir)

    except Exception as exc:
        console.print_exception()
        _abort(f"Phase 4 failed: {exc}")

    # ── Final summary ─────────────────────────────────────────────────────────
    console.rule("[bold green]Complete[/bold green]")
    tracker.print_summary(console)

    console.print(Panel(
        f"[bold]Project:[/bold] {project}\n"
        f"[bold]Specs:[/bold] {out_dir / 'specs'}\n"
        f"[bold]Contracts:[/bold] {out_dir / 'contracts'}\n"
        f"[bold]Migration plan:[/bold] {out_dir / 'architecture' / 'migration-plan.md'}\n\n"
        "[dim]Hand these artifacts off to the forward engineering phase.[/dim]",
        title="[bold green]Specs Ready — Forward Engineering Phase[/bold green]",
        border_style="green",
    ))

    neo4j_driver.close()


# ── Utilities ─────────────────────────────────────────────────────────────────

def _print_classification_table(total: int, fast: int, smart: int) -> None:
    table = Table(title="File Classification", show_lines=False)
    table.add_column("Model tier", style="bold")
    table.add_column("Files", justify="right")
    table.add_column("% of total", justify="right")
    table.add_row("[cyan]Fast (Flash)[/cyan]", str(fast), f"{fast / total * 100:.0f}%")
    table.add_row("[magenta]Smart (Pro)[/magenta]", str(smart), f"{smart / total * 100:.0f}%")
    table.add_row("[bold]Total[/bold]", str(total), "100%")
    console.print(table)


def _count_open_gaps(gaps_file: Path) -> int:
    if not gaps_file.exists():
        return 0
    return gaps_file.read_text(encoding="utf-8").count("**Status**: OPEN")


def _find_open_gaps(gaps_file: Path) -> list[tuple[str, str]]:
    """Return list of (gap_id, description) for all OPEN gaps."""
    import re
    content = gaps_file.read_text(encoding="utf-8")
    open_gaps: list[tuple[str, str]] = []
    # Find all gap blocks
    blocks = re.findall(r"## (GAP-\d+) — ([^\n]+).*?\*\*Status\*\*: (OPEN|RESOLVED|ACCEPTED)", content, re.DOTALL)
    for gap_id, description, status in blocks:
        if status == "OPEN":
            open_gaps.append((gap_id, description.strip()))
    return open_gaps


def _reconstruct_model_from_summary(ddd_summary: dict, jms_topology: list, project_name: str):
    """Build a minimal DDDModel from flat Neo4j query results for use in Phase 4."""
    from phases.phase1_analysis.ddd_extractor import DDDModel

    # ddd_summary has flat lists: value_streams, domains, bounded_contexts, components
    components = [
        {
            "name": c.get("name", ""),
            "type": c.get("type", "service"),
            "context": c.get("bounded_context", ""),
            "description": c.get("description", ""),
            "jms_role": None,
            "depends_on": [],
        }
        for c in ddd_summary.get("components", [])
    ]

    # jms_topology comes from get_jms_topology()
    jms_queues = [
        {
            "name": q.get("queue_name", ""),
            "producers": q.get("producers", []),
            "consumers": q.get("consumers", []),
            "message_type": q.get("message_type", ""),
            "description": q.get("description", ""),
        }
        for q in jms_topology
    ]

    return DDDModel(
        project_name=project_name,
        value_streams=ddd_summary.get("value_streams", []),
        capabilities=[],
        domains=ddd_summary.get("domains", []),
        subdomains=[],
        bounded_contexts=ddd_summary.get("bounded_contexts", []),
        components=components,
        contracts=[],
        jms_queues=jms_queues,
        interfaces=[],
        gaps=[],
    )


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    cli()

"""Qdrant ingestion for Phase 2 — embeds document chunks into the local vector store."""
from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from rich.console import Console
from rich.progress import Progress, SpinnerColumn, BarColumn, TextColumn, TimeElapsedColumn

if TYPE_CHECKING:
    from vector.collections import QdrantManager
    from phases.phase2_ingestion.document_loader import DocumentChunk

console = Console()

EMBED_BATCH_SIZE = 32


class QdrantIngester:
    def __init__(self, qdrant_manager: "QdrantManager", embedding_model_name: str = "all-MiniLM-L6-v2"):
        self.qdrant = qdrant_manager
        self.embedding_model_name = embedding_model_name
        self._model = None  # lazy-loaded

    # ── Model ────────────────────────────────────────────────────────────────

    def _get_model(self):
        if self._model is None:
            console.print(f"[cyan]Loading embedding model: {self.embedding_model_name}[/cyan]")
            from sentence_transformers import SentenceTransformer
            self._model = SentenceTransformer(self.embedding_model_name)
            console.print("[green]  ✓ Embedding model loaded[/green]")
        return self._model

    # ── Public ───────────────────────────────────────────────────────────────

    def ingest(self, chunks: list["DocumentChunk"], project_name: str) -> int:
        """Embed and upsert all chunks into Qdrant. Returns total points ingested."""
        if not chunks:
            console.print("[yellow]No chunks to ingest.[/yellow]")
            return 0

        model = self._get_model()
        vector_size = model.get_sentence_embedding_dimension()
        self.qdrant.ensure_collection(vector_size=vector_size)

        total = 0
        batches = [chunks[i:i + EMBED_BATCH_SIZE] for i in range(0, len(chunks), EMBED_BATCH_SIZE)]

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TextColumn("{task.completed}/{task.total} batches"),
            TimeElapsedColumn(),
            console=console,
        ) as progress:
            task = progress.add_task("Embedding & ingesting chunks...", total=len(batches))

            for batch in batches:
                texts = [c.text for c in batch]
                vectors = model.encode(texts, show_progress_bar=False).tolist()

                for chunk, vector in zip(batch, vectors):
                    point_id = str(uuid.uuid4())
                    payload = {
                        "text": chunk.text,
                        "source_file": chunk.source_file,
                        "chunk_index": chunk.chunk_index,
                        "project": project_name,
                        "metadata": chunk.metadata,
                    }
                    self.qdrant.upsert(entity_id=point_id, vector=vector, payload=payload)
                    total += 1

                progress.advance(task)

        console.print(f"[green]✓ Ingested {total} chunks into Qdrant[/green]")
        return total

    def embed_query(self, query: str) -> list[float]:
        """Encode a single query string into a vector."""
        model = self._get_model()
        return model.encode(query, show_progress_bar=False).tolist()

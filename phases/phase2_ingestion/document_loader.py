"""Document loader for Phase 2 — loads and chunks PDFs, text, markdown, and docx files."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from rich.console import Console

console = Console()

SUPPORTED_EXTENSIONS = {".pdf", ".txt", ".md", ".docx"}
CHUNK_SIZE_CHARS = 2048
CHUNK_OVERLAP_CHARS = 256


@dataclass
class DocumentChunk:
    source_file: str
    chunk_index: int
    text: str
    metadata: dict = field(default_factory=dict)


class DocumentLoader:
    def __init__(self, chunk_size: int = CHUNK_SIZE_CHARS, chunk_overlap: int = CHUNK_OVERLAP_CHARS):
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap

    # ── Public ──────────────────────────────────────────────────────────────

    def load_all(self, docs_folder: Path) -> list[DocumentChunk]:
        if not docs_folder.exists():
            console.print(f"[yellow]Docs folder not found: {docs_folder}[/yellow]")
            return []

        all_chunks: list[DocumentChunk] = []
        files = [f for f in docs_folder.rglob("*") if f.is_file() and f.suffix.lower() in SUPPORTED_EXTENSIONS]

        console.print(f"[cyan]Found {len(files)} document(s) to ingest[/cyan]")
        for doc_path in files:
            try:
                text = self._extract_text(doc_path)
                if not text.strip():
                    console.print(f"[yellow]  Skipping empty file: {doc_path.name}[/yellow]")
                    continue
                chunks = self._chunk_text(text)
                file_type = doc_path.suffix.lower().lstrip(".")
                for idx, chunk in enumerate(chunks):
                    all_chunks.append(DocumentChunk(
                        source_file=str(doc_path),
                        chunk_index=idx,
                        text=chunk,
                        metadata={
                            "source": doc_path.name,
                            "file_type": file_type,
                            "total_chunks": len(chunks),
                        },
                    ))
                console.print(f"  [green]✓[/green] {doc_path.name} → {len(chunks)} chunk(s)")
            except Exception as exc:
                console.print(f"  [red]✗[/red] {doc_path.name}: {exc}")

        console.print(f"[cyan]Total chunks produced: {len(all_chunks)}[/cyan]")
        return all_chunks

    # ── Extraction ───────────────────────────────────────────────────────────

    def _extract_text(self, path: Path) -> str:
        suffix = path.suffix.lower()
        if suffix == ".pdf":
            return self._extract_pdf(path)
        elif suffix in (".txt", ".md"):
            return path.read_text(encoding="utf-8", errors="replace")
        elif suffix == ".docx":
            return self._extract_docx(path)
        return ""

    def _extract_pdf(self, path: Path) -> str:
        from pypdf import PdfReader  # lazy import

        reader = PdfReader(str(path))
        pages: list[str] = []
        for page in reader.pages:
            text = page.extract_text()
            if text:
                pages.append(text)
        return "\n\n".join(pages)

    def _extract_docx(self, path: Path) -> str:
        from docx import Document  # lazy import

        doc = Document(str(path))
        paragraphs = [p.text for p in doc.paragraphs if p.text.strip()]
        return "\n\n".join(paragraphs)

    # ── Chunking ─────────────────────────────────────────────────────────────

    def _chunk_text(self, text: str) -> list[str]:
        """Split text into overlapping chunks, preferring sentence/paragraph boundaries."""
        # Normalise whitespace
        text = re.sub(r"\n{3,}", "\n\n", text).strip()

        if len(text) <= self.chunk_size:
            return [text]

        # Split on double-newlines (paragraph boundary) first, then on ". "
        paragraphs = re.split(r"\n\n+", text)
        chunks: list[str] = []
        current = ""

        for para in paragraphs:
            # If adding this paragraph keeps us under the limit, append it
            if len(current) + len(para) + 2 <= self.chunk_size:
                current = (current + "\n\n" + para).strip()
            else:
                # Flush current chunk
                if current:
                    chunks.append(current)
                # If single paragraph is larger than chunk_size, split on sentences
                if len(para) > self.chunk_size:
                    sentence_chunks = self._split_paragraph(para)
                    chunks.extend(sentence_chunks[:-1])
                    current = sentence_chunks[-1] if sentence_chunks else ""
                else:
                    # Start new chunk with overlap from previous chunk
                    overlap = current[-self.chunk_overlap:] if current else ""
                    current = (overlap + "\n\n" + para).strip() if overlap else para

        if current:
            chunks.append(current)

        return [c for c in chunks if c.strip()]

    def _split_paragraph(self, text: str) -> list[str]:
        """Split an oversized paragraph on sentence boundaries."""
        sentences = re.split(r"(?<=\. )", text)
        chunks: list[str] = []
        current = ""
        for sentence in sentences:
            if len(current) + len(sentence) <= self.chunk_size:
                current += sentence
            else:
                if current:
                    chunks.append(current.strip())
                overlap = current[-self.chunk_overlap:] if current else ""
                current = overlap + sentence
        if current:
            chunks.append(current.strip())
        return chunks or [text[:self.chunk_size]]

"""Parse Java source files and extract structural information for DDD analysis."""
from __future__ import annotations

import re
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from rich.console import Console

console = Console()

SKIP_DIRS = {
    "target", "build", ".git", ".svn", "node_modules",
    ".idea", ".eclipse", "__pycache__", ".venv", "out", "bin", "dist",
}

SKIP_EXTENSIONS = {
    ".class", ".jar", ".war", ".ear", ".zip", ".tar", ".gz",
    ".bin", ".exe", ".dll", ".so", ".dylib", ".png", ".jpg",
    ".gif", ".svg", ".ico", ".pdf", ".doc", ".docx",
}

# Magic bytes for binary detection (first 4 bytes)
BINARY_MAGIC = {
    b"\xca\xfe\xba\xbe",  # Java .class
    b"PK\x03\x04",        # ZIP / JAR / WAR
    b"\x1f\x8b\x08",      # gzip
}


@dataclass
class JavaFileInfo:
    filepath: Path
    package: str
    class_name: str
    is_interface: bool
    is_enum: bool
    is_abstract: bool
    annotations: list[str]
    extends: Optional[str]
    implements: list[str]
    imports: list[str]
    fields: list[dict]
    methods: list[dict]
    method_count: int
    field_count: int
    line_count: int
    jms_queues: list[str]
    jms_role: Optional[str]
    raw_excerpt: str


def _is_binary(path: Path) -> bool:
    try:
        with open(path, "rb") as f:
            header = f.read(4)
        for magic in BINARY_MAGIC:
            if header[: len(magic)] == magic:
                return True
        return False
    except OSError:
        return True


def _extract_string_literals(source: str) -> list[str]:
    return re.findall(r'"([^"\\]{1,200})"', source)


def _extract_jms_queues_from_source(source: str, annotations: list[str]) -> list[str]:
    queues: list[str] = []
    # @JmsListener(destination = "some.queue")
    for m in re.finditer(r'@JmsListener\s*\([^)]*destination\s*=\s*["\']([^"\']+)["\']', source):
        queues.append(m.group(1))
    # @MessageDriven activationConfig destinationName
    for m in re.finditer(r'destinationName\s*=\s*["\']([^"\']+)["\']', source):
        queues.append(m.group(1))
    # Spring JmsTemplate / ActiveMQ constants referencing queue names
    for m in re.finditer(r'(?:QUEUE|TOPIC|DESTINATION)[_A-Z]*\s*=\s*"([^"]+)"', source):
        queues.append(m.group(1))
    # Strings that look like queue names (contain dots or slashes, not URLs)
    for lit in _extract_string_literals(source):
        if (
            ("queue" in lit.lower() or "topic" in lit.lower() or "dest" in lit.lower())
            and "http" not in lit.lower()
            and len(lit) < 120
        ):
            queues.append(lit)
    return list(dict.fromkeys(queues))  # deduplicate preserving order


def _detect_jms_role(source: str, methods: list[dict]) -> Optional[str]:
    method_names = {m["name"] for m in methods}
    is_consumer = (
        "onMessage" in method_names
        or bool(re.search(r'@JmsListener', source))
        or bool(re.search(r'MessageDrivenBean|MessageListener', source))
    )
    is_producer = bool(
        re.search(r'\.send\s*\(|\.convertAndSend\s*\(|jmsTemplate\.|JmsTemplate', source)
    )
    if is_consumer and is_producer:
        return "both"
    if is_consumer:
        return "consumer"
    if is_producer:
        return "producer"
    return None


class JavaParser:
    def parse_file(self, filepath: Path) -> Optional[JavaFileInfo]:
        if _is_binary(filepath):
            return None

        try:
            source = filepath.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            console.print(f"[yellow]  ⚠ Cannot read {filepath}: {e}[/yellow]")
            return None

        lines = source.splitlines()
        line_count = len(lines)
        raw_excerpt = "\n".join(lines[:50])

        # Use regex-based parsing — javalang chokes on many Java 8 constructs
        package = self._extract_package(source)
        class_name = self._extract_class_name(filepath, source)
        is_interface = bool(re.search(r'\binterface\s+\w', source))
        is_enum = bool(re.search(r'\benum\s+\w', source))
        is_abstract = bool(re.search(r'\babstract\s+class\b', source))
        annotations = self._extract_annotations(source)
        extends = self._extract_extends(source)
        implements = self._extract_implements(source)
        imports = self._extract_imports(source)
        fields = self._extract_fields(source)
        methods = self._extract_methods(source)
        jms_queues = _extract_jms_queues_from_source(source, annotations)
        jms_role = _detect_jms_role(source, methods)

        return JavaFileInfo(
            filepath=filepath,
            package=package,
            class_name=class_name,
            is_interface=is_interface,
            is_enum=is_enum,
            is_abstract=is_abstract,
            annotations=annotations,
            extends=extends,
            implements=implements,
            imports=imports,
            fields=fields,
            methods=methods,
            method_count=len(methods),
            field_count=len(fields),
            line_count=line_count,
            jms_queues=jms_queues,
            jms_role=jms_role,
            raw_excerpt=raw_excerpt,
        )

    # ── private extraction helpers ────────────────────────────────────────────

    def _extract_package(self, source: str) -> str:
        m = re.search(r'^\s*package\s+([\w.]+)\s*;', source, re.MULTILINE)
        return m.group(1) if m else ""

    def _extract_class_name(self, filepath: Path, source: str) -> str:
        # Try to get it from the public class/interface/enum declaration
        m = re.search(
            r'\b(?:public\s+)?(?:abstract\s+)?(?:class|interface|enum)\s+(\w+)',
            source,
        )
        if m:
            return m.group(1)
        return filepath.stem

    def _extract_annotations(self, source: str) -> list[str]:
        raw = re.findall(r'@([\w.]+)', source)
        # Deduplicate, keep only annotation-looking tokens (PascalCase or known)
        seen: dict[str, None] = {}
        for a in raw:
            if a[0].isupper() or a in ("override", "deprecated", "suppress"):
                seen[a] = None
        return list(seen.keys())

    def _extract_extends(self, source: str) -> Optional[str]:
        m = re.search(r'\bextends\s+([\w.]+)', source)
        return m.group(1) if m else None

    def _extract_implements(self, source: str) -> list[str]:
        m = re.search(r'\bimplements\s+([\w.,\s<>]+?)(?:\{|extends)', source)
        if not m:
            m = re.search(r'\bimplements\s+([\w.,\s<>]+?)\s*\{', source)
        if not m:
            return []
        raw = m.group(1)
        # Strip generics, split by comma
        raw = re.sub(r'<[^>]*>', '', raw)
        return [p.strip() for p in raw.split(",") if p.strip()]

    def _extract_imports(self, source: str) -> list[str]:
        return re.findall(r'^\s*import\s+([\w.*]+)\s*;', source, re.MULTILINE)

    def _extract_fields(self, source: str) -> list[dict]:
        fields = []
        pattern = re.compile(
            r'(?:private|protected|public|static|final|\s)+'
            r'([\w<>\[\].,\s]+?)\s+(\w+)\s*(?:=|;)',
        )
        for m in pattern.finditer(source):
            ftype = m.group(1).strip()
            fname = m.group(2).strip()
            if fname and ftype and not ftype.startswith("//") and fname not in ("class", "void"):
                fields.append({"name": fname, "type": ftype, "annotations": []})
            if len(fields) >= 50:
                break
        return fields

    def _extract_methods(self, source: str) -> list[dict]:
        methods = []
        pattern = re.compile(
            r'(?:(?:public|protected|private|static|final|synchronized|abstract)\s+)+'
            r'([\w<>\[\].,\s]+?)\s+(\w+)\s*\(([^)]{0,300})\)\s*(?:throws\s+[\w,\s]+)?\s*[{;]',
        )
        for m in pattern.finditer(source):
            ret = m.group(1).strip()
            name = m.group(2).strip()
            params_raw = m.group(3).strip()
            if name in ("if", "while", "for", "switch", "catch", "class"):
                continue
            # Simple param parsing
            params = [p.strip() for p in params_raw.split(",") if p.strip()] if params_raw else []
            is_public = bool(re.search(r'\bpublic\b', m.group(0)))
            methods.append({
                "name": name,
                "return_type": ret,
                "params": params[:8],
                "annotations": [],
                "is_public": is_public,
            })
            if len(methods) >= 60:
                break
        return methods

    def scan_repo(self, repo_path: Path) -> list[JavaFileInfo]:
        results: list[JavaFileInfo] = []
        skipped = 0
        errors = 0

        all_java = []
        for root, dirs, files in repo_path.walk() if hasattr(repo_path, 'walk') else self._walk(repo_path):
            # Prune skip dirs in-place
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
            for fname in files:
                fpath = root / fname
                if fpath.suffix in SKIP_EXTENSIONS:
                    skipped += 1
                    continue
                if fpath.suffix == ".java":
                    all_java.append(fpath)

        console.print(f"  Found [bold]{len(all_java)}[/bold] .java files to analyze")

        for fpath in all_java:
            info = self.parse_file(fpath)
            if info is None:
                errors += 1
            else:
                results.append(info)

        console.print(
            f"  Parsed [green]{len(results)}[/green] files "
            f"([yellow]{errors} errors[/yellow], {skipped} non-java skipped)"
        )
        return results

    def _walk(self, root: Path):
        """Fallback walk for Python < 3.12."""
        import os
        for dirpath, dirnames, filenames in os.walk(root):
            p = Path(dirpath)
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
            yield p, dirnames, filenames

    @staticmethod
    def summarize_for_llm(info: JavaFileInfo) -> str:
        if info.is_interface:
            kind = "Interface"
        elif info.is_enum:
            kind = "Enum"
        elif info.is_abstract:
            kind = "Abstract Class"
        else:
            kind = "Class"

        ann_str = ", ".join(f"@{a}" for a in info.annotations[:15]) or "none"
        impl_str = ", ".join(info.implements[:8]) or "none"
        queue_str = ", ".join(info.jms_queues[:10]) or "none"

        top_methods = info.methods[:10]
        method_lines = []
        for m in top_methods:
            params = ", ".join(m["params"][:4])
            method_lines.append(f"  {m['return_type']} {m['name']}({params})")
        methods_str = "\n".join(method_lines) or "  (none)"

        top_fields = info.fields[:10]
        fields_str = "\n".join(f"  {f['type']} {f['name']}" for f in top_fields) or "  (none)"

        return (
            f"File: {info.package}/{info.class_name}.java\n"
            f"Type: {kind}\n"
            f"Annotations: {ann_str}\n"
            f"Extends: {info.extends or 'none'}\n"
            f"Implements: {impl_str}\n"
            f"JMS Role: {info.jms_role or 'none'} | Queues: {queue_str}\n"
            f"Methods ({info.method_count}):\n{methods_str}\n"
            f"Fields ({info.field_count}):\n{fields_str}"
        )

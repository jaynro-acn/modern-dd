"""Parse project-level files: pom.xml, build.gradle, properties, READMEs."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml
import xmltodict
from rich.console import Console

console = Console()

JMS_BROKER_PATTERNS = {
    "activemq": ["activemq", "apache.activemq"],
    "ibm-mq": ["ibm.mq", "com.ibm", "wmq", "ibmmq"],
    "jboss-mq": ["jboss", "hornetq", "artemis"],
    "rabbitmq": ["rabbitmq", "amqp"],
    "generic": [],
}


@dataclass
class ProjectInfo:
    name: str
    build_tool: str
    java_version: str
    modules: list[str]
    dependencies: list[dict]
    jms_broker: Optional[str]
    spring_version: Optional[str]
    readme_content: str
    config_properties: dict
    queue_names: list[str]


class ProjectParser:
    def parse(self, repo_path: Path) -> ProjectInfo:
        console.print("  Parsing project structure...")

        name = repo_path.name
        build_tool = "unknown"
        java_version = "unknown"
        modules: list[str] = []
        dependencies: list[dict] = []
        jms_broker: Optional[str] = None
        spring_version: Optional[str] = None

        pom = repo_path / "pom.xml"
        gradle = repo_path / "build.gradle"
        gradle_kts = repo_path / "build.gradle.kts"

        if pom.exists():
            build_tool = "maven"
            name, java_version, modules, dependencies, spring_version = self._parse_maven(pom)
        elif gradle.exists() or gradle_kts.exists():
            build_tool = "gradle"
            gfile = gradle if gradle.exists() else gradle_kts
            name, java_version, modules, dependencies, spring_version = self._parse_gradle(
                gfile, repo_path
            )

        jms_broker = self._detect_jms_broker(dependencies)
        readme_content = self._read_readmes(repo_path)
        config_properties, queue_names = self._read_configs(repo_path)

        # Also pull queue names from dependency config if not found
        if not queue_names:
            queue_names = self._extract_queue_names_from_readmes(readme_content)

        console.print(
            f"  Project: [bold]{name}[/bold] | "
            f"Build: {build_tool} | Java: {java_version} | "
            f"JMS broker: {jms_broker or 'unknown'} | "
            f"Queues found in config: {len(queue_names)}"
        )

        return ProjectInfo(
            name=name,
            build_tool=build_tool,
            java_version=java_version,
            modules=modules,
            dependencies=dependencies,
            jms_broker=jms_broker,
            spring_version=spring_version,
            readme_content=readme_content,
            config_properties=config_properties,
            queue_names=list(dict.fromkeys(queue_names)),
        )

    # ── Maven ─────────────────────────────────────────────────────────────────

    def _parse_maven(
        self, pom_path: Path
    ) -> tuple[str, str, list[str], list[dict], Optional[str]]:
        try:
            raw = pom_path.read_text(encoding="utf-8", errors="replace")
            data = xmltodict.parse(raw)
        except Exception as e:
            console.print(f"  [yellow]⚠ Could not parse pom.xml: {e}[/yellow]")
            return pom_path.parent.name, "unknown", [], [], None

        project = data.get("project", {})
        name = project.get("artifactId") or pom_path.parent.name
        java_version = "unknown"
        spring_version: Optional[str] = None
        modules: list[str] = []
        dependencies: list[dict] = []

        # Java version from compiler plugin
        build = project.get("build", {}) or {}
        plugins_raw = (build.get("plugins") or {}).get("plugin") or []
        if isinstance(plugins_raw, dict):
            plugins_raw = [plugins_raw]
        for plugin in plugins_raw:
            if "compiler" in (plugin.get("artifactId") or ""):
                config = plugin.get("configuration") or {}
                java_version = config.get("release") or config.get("source") or java_version

        # Also check properties
        props = project.get("properties") or {}
        if isinstance(props, dict):
            java_version = (
                props.get("java.version")
                or props.get("maven.compiler.source")
                or props.get("maven.compiler.release")
                or java_version
            )

        # Dependencies
        deps_raw = (project.get("dependencies") or {}).get("dependency") or []
        if isinstance(deps_raw, dict):
            deps_raw = [deps_raw]
        for dep in deps_raw:
            if not isinstance(dep, dict):
                continue
            d = {
                "groupId": dep.get("groupId", ""),
                "artifactId": dep.get("artifactId", ""),
                "version": dep.get("version", ""),
            }
            dependencies.append(d)
            if "spring-boot" in d["artifactId"] or "spring-boot" in d["groupId"]:
                spring_version = spring_version or d["version"]

        # Spring version from dependencyManagement
        dm = (project.get("dependencyManagement") or {}).get("dependencies") or {}
        dm_deps = dm.get("dependency") or []
        if isinstance(dm_deps, dict):
            dm_deps = [dm_deps]
        for dep in dm_deps:
            if isinstance(dep, dict) and "spring-boot" in (dep.get("artifactId") or ""):
                spring_version = spring_version or dep.get("version")
                break

        # Modules
        mods_raw = project.get("modules") or {}
        if isinstance(mods_raw, dict):
            mod_list = mods_raw.get("module") or []
            if isinstance(mod_list, str):
                mod_list = [mod_list]
            modules = mod_list
        # Also parse child poms for their dependencies
        for mod in modules:
            child_pom = pom_path.parent / mod / "pom.xml"
            if child_pom.exists():
                _, _, _, child_deps, sv = self._parse_maven(child_pom)
                dependencies.extend(child_deps)
                spring_version = spring_version or sv

        return name, str(java_version), modules, dependencies, spring_version

    # ── Gradle ────────────────────────────────────────────────────────────────

    def _parse_gradle(
        self, gradle_path: Path, repo_root: Path
    ) -> tuple[str, str, list[str], list[dict], Optional[str]]:
        try:
            source = gradle_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return repo_root.name, "unknown", [], [], None

        name = repo_root.name
        java_version = "unknown"
        spring_version: Optional[str] = None
        modules: list[str] = []
        dependencies: list[dict] = []

        # Project name from settings.gradle
        settings = repo_root / "settings.gradle"
        if not settings.exists():
            settings = repo_root / "settings.gradle.kts"
        if settings.exists():
            stxt = settings.read_text(encoding="utf-8", errors="replace")
            m = re.search(r"rootProject\.name\s*[=:]\s*['\"]([^'\"]+)['\"]", stxt)
            if m:
                name = m.group(1)
            # Subprojects
            includes = re.findall(r"include\s*[\(']([^'\")\n]+)['\)]", stxt)
            for inc in includes:
                modules.extend([p.strip().strip("'\"") for p in inc.split(",")])

        # Java version
        for pattern in [
            r"sourceCompatibility\s*[=:]\s*['\"]?([0-9.]+)['\"]?",
            r"JavaVersion\.VERSION_(\d+)",
            r"javaVersion\s*[=:]\s*['\"]([^'\"]+)['\"]",
            r"release\s*[=:]\s*(\d+)",
        ]:
            m = re.search(pattern, source)
            if m:
                java_version = m.group(1)
                break

        # Dependencies block
        dep_block = re.search(r"dependencies\s*\{([^}]+)\}", source, re.DOTALL)
        if dep_block:
            for m in re.finditer(
                r'(?:implementation|compile|api|runtimeOnly|testImplementation)\s*[\'"]([^\'"]+)[\'"]',
                dep_block.group(1),
            ):
                parts = m.group(1).split(":")
                if len(parts) >= 2:
                    d = {
                        "groupId": parts[0],
                        "artifactId": parts[1],
                        "version": parts[2] if len(parts) > 2 else "",
                    }
                    dependencies.append(d)
                    if "spring-boot" in d["artifactId"]:
                        spring_version = spring_version or d["version"]

        return name, java_version, modules, dependencies, spring_version

    # ── JMS broker detection ──────────────────────────────────────────────────

    def _detect_jms_broker(self, dependencies: list[dict]) -> Optional[str]:
        dep_str = " ".join(
            f"{d.get('groupId','')} {d.get('artifactId','')}" for d in dependencies
        ).lower()
        for broker, patterns in JMS_BROKER_PATTERNS.items():
            if broker == "generic":
                continue
            for pat in patterns:
                if pat in dep_str:
                    return broker
        # If there's any JMS dependency at all
        if "jms" in dep_str or "messaging" in dep_str:
            return "generic"
        return None

    # ── Config files ──────────────────────────────────────────────────────────

    def _read_configs(self, repo_root: Path) -> tuple[dict, list[str]]:
        props: dict = {}
        queue_names: list[str] = []
        queue_keywords = {"queue", "topic", "destination", "channel", "endpoint"}

        for path in repo_root.rglob("*.properties"):
            if any(skip in path.parts for skip in {"target", "build", ".git"}):
                continue
            try:
                for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    if "=" in line:
                        k, _, v = line.partition("=")
                        k, v = k.strip(), v.strip()
                        props[k] = v
                        if any(kw in k.lower() for kw in queue_keywords):
                            queue_names.append(v)
            except OSError:
                pass

        for pattern in ["application*.yml", "application*.yaml", "*.yml", "*.yaml"]:
            for path in repo_root.rglob(pattern):
                if any(skip in path.parts for skip in {"target", "build", ".git"}):
                    continue
                try:
                    raw = path.read_text(encoding="utf-8", errors="replace")
                    data = yaml.safe_load(raw) or {}
                    flat = self._flatten_dict(data)
                    props.update(flat)
                    for k, v in flat.items():
                        if any(kw in k.lower() for kw in queue_keywords) and isinstance(v, str):
                            queue_names.append(v)
                except Exception:
                    pass

        return props, list(dict.fromkeys(queue_names))

    def _flatten_dict(self, d: dict, prefix: str = "") -> dict:
        out: dict = {}
        if not isinstance(d, dict):
            return out
        for k, v in d.items():
            key = f"{prefix}.{k}" if prefix else str(k)
            if isinstance(v, dict):
                out.update(self._flatten_dict(v, key))
            elif isinstance(v, (str, int, float, bool)):
                out[key] = str(v)
        return out

    # ── READMEs ───────────────────────────────────────────────────────────────

    def _read_readmes(self, repo_root: Path) -> str:
        parts: list[str] = []
        for path in sorted(repo_root.rglob("README*")):
            if any(skip in path.parts for skip in {"target", "build", ".git", "node_modules"}):
                continue
            if path.suffix.lower() in {".md", ".txt", ".rst", ""}:
                try:
                    content = path.read_text(encoding="utf-8", errors="replace")
                    parts.append(f"### {path.relative_to(repo_root)}\n\n{content}")
                except OSError:
                    pass
        return "\n\n---\n\n".join(parts)

    def _extract_queue_names_from_readmes(self, readme: str) -> list[str]:
        queues: list[str] = []
        for m in re.finditer(
            r'(?:queue|topic|destination)\s*[:\-=]\s*["\']?([A-Za-z0-9._/:-]{3,80})["\']?',
            readme,
            re.IGNORECASE,
        ):
            queues.append(m.group(1))
        return queues

"""Contract generator — produces AsyncAPI 2.6 and OpenAPI 3.0 contracts from JMS topology."""
from __future__ import annotations

import json
import re
from pathlib import Path

import yaml
from rich.console import Console

console = Console()

_CONTRACT_PROMPT = """You are an API architect generating an AsyncAPI 2.6 contract for a Kafka topic.

JMS QUEUE (legacy): {jms_queue}
KAFKA TOPIC (target): {kafka_topic}
COMPONENTS THAT USE IT: {components}
DESCRIPTION: {description}

Generate an AsyncAPI 2.6 specification for this Kafka topic.
Infer the message payload schema from the JMS queue name, description, and component context.

Respond ONLY with valid JSON that represents the AsyncAPI spec structure:
{{
  "asyncapi": "2.6.0",
  "info": {{
    "title": "string",
    "version": "1.0.0",
    "description": "string"
  }},
  "channels": {{
    "{kafka_topic}": {{
      "description": "string",
      "subscribe": {{
        "operationId": "string",
        "message": {{
          "name": "string",
          "contentType": "application/json",
          "payload": {{
            "type": "object",
            "properties": {{}}
          }}
        }}
      }},
      "publish": {{
        "operationId": "string",
        "message": {{
          "$ref": "#/channels/{kafka_topic_ref}/subscribe/message"
        }}
      }}
    }}
  }},
  "components": {{
    "schemas": {{}}
  }}
}}"""

_OPENAPI_PROMPT = """You are an API architect generating an OpenAPI 3.0 contract for a REST service.

SERVICE NAME: {service_name}
CONTEXT: {context}
METHODS/ENDPOINTS DETECTED: {methods}

Generate an OpenAPI 3.0.3 specification. Infer realistic endpoints from the method names and context.

Respond ONLY with valid JSON representing the OpenAPI spec:
{{
  "openapi": "3.0.3",
  "info": {{
    "title": "string",
    "version": "1.0.0",
    "description": "string"
  }},
  "paths": {{
    "/path": {{
      "post": {{
        "summary": "string",
        "requestBody": {{}},
        "responses": {{}}
      }}
    }}
  }}
}}"""


class ContractGenerator:
    def __init__(self, llm_adapter, token_tracker):
        self.llm = llm_adapter
        self.tracker = token_tracker

    # ── Public ───────────────────────────────────────────────────────────────

    def generate(self, plan, model, output_dir: Path) -> None:
        asyncapi_dir = output_dir / "contracts" / "asyncapi"
        openapi_dir = output_dir / "contracts" / "openapi"
        asyncapi_dir.mkdir(parents=True, exist_ok=True)
        openapi_dir.mkdir(parents=True, exist_ok=True)

        self._generate_asyncapi(plan, model, asyncapi_dir)
        self._generate_openapi(model, openapi_dir)

    # ── AsyncAPI ─────────────────────────────────────────────────────────────

    def _generate_asyncapi(self, plan, model, asyncapi_dir: Path) -> None:
        console.print("[cyan]  Generating AsyncAPI contracts...[/cyan]")

        # Build JMS→Kafka mapping from plan + model
        jms_queues = {q.get("name", ""): q for q in (model.jms_queues or [])}
        topic_mapping = plan.arch_option.kafka_topic_mapping or {}

        generated = 0
        for jms_name, kafka_topic in topic_mapping.items():
            queue_info = jms_queues.get(jms_name, {})
            components_using = queue_info.get("producers", []) + queue_info.get("consumers", [])

            kafka_topic_ref = kafka_topic.replace(".", "-").replace("/", "-")
            prompt = _CONTRACT_PROMPT.format(
                jms_queue=jms_name,
                kafka_topic=kafka_topic,
                kafka_topic_ref=kafka_topic_ref,
                components=json.dumps(components_using),
                description=queue_info.get("description", f"Migrated from JMS queue {jms_name}"),
            )

            try:
                response = self.llm.complete(prompt, model_tier="smart")
                self.tracker.record(
                    phase="phase4",
                    file=f"asyncapi_{kafka_topic}",
                    model=response.model,
                    provider=response.provider,
                    prompt_tokens=response.prompt_tokens,
                    completion_tokens=response.completion_tokens,
                )

                spec_dict = self._parse_json(response.text)
                if not spec_dict:
                    spec_dict = self._default_asyncapi(kafka_topic, jms_name)

                filename = re.sub(r"[^a-z0-9\-]", "-", kafka_topic.lower()) + ".yaml"
                (asyncapi_dir / filename).write_text(
                    yaml.dump(spec_dict, default_flow_style=False, allow_unicode=True),
                    encoding="utf-8",
                )
                generated += 1

            except Exception as exc:
                console.print(f"    [red]✗ {kafka_topic}: {exc}[/red]")

        # Generate stubs for any queues in model that don't have a mapping
        for queue in (model.jms_queues or []):
            q_name = queue.get("name", "")
            if q_name and q_name not in topic_mapping:
                topic_guess = "topic." + re.sub(r"[^a-z0-9]", ".", q_name.lower()).strip(".")
                spec_dict = self._default_asyncapi(topic_guess, q_name)
                filename = re.sub(r"[^a-z0-9\-]", "-", topic_guess.lower()) + ".yaml"
                (asyncapi_dir / filename).write_text(
                    yaml.dump(spec_dict, default_flow_style=False, allow_unicode=True),
                    encoding="utf-8",
                )
                generated += 1

        console.print(f"[green]  ✓ {generated} AsyncAPI contract(s) written to {asyncapi_dir}[/green]")

    # ── OpenAPI ──────────────────────────────────────────────────────────────

    def _generate_openapi(self, model, openapi_dir: Path) -> None:
        console.print("[cyan]  Generating OpenAPI contracts...[/cyan]")

        # Find REST-exposed components
        rest_components = [
            c for c in (model.components or [])
            if c.get("jms_role") not in ("producer", "consumer", "both")
        ]
        # Also find contracts of type "rest"
        rest_contracts: dict[str, list] = {}
        for contract in (model.contracts or []):
            if contract.get("type") == "rest":
                comp = contract.get("component", "unknown")
                rest_contracts.setdefault(comp, []).append(contract)

        generated = 0
        seen = set()
        for comp_name, contracts in rest_contracts.items():
            if comp_name in seen:
                continue
            seen.add(comp_name)

            methods = [c.get("name", "") for c in contracts]
            context_name = next(
                (c.get("context", "") for c in (model.components or []) if c.get("name") == comp_name), ""
            )

            prompt = _OPENAPI_PROMPT.format(
                service_name=comp_name,
                context=context_name,
                methods=json.dumps(methods),
            )

            try:
                response = self.llm.complete(prompt, model_tier="smart")
                self.tracker.record(
                    phase="phase4",
                    file=f"openapi_{comp_name}",
                    model=response.model,
                    provider=response.provider,
                    prompt_tokens=response.prompt_tokens,
                    completion_tokens=response.completion_tokens,
                )

                spec_dict = self._parse_json(response.text)
                if not spec_dict:
                    spec_dict = self._default_openapi(comp_name)

                filename = re.sub(r"[^a-z0-9\-]", "-", comp_name.lower()) + ".yaml"
                (openapi_dir / filename).write_text(
                    yaml.dump(spec_dict, default_flow_style=False, allow_unicode=True),
                    encoding="utf-8",
                )
                generated += 1

            except Exception as exc:
                console.print(f"    [red]✗ {comp_name}: {exc}[/red]")

        console.print(f"[green]  ✓ {generated} OpenAPI contract(s) written to {openapi_dir}[/green]")

    # ── Defaults (fallback stubs) ─────────────────────────────────────────────

    @staticmethod
    def _default_asyncapi(kafka_topic: str, jms_source: str) -> dict:
        return {
            "asyncapi": "2.6.0",
            "info": {
                "title": kafka_topic,
                "version": "1.0.0",
                "description": f"Migrated from JMS queue: {jms_source}",
            },
            "channels": {
                kafka_topic: {
                    "description": f"Events migrated from {jms_source}",
                    "subscribe": {
                        "operationId": f"receive_{re.sub(r'[^a-z0-9]', '_', kafka_topic.lower())}",
                        "message": {
                            "name": "Message",
                            "contentType": "application/json",
                            "payload": {
                                "type": "object",
                                "properties": {
                                    "id": {"type": "string"},
                                    "timestamp": {"type": "string", "format": "date-time"},
                                    "payload": {"type": "object"},
                                },
                            },
                        },
                    },
                }
            },
        }

    @staticmethod
    def _default_openapi(service_name: str) -> dict:
        return {
            "openapi": "3.0.3",
            "info": {
                "title": service_name,
                "version": "1.0.0",
                "description": f"REST API for {service_name}",
            },
            "paths": {},
        }

    @staticmethod
    def _parse_json(text: str) -> dict:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group())
            except json.JSONDecodeError:
                pass
        return {}

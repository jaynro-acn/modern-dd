"""LLM prompt templates for the Legacy Modernization Accelerator."""

DDD_EXTRACT_PROMPT = """You are a senior software architect specializing in Domain-Driven Design (DDD) and legacy Java modernization.

Project: {project_name} | Java {java_version} | JMS Broker: {jms_broker}
Package group: {group_name}

Analyze these Java class summaries and extract DDD concepts:

{file_summaries}

Respond ONLY with valid JSON in this exact structure:
{{
  "domains": [
    {{"name": "string", "description": "string", "confidence": 0.8}}
  ],
  "subdomains": [
    {{"name": "string", "domain": "string", "description": "string", "type": "core|supporting|generic", "confidence": 0.8}}
  ],
  "bounded_contexts": [
    {{"name": "string", "subdomain": "string", "description": "string", "ubiquitous_language": "string", "key_terms": ["string"], "confidence": 0.8}}
  ],
  "capabilities": [
    {{"name": "string", "level": "L1|L2|L3", "parent": null, "description": "string"}}
  ],
  "components": [
    {{"name": "string", "type": "service|gateway|repository|listener|producer|unknown", "context": "string", "description": "string", "jms_role": "producer|consumer|both|null", "depends_on": [], "source_files": ["string"]}}
  ],
  "contracts": [
    {{"name": "string", "type": "rest|event|rpc|unknown", "component": "string", "description": "string"}}
  ],
  "interfaces": [
    {{"name": "string", "component": "string", "description": "string", "methods": ["string"]}}
  ],
  "jms_queues": [
    {{"name": "string", "producers": [], "consumers": [], "message_type": null, "description": "string"}}
  ],
  "gaps": [
    {{"description": "string", "hypothesis": "string", "evidence_needed": "string", "confidence": 40}}
  ]
}}

Rules:
- Use package structure to infer domain/subdomain boundaries.
- @JmsListener/@MessageDriven = consumer. JmsTemplate/MessageProducer = producer.
- Note ubiquitous language conflicts (same term, different meaning in different contexts).
- If confidence < 0.5, add a gap entry instead of guessing.
- Output ONLY the JSON — no markdown, no explanation."""


VALUE_STREAM_PROMPT = """You are a senior enterprise architect applying Domain-Driven Design strategic patterns.

Project: {project_name}

Domains identified:
{domains}

Key components:
{components}

README context:
{readme}

Identify 1–4 value streams this application participates in, and map capabilities (L1/L2/L3) to each.

Respond ONLY with valid JSON:
{{
  "value_streams": [
    {{
      "name": "string",
      "description": "string",
      "domains_involved": ["domain names"]
    }}
  ],
  "capabilities": [
    {{"name": "string", "level": "L1|L2|L3", "parent": null, "description": "string"}}
  ]
}}

Output ONLY the JSON — no markdown, no explanation."""


GAP_ANALYSIS_PROMPT = """You are a senior software architect performing a gap analysis on a DDD model extracted from a legacy Java 8 JMS application.

Project: {project_name} | Java {java_version} | JMS Broker: {jms_broker}

DDD model summary:
{model_summary}

README context:
{readme}

Identify gaps, ambiguities, and hypotheses in this model:
- Unclear domain/bounded context boundaries
- Components with ambiguous ownership
- JMS queues with unknown purpose or message contracts
- Ubiquitous language conflicts
- Missing business capabilities
- Unclear integration patterns

Respond ONLY with valid JSON:
{{
  "gaps": [
    {{
      "description": "string (detailed explanation)",
      "hypothesis": "string (your best guess)",
      "evidence_needed": "string (what would resolve this)",
      "confidence": 40,
      "category": "domain-boundary|bounded-context|jms-topology|ubiquitous-language|capability|integration|other"
    }}
  ]
}}

Output ONLY the JSON — no markdown, no explanation."""


ARCH_PROPOSAL_PROMPT = """You are a principal architect specializing in modernizing Java 8 JMS applications to event-driven microservices.

## Current System DDD Model
{ddd_model}

## JMS Topology (Legacy)
{jms_topology}

## Resolved Gaps & Decisions
{resolved_gaps}

## Target Constraints
- Source: Java 8, JMS (ActiveMQ / IBM MQ / RabbitMQ)
- Target: Java 17, Apache Kafka
- Cloud preference: {cloud_preference}
- Each bounded context becomes one or more microservices
- JMS queues/topics become Kafka topics

## Task
Propose 3 target architecture options, ranging from conservative to progressive.

Respond ONLY with valid JSON:
{{
  "options": [
    {{
      "id": "OPT-1",
      "name": "string (short name)",
      "description": "string (2-3 sentences)",
      "rationale": "string (why this fits the current system)",
      "risks": ["string"],
      "kafka_topology": "string (describe topic naming, consumer groups, partition strategy)",
      "microservice_mapping": [
        {{"bounded_context": "string", "service_name": "string", "kafka_topics": ["string"]}}
      ],
      "target_java": "17",
      "cloud": "azure|gcp|multi-cloud",
      "migration_complexity": "low|medium|high",
      "effort_weeks_estimate": 0
    }}
  ]
}}

Output ONLY the JSON — no markdown, no explanation."""


SPEC_GENERATION_PROMPT = """You are a senior software engineer writing a technical specification following the UKP spec format.

## Bounded Context
{bounded_context}

## Selected Architecture
{selected_architecture}

## Migration Plan Phase
{migration_phase}

## Source Components (Legacy Java 8)
{source_components}

## Task
Generate a technical spec (spec.md) and implementation plan (plan.md) for modernizing this bounded context.

The spec.md follows this structure:
- Status: Draft
- Objective: one paragraph
- Boundaries (in-scope, out-of-scope)
- Testing Strategy (TDD / integration / manual)
- Acceptance Criteria (checkbox list, each testable)

The plan.md follows this structure:
- Status: Drafting
- Design (LLD): data & schema, interfaces & contracts, component decomposition, state & control flow, failure & resilience
- Tasks (ordered list with: task name, description, tests, approach, depends-on)
- Rollout

Respond ONLY with valid JSON:
{{
  "spec_md": "string (full markdown content of spec.md)",
  "plan_md": "string (full markdown content of plan.md)"
}}

Output ONLY the JSON — no markdown wrapper, no explanation."""


CONTRACT_GENERATION_PROMPT = """You are a senior API architect generating AsyncAPI 2.6 specifications for Kafka topics.

## Legacy JMS Queue/Topic
{jms_queue}

## Kafka Topic Mapping
- JMS destination: {jms_name}
- Kafka topic: {kafka_topic}
- Producer service: {producer}
- Consumer service: {consumer}
- Message type / payload (inferred): {message_type}

## Selected Architecture
{selected_architecture}

## Task
Generate a complete AsyncAPI 2.6 YAML specification for this Kafka topic.

Include:
- asyncapi: 2.6.0
- info: title, version, description
- servers: kafka broker (localhost:9092 as placeholder)
- channels: one channel per topic
- components/schemas: message payload schema (infer from JMS message type name and context)
- operationId for publish and subscribe
- message headers (correlationId, eventType, sourceService, timestamp)

Respond ONLY with valid JSON:
{{
  "kafka_topic": "string",
  "asyncapi_yaml": "string (complete AsyncAPI 2.6 YAML)"
}}

Output ONLY the JSON — no markdown, no explanation."""


MIGRATION_PLAN_PROMPT = """You are a principal architect creating a phased migration plan for a Java 8 JMS application.

## DDD Model
{ddd_model}

## Selected Architecture
{selected_architecture}

## JMS Topology
{jms_topology}

## Task
Create a phased migration plan from Java 8 JMS to Java 17 Kafka using the strangler-fig pattern.

Also create an Architecture Decision Record (ADR) documenting the target architecture choice.

Respond ONLY with valid JSON:
{{
  "phases": [
    {{
      "number": 1,
      "name": "string",
      "description": "string",
      "components": ["bounded context or service names"],
      "effort_weeks": 0,
      "deliverables": ["string"],
      "dependencies": ["phase names this depends on"]
    }}
  ],
  "adr": {{
    "number": "0001",
    "title": "string",
    "status": "Accepted",
    "context": "string (why a decision was needed)",
    "decision": "string (what was decided)",
    "rationale": "string (why this over alternatives)",
    "consequences": "string (trade-offs and implications)"
  }},
  "open_items": [
    {{
      "id": "OI-001",
      "description": "string",
      "owner": "architect|developer|product|operations",
      "priority": "high|medium|low"
    }}
  ]
}}

Output ONLY the JSON — no markdown, no explanation."""

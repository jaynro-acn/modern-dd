# Legacy Modernization Accelerator

A self-contained Python CLI that reverse-engineers Java 8 JMS applications into a validated DDD knowledge graph, then produces specs and contracts ready for spec-driven modernization to Java 17 + Kafka on Azure or GCP.

Runs fully local — Neo4j + Qdrant on your machine. No cloud required to run the tool itself.

---

## How it works — four phases, two commands

```
python main.py analyze --repo PATH --docs PATH
```
Runs **Phase 1** (code analysis → DDD extraction → Neo4j + markdowns) and **Phase 2** (document ingestion → Qdrant → gap cross-reference). Exits with a gap report for you to review.

```
python main.py continue --project NAME
```
Validates all gaps are resolved, then runs **Phase 3** (target architecture proposal → you select → migration plan) and **Phase 4** (spec.md + plan.md per bounded context + AsyncAPI/OpenAPI contracts).

---

## Prerequisites

- macOS with Homebrew (or Linux with equivalent)
- Python 3.12
- Neo4j (local) — `brew install neo4j && brew services start neo4j`
- Java 17+ (required by Neo4j) — installed automatically by Homebrew

---

## Setup

### 1. Clone and create virtual environment

```bash
git clone <this-repo>
cd legacy-modernizer
python3.12 -m venv .venv312
source .venv312/bin/activate
pip install -r requirements.txt
```

### 2. Configure environment

```bash
cp .env.example .env
```

Edit `.env`:

```ini
LLM_PROVIDER=gemini
LLM_API_KEY=your-gemini-api-key

NEO4J_URI=bolt://localhost:7687
NEO4J_USER=neo4j
NEO4J_PASSWORD=your-neo4j-password

# Optional — defaults to ./outputs
OUTPUT_DIR=outputs
```

Get a Gemini API key at https://aistudio.google.com/app/apikey (free tier available).

### 3. Start Neo4j

```bash
brew services start neo4j
# Set password on first run:
echo "ALTER CURRENT USER SET PASSWORD FROM 'neo4j' TO 'your-password';" | \
  cypher-shell -u neo4j -p neo4j -d system
```

---

## Usage

### Phase 1 + 2 — Analyze

```bash
python main.py analyze \
  --repo /path/to/legacy-java-repo \
  --docs /path/to/documents-folder \
  [--project-name my-project]
```

**Options:**

| Flag | Description |
|---|---|
| `--repo PATH` | Path to the legacy Java 8 repository (required) |
| `--docs PATH` | Folder with PDFs, .md, .txt, .docx to ingest (required) |
| `--project-name NAME` | Defaults to the repo folder name |
| `--skip-neo4j` | Skip writing to Neo4j (markdowns still generated) |
| `--skip-qdrant` | Skip document ingestion |
| `--dry-run` | Scan and classify files, print stats — no LLM calls or DB writes |

**What gets analyzed:**

- `.java` files — package structure, annotations, JMS queues/topics, interfaces, services, DTOs
- `pom.xml` / `build.gradle` — modules, Java version, dependencies, JMS broker detection
- `application.properties` / `application.yml` / `.properties` — config, queue names
- `README*.md` and any other `.md` files

**Skipped:** `.class`, `.jar`, `.war`, binaries, `target/`, `build/`, `.git/`

**Outputs written to `outputs/<project-name>/`:**

```
outputs/<project>/
  ddd/
    value-streams.md
    domains.md
    capabilities.md
    bounded-contexts.md
    components.md
    contracts.md
    jms-topology.md
    ubiquitous-language.md
  architecture/
    overview.md
    interfaces.md
    dependencies.md
  gaps/
    gaps.md          <- review and resolve these
    decisions.md     <- record your decisions here
  logs/
    token_usage.jsonl
    session_summary.md
```

### Review and resolve gaps

Open `outputs/<project>/gaps/gaps.md`. For each gap, change:

```markdown
- **Status**: OPEN
```

to either:

```markdown
- **Status**: RESOLVED
```
(you confirmed or disproved the hypothesis)

```markdown
- **Status**: ACCEPTED
```
(you acknowledge it and want to proceed with the hypothesis as-is)

Record any decisions in `gaps/decisions.md`.

### Phase 3 + 4 — Continue

Once all gaps are resolved:

```bash
python main.py continue --project my-project
```

The tool will:
1. Validate no OPEN gaps remain (exits if any found — prints the list)
2. Query Neo4j + Qdrant to propose 2–3 target architecture options
3. Prompt you to select one interactively
4. Generate the migration plan and ADRs
5. Generate `spec.md` + `plan.md` per bounded context
6. Generate AsyncAPI 2.6 contracts (Kafka topics) and OpenAPI 3.0 contracts (REST)

**Additional outputs:**

```
outputs/<project>/
  architecture/
    proposals.md
    migration-plan.md
    kafka-topics.md
    open-items.md
    adrs/
      ADR-0001-*.md
  specs/
    <bounded-context>/
      spec.md
      plan.md
  contracts/
    asyncapi/
      <topic-name>.yaml
    openapi/
      <service-name>.yaml
```

---

## Smart model selection

The tool routes each Java file to the appropriate LLM tier based on its role:

| Category | Signal | Model |
|---|---|---|
| DTO / POJO | `@Data`, `@Getter`, `@Setter`, few methods | Gemini Flash (cheap) |
| Interface | `interface` keyword | Gemini Flash |
| Config class | `@Configuration`, `@Bean` | Gemini Flash |
| Enum / Exception | `enum`, extends `*Exception` | Gemini Flash |
| Repository | `@Repository`, extends `*DAO` | Gemini Flash |
| JMS Listener | `@JmsListener`, `@MessageDriven`, `onMessage()` | Gemini Pro |
| Service with logic | `@Service` + non-trivial methods | Gemini Pro |
| Domain class | Complex business logic, many methods | Gemini Pro |

Use `--dry-run` first to see the split before spending tokens.

---

## Token tracking & cost

Every LLM call is logged to `outputs/<project>/logs/token_usage.jsonl`:

```json
{"timestamp":"...","phase":"phase1","file":"OrderService.java","model":"gemini-1.5-flash","prompt_tokens":1240,"completion_tokens":380,"cost_usd":0.000207}
```

At the end of each command, a cost table is printed and `logs/session_summary.md` is written. The JSONL file is append-only — cumulative cost across all runs is tracked automatically.

---

## Switching LLM providers

Only Gemini is implemented. To add another provider, set `LLM_PROVIDER` in `.env` and implement `_complete_<provider>` in `llm/adapter.py`. The `LLMResponse` dataclass interface stays the same.

---

## Graph schema (Neo4j)

Follows the UKP node hierarchy:

```
ValueStream -[INVESTS_IN]->   Capability (L1)
Capability  -[HAS_CHILD]->    Capability (L2/L3)
Domain      -[CONTAINS]->     Subdomain
Subdomain   -[CONTAINS]->     BoundedContext
Component   -[IMPLEMENTS]->   BoundedContext
Component   -[DEPENDS_ON]->   Component
Component   -[EXPOSES]->      Contract
Component   -[PRODUCES_TO]->  JmsQueue
Component   -[CONSUMES_FROM]->JmsQueue
```

All nodes carry a `project` property so multiple projects coexist in the same Neo4j instance.

---

## Project structure

```
legacy-modernizer/
├── main.py                          # CLI — analyze | continue
├── config.yaml                      # skip lists, batch sizes, cloud options
├── requirements.txt
├── .env.example
├── llm/
│   ├── adapter.py                   # LLM provider abstraction (Gemini)
│   ├── classifier.py                # Fast vs Smart model routing per Java file
│   └── prompts.py                   # All LLM prompt templates
├── tracking/
│   └── token_tracker.py             # Per-call cost logging + rich summary
├── graph/
│   ├── schema.py                    # Neo4j constraint setup
│   └── queries.py                   # Cypher helpers
├── vector/
│   └── collections.py               # Qdrant embedded-mode manager
├── phases/
│   ├── phase1_analysis/
│   │   ├── java_parser.py           # Regex-based Java file parser
│   │   ├── project_parser.py        # pom.xml, Gradle, properties, READMEs
│   │   ├── ddd_extractor.py         # LLM DDD extraction orchestrator
│   │   └── neo4j_writer.py          # UKP schema writer + markdown artifacts
│   ├── phase2_ingestion/
│   │   ├── document_loader.py       # PDF / txt / md / docx chunker
│   │   ├── qdrant_writer.py         # Embedding + Qdrant ingestion
│   │   └── gap_cross_reference.py   # Semantic gap resolution from documents
│   ├── phase3_architecture/
│   │   ├── arch_proposer.py         # Architecture options + interactive selection
│   │   └── migration_planner.py     # Migration plan + ADRs + open items
│   └── phase4_specs/
│       ├── spec_generator.py        # spec.md + plan.md per bounded context
│       └── contract_generator.py    # AsyncAPI 2.6 + OpenAPI 3.0
└── outputs/                         # Generated per project — gitignored
```

# NEXUS
NEXUS is an agentic platform where specialized AI agents collaborate like a real proposal team—analyzing the RFP, deriving requirements, designing solutions, preparing the proposal, reviewing it, and maintaining traceability back to the source.

NEXUS will turn a business requirements document (BRD) and non-functional requirements (NFRs) into a functional document, a solution architecture diagram, and a technology stack recommendation for human review.

The current milestone is **BRD ingestion and vector retrieval**. Python reads a Word BRD, groups the text into chunks, and uses MiniLM to embed them on the local CPU. Neo4j Aura stores the chunks and vectors and returns ranked passages for a question. The current commands run without Claude credentials, Ollama, or a hosted embedding API. Aura is cloud-hosted, so upload only sample or approved material.

## 1. Clone and install

Clone the NEXUS repository and open a terminal at its root, the folder containing `backend/`. Use a Python version supported by PyTorch on your machine. Python 3.12 is a starting point for a fresh environment.

**Windows**

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r backend\requirement.txt
```

**macOS or Linux**

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -r backend/requirement.txt
```

The remaining commands use `python` and assume the virtual environment is active. Activate it with `.\.venv\Scripts\Activate.ps1` in PowerShell, `.venv\Scripts\activate.bat` in Command Prompt, or `source .venv/bin/activate` in bash or zsh. If PowerShell blocks activation, use `.\.venv\Scripts\python.exe` directly in place of `python`.

The dependency file is named **`backend/requirement.txt`** in this project. It includes `python-docx`, `sentence-transformers`, `neo4j`, and `python-dotenv`. The first embedding run needs access to Hugging Face to download `sentence-transformers/all-MiniLM-L6-v2`. The model then runs on your CPU.

## 2. Prepare the BRD chunks

For the same results as the original XATA demo, use the **reviewed** `backend/data/output/brd_chunks.json` containing 17 chunks. If it is included in your clone, go straight to the preview in step 3. The repository maintainer should include this JSON with the approved sample BRD so a teammate can reproduce the manual split.

To reproduce the Word extraction and chunking:

1. Place the approved sample at `backend/data/documents/XATA_TODO_BRD.docx`. If the input is a legacy `.doc`, save it as `.docx` first. Check that the source paths in the scripts match this filename.
2. From the repository root, change into `backend` and run the scripts.

   ```text
   cd backend
   python app/ingestion/read_brd.py
   python app/ingestion/chunk_brd.py
   cd ..
   ```

   `read_brd.py` prints paragraphs, heading styles, and table rows in document order. Compare a heading, a paragraph, and a requirement row against Word. `chunk_brd.py` groups the content by heading and saves `backend/data/output/brd_chunks.json`. Each record has `chunk_id`, `source`, `heading`, and `text`.

3. Check the split of section **6. Business Requirements**. It takes 316 tokens against the model's 256-token limit, so `chunk_brd.py` splits it automatically into `XATA_TODO_BRD-008-A` and `XATA_TODO_BRD-008-B` at a table-row boundary (BR-001 through BR-007, then BR-008 through BR-015) and repeats the `ID | Requirement | Priority` header in both. Sections that fit keep a plain `-NNN` ID. The script counts tokens the same way the embedding step does and prints each chunk's count. No manual edit is needed.

   Chunk the NFR the same way with `python app/ingestion/chunk_brd.py nfr`, which writes `nfr_chunks.json`.

## 3. Preview an embedding

From the repository root:

```text
python backend/app/embeddings/brd_vectors.py preview
```

Expect a chunk ID, `Dimensions: 384`, and the first five vector values. The script embeds the heading and passage text together. It keeps the ID and source filename as metadata. The preview checks the selected chunk and requires no Neo4j connection. The upload checks every chunk and stops before writing the batch if an input is too long.

## 4. Set up Neo4j Aura

1. Sign in to the [Neo4j Aura Console](https://console.neo4j.io/) and create an **AuraDB Free** instance, or use a dedicated existing instance for the demo.
2. Save the database credentials provided by Aura. Copy the **Connection URI** starting with `neo4j+s://`.
3. Open **Studio → Query**, connect to the instance, and check the **Database** selector. Use the actual database name shown there or in the Aura credentials. The instance's display name can differ from the database name.
4. Create `backend/.env` with your own values:

   ```dotenv
   NEO4J_URI=neo4j+s://YOUR_INSTANCE_ID.databases.neo4j.io
   NEO4J_USERNAME=YOUR_DATABASE_USERNAME
   NEO4J_PASSWORD=YOUR_DATABASE_PASSWORD
   NEO4J_DATABASE=YOUR_ACTUAL_DATABASE_NAME
   ```

5. In Aura Query, run `RETURN 'Connected' AS status;` to verify your browser connection.

The URI identifies where the driver connects. The database name identifies where the Cypher runs. Keep `backend/.env` and the downloaded credentials out of Git. If a teammate uses a separate Aura instance, they must create the index and upload the sample in their own database.

## 5. Create the vector index

With the same database selected in Aura Query, run:

```cypher
CREATE VECTOR INDEX nexus_brd_embeddings IF NOT EXISTS
FOR (c:NexusChunk) ON (c.embedding)
OPTIONS {
  indexConfig: {
    `vector.dimensions`: 384,
    `vector.similarity_function`: 'cosine'
  }
};
```

The index tells Neo4j how to search the `embedding` property on nodes labeled `NexusChunk`. The 384 dimensions match MiniLM, and the index name matches the Python search query. Run this separately to verify it:

```cypher
SHOW VECTOR INDEXES
YIELD name, state, populationPercent, options
WHERE name = 'nexus_brd_embeddings'
RETURN name, state, populationPercent, options;
```

Check that the index is `ONLINE` with 384 dimensions and cosine similarity. If it is `POPULATING`, check again shortly. If an index already exists under that name, `IF NOT EXISTS` preserves its settings. Use a dedicated database for this demo if the existing index has a different configuration.

## 6. Upload the chunks

```text
python backend/app/embeddings/brd_vectors.py upload
```

Python reads the reviewed JSON, generates one embedding per chunk, and writes the nodes and embeddings to Aura. Neo4j creates nodes as needed with `MERGE` on the source filename and chunk ID. Repeating the same upload updates those records. Changed or removed chunk IDs can leave old nodes behind, so use a fresh demo database when comparing different chunking strategies.

Verify the XATA sample in Aura Query:

```cypher
MATCH (c:NexusChunk {source: 'XATA_TODO_BRD.docx'})
RETURN count(c) AS chunks,
       count(c.embedding) AS embeddings,
       collect(DISTINCT size(c.embedding)) AS dimensions;
```

For the reviewed sample in a clean database, expect **17 chunks**, **17 embeddings**, and **`[384]`**.

## 7. Run a retrieval smoke test

```text
python backend/app/embeddings/brd_vectors.py search "What roles does the app require?"
```

Look for **`XATA_TODO_BRD-007`**, which ranked first in the original run and lists Employee, Team Lead, and Administrator. The search prints the similarity score, source, chunk ID, heading, and passage text. It embeds the question locally with the same model and queries the Aura index. The Word document and JSON are only needed for ingestion and inspection.

Two more queries reproduce the retrieval experiments:

| Query | What to look for |
|---|---|
| Who can assign a task to another user? | `007` describes the Team Lead role, `008-A` contains BR-005, and `012` states the assigner's authorization rule. With a limit of 10, these appeared at ranks 1, 6, and 9 in the original run. |
| Which fields does a user enter or select when creating a TODO? | `009` describes the workflow, and `008-A` describes the fields the saved item must contain. In the top-10 run they appeared at ranks 7 and 4, respectively. |

If your checkout uses `LIMIT 3` inside the Cypher `SEARCH` clause, temporarily use `LIMIT 10` to inspect more candidates. These ranks are observations from the sample run. Check whether the evidence appears and answers the question.

## 8. Ask a question with Claude

```text
python backend/app/embeddings/brd_vectors.py ask "What roles does the app require?" --dry-run
python backend/app/embeddings/brd_vectors.py ask "What roles does the app require?"
```

`ask` retrieves the top five chunks with the same search as step 7, sends them to Claude, and checks every `[CHUNK_ID]` citation in the answer against the retrieved chunks. Cited IDs that were not retrieved, or an answer with no citations, print a warning. `--dry-run` shows the retrieved passages and the exact prompt without calling Claude, so it needs no API key.

For a live answer add `ANTHROPIC_API_KEY` to `backend/.env`. `ANTHROPIC_MODEL` is optional and defaults to `claude-sonnet-5-5`. The retrieval code lives in `backend/app/rag/retrieval.py` for reuse by the agents.

## 9. Generate the functional document

```text
python backend/app/agents/functional_analyst.py --dry-run
python backend/app/agents/functional_analyst.py
```

The Functional Analyst loads **every** BRD chunk from Aura (not just search hits) and parses the requirement table in code to build a checklist of requirement IDs and priorities. Claude returns a structured document (requirements, business rules, workflows, Given/When/Then acceptance criteria, open questions, assumptions), each item citing chunk IDs. A deterministic check then confirms that all citations are real chunks, every requirement is described, and every `Must` requirement has an acceptance criterion. Failures go back to Claude for up to two repair rounds, and anything still failing is listed in the output.

It writes `backend/data/output/functional_document.md` and `functional_document.json`. `--dry-run` prints the checklist and prompt size without calling Claude. The model comes from `ANTHROPIC_MODEL` (default `claude-sonnet-5-5`).

## 10. Generate the architecture

Run the Functional Analyst first, because the Architect reads its `functional_document.json`.

```text
python backend/app/agents/solution_architect.py --dry-run
python backend/app/agents/solution_architect.py
```

The Solution Architect combines the functional document with every BRD and NFR chunk from Aura. Code parses the numbered NFR targets (`NFR-PERF-001` and so on) and the category priorities from the NFR priority section. Claude returns design decisions with rationale and alternatives, a technology stack with assumptions, components and data flows, NFR coverage per category, assumptions, and risks. The Mermaid diagram is generated in code from the components and flows, so its syntax is always valid.

A deterministic check confirms that citations are real chunks, every `Must` requirement and numbered NFR target is referenced by a decision, every `Must` NFR category is covered, and every flow endpoint and component is consistent. Failures go back to Claude for up to two repair rounds, and anything still failing is listed in the output.

It writes `backend/data/output/architecture.md` (including the fenced `mermaid` diagram) and `architecture.json`.

## 11. Generate the project plan

Run the Functional Analyst and the Solution Architect first, because the Project Manager reads their JSON output.

```text
python backend/app/agents/project_manager.py --dry-run
python backend/app/agents/project_manager.py [--start-date 2027-01-11]
```

The Project Manager does three jobs. It reviews the BRD and NFR for objectives, scope, assumptions, constraints, stakeholders, dependencies and any stated timelines, with chunk citations. It determines roles, skills, FTE, onboarding and availability assumptions from the architecture's tech-stack layers. It prepares phases, milestones, releases and a roadmap. Claude supplies phase durations and dependencies only; the code computes the week schedule, critical path, staffing per phase and the Mermaid Gantt chart.

The documents give no start date, deadline, budget, team or availability, so without more input every duration, FTE and date is an **estimate** and the missing facts are listed under *Information gaps*. To supply real facts, copy `backend/data/inputs/planning_inputs.example.json` to `planning_inputs.json` and edit it (start date, go-live, available people with skills and availability, constraints). Those values are then treated as facts. `--start-date` overrides the start date; with neither, the Gantt chart uses a nominal next-Monday placeholder.

A code check confirms that citations are real chunks, every `Must` requirement is in a release (nothing is silently dropped), the phase graph has no cycles, every external dependency is resolved by an onboarding item or phase, and every tech-stack layer has a role. Failures go back to Claude for up to two repair rounds and any left are listed in the output.

It writes `backend/data/output/project_plan.md` and `project_plan.json`.

## 12. Run everything with the orchestrator

```text
python backend/app/orchestrator.py run --dry-run
python backend/app/orchestrator.py run
python backend/app/orchestrator.py status
python backend/app/orchestrator.py approve <run_id> [--version N]
```

`run` fetches the BRD and NFR once, then runs the Functional Analyst, Solution Architect and Project Manager in that order. After each pass, code gathers findings: any problems an agent could not resolve itself, plus cross-agent checks (for example, a requirement placed in a release that no phase delivers in time). Each finding is routed back as feedback to the agent that owns the artifact. Only that agent and the agents downstream of it re-run, for up to `--max-rounds` (default 2) rework rounds. Claude never decides the order; Python does.

Every pass is saved as an immutable version in `backend/data/runs/<run_id>/v<N>/`: each agent's JSON and Markdown, a `traceability.md` matrix (requirement → acceptance criteria → decisions → phases → releases, plus NFR targets → decisions) and a `manifest.json` with SHA-256 hashes, the findings, and the feedback that was sent. The runs folder is git-ignored because it can hold proposal content.

`approve` is the human gate. It re-hashes the version from disk and refuses if any file changed. You review the files, then type the first 8 characters of the **bundle hash** (or pass `--confirm`). Versions with open findings also need `--accept-open-issues`. Approval writes `approval.json` for that exact version, so a newer version is never approved by inheritance. `status` lists versions and flags unapproved or modified ones.

## Troubleshooting

| Symptom | What to check |
|---|---|
| Word or JSON file missing | Check the filename and location. Run the ingestion scripts from `backend`. The embedding script expects `backend/data/output/brd_chunks.json`. |
| Chunk exceeds 256 tokens | Check the reported chunk ID. Split at a paragraph or table-row boundary, preserve the heading, and repeat the table header where needed. |
| `DatabaseNotFound` | Compare `NEO4J_DATABASE` with the Aura Query database selector. Check for a stale shell environment variable overriding `.env`. |
| `IndexNotFound` | Create `nexus_brd_embeddings` in the database used by Python. Once it is online, retry search using the vectors already stored there. |
| Hugging Face unauthenticated warning | If the weights load and an embedding is printed, the model worked. If the download fails, check the machine's access to Hugging Face. |

Reference documentation: [Connect to Aura](https://neo4j.com/docs/aura/getting-started/connect-instance/) · [Neo4j vector indexes](https://neo4j.com/docs/cypher-manual/current/indexes/search-indexes/vector-indexes/) · [MiniLM model card](https://huggingface.co/sentence-transformers/all-MiniLM-L6-v2)

## TODO

- [x] Complete RAG with an `ask` command. Extract reusable retrieval from `search`, send the question and retrieved passages to Claude, and verify citations against chunk IDs. (Live Claude call untested until an API key is added.)
- [x] Ingest the NFR alongside the BRD and preserve each source's identity. Automate token-aware splitting, including repeated headers for long tables.
- [x] Build the **Functional Analyst**. Process all relevant BRD sections, track mandatory requirement IDs, and produce the functional document with cited rules, workflows, acceptance criteria, and open questions.
- [x] Build the **Solution Architect**. Use the functional draft and NFRs to produce structured design decisions, a Mermaid architecture diagram, and a technology stack with rationale and assumptions.
- [x] Build the **Project Manager**. Review the proposal (objectives, scope, assumptions, constraints, dependencies, timelines), determine skills, team size, availability and onboarding, and prepare phases, milestones, releases, and a delivery roadmap. (Replaces the Proposal Reviewer.)
- [x] Orchestrate the three roles (Functional Analyst, Solution Architect, Project Manager) in Python. Persist draft versions, route each agent's verification problems for rework, and require human approval of the exact version containing all three deliverables.
- [ ] Add GraphRAG for amendment impact analysis and expose an existing tool through MCP as separate extensions.

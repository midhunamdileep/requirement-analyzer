# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

requirement-analyzer (codename NEXUS) is planned as a multi-agent proposal platform (Functional Analyst, Solution Architect, Project Manager) that turns a BRD + NFR into a functional document, architecture diagram, and tech-stack recommendation. **Only the first milestone exists**: ingesting a Word BRD, chunking it, embedding the chunks locally, and retrieving them from Neo4j Aura via vector search. The agents and orchestration are TODO items in `README.md`, which also holds the full step-by-step setup (Aura, vector index Cypher, troubleshooting).

There are no tests, linter, or build system.

## Commands

Setup (the dependency file is named `requirement.txt`, singular):

```bash
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r backend/requirement.txt   # Windows; use .venv/bin/python on macOS/Linux
```

Ingestion — `read_brd.py` uses a CWD-relative path, so run from `backend/`:

```bash
cd backend
python app/ingestion/read_brd.py     # print paragraphs/styles/tables in order
python app/ingestion/chunk_brd.py    # writes backend/data/output/brd_chunks.json
```

Embedding/retrieval — run from the repo root:

```bash
python backend/app/embeddings/brd_vectors.py preview             # local only, no Neo4j needed
python backend/app/embeddings/brd_vectors.py upload              # embed all chunks, MERGE into Neo4j
python backend/app/embeddings/brd_vectors.py search "question"   # top-10 vector search
python backend/app/embeddings/brd_vectors.py ask "question" [--dry-run]   # RAG answer with citation check; --dry-run needs no API key
```

UI (Streamlit; upload BRD/NFR, run orchestrator with inputs, review, approve, ask) — run from the repo root:

```bash
streamlit run backend/app/ui.py
```

`ask` also needs `ANTHROPIC_API_KEY` (optional `ANTHROPIC_MODEL`) in `backend/.env`. Chunk the NFR with `python app/ingestion/chunk_brd.py nfr` (from `backend/`); `brd` is the default and would overwrite the hand-edited BRD JSON.

`upload`/`search` read `backend/.env` (gitignored) for `NEO4J_URI`, `NEO4J_USERNAME`, `NEO4J_PASSWORD`, `NEO4J_DATABASE`. The first run downloads `sentence-transformers/all-MiniLM-L6-v2` from Hugging Face; it runs on CPU.

## Architecture

Pipeline: `.docx` → `chunk_brd.py` → `backend/data/output/brd_chunks.json` → `brd_vectors.py` → Neo4j.

- **Chunking** (`app/ingestion/chunk_brd.py`): one chunk per `Heading *` style section; tables are flattened to `cell | cell | cell` rows. Records are `{chunk_id, source, heading, text}` with IDs `<file stem>-NNN`.
- **Retrieval** (`app/rag/retrieval.py`, `app/rag/ask.py`): reusable `load_model`, `embed`, `connect`, `search_chunks` (agents should import these, not shell out to the script) and `ask`, which prompts Claude with retrieved chunks and `verify_citations` against retrieved chunk IDs. `brd_vectors.py` adds `backend/` to `sys.path` so `app.*` imports work when run by path. Both BRD and NFR chunk files are embedded and stored.
- **Functional Analyst** (`app/agents/functional_analyst.py`, run as `python backend/app/agents/functional_analyst.py [--dry-run]`): `fetch_chunks` loads the whole BRD from Neo4j; `parse_requirements` builds the BR-ID/priority checklist in code; Claude returns a Pydantic-validated `FunctionalDocument` via a tool call; `verify` checks citations, requirement coverage and `Must` acceptance criteria, with up to two repair rounds. Outputs `functional_document.md`/`.json` in `data/output/`. The configured model rejects forced `tool_choice`, so it uses `auto`.
- **Agent helpers** (`app/agents/common.py`): `call_tool` (Pydantic model as tool schema, `tool_choice: auto`), `generate_with_repair` (draft → code verification → up to two repair rounds), `get_client`, `cite`. New agents should reuse these.
- **Solution Architect** (`app/agents/solution_architect.py`, `python backend/app/agents/solution_architect.py [--dry-run]`): reads `functional_document.json` (run the Functional Analyst first) plus all BRD/NFR chunks; code parses `NFR-*` targets and the NFR priority table; Claude returns an `ArchitectureDesign`; the Mermaid diagram is rendered in code from components/flows; `verify` checks citations, Must BR/NFR coverage and flow consistency. Outputs `architecture.md`/`.json`.
- **Project Manager** (`app/agents/project_manager.py`, `python backend/app/agents/project_manager.py [--dry-run] [--start-date YYYY-MM-DD]`): needs `functional_document.json` and `architecture.json`. Claude returns a `ProjectPlan` (review, resourcing, roadmap); code computes the schedule (`compute_schedule`: week offsets, critical path, cycle detection), staffing and the Mermaid Gantt. The BRD/NFR contain no dates, budget or team, so durations/FTE are estimates unless the optional `backend/data/inputs/planning_inputs.json` (template: `planning_inputs.example.json`) supplies facts. Outputs `project_plan.md`/`.json`. The orchestrator TODO now covers these three agents (the Proposal Reviewer was dropped).
- **Orchestrator** (`app/orchestrator.py`: `run [--dry-run] [--max-rounds N] [--start-date]`, `status`, `approve <run_id> [--version N] [--confirm hash8] [--accept-open-issues]`): a Python workflow, not an LLM, runs `fa.generate` → `sa.generate` → `pm.generate` (each accepts `feedback`, appended via `with_feedback` in `common.py`). `collect_findings` = unresolved agent problems + `cross_check`; findings route to the owning agent, which re-runs along with its downstream agents. Each pass is an immutable `backend/data/runs/<run_id>/v<N>/` (git-ignored) with JSON/MD, `traceability.md` and `manifest.json` (per-file SHA-256 and `bundle_hash`). `approve` re-hashes from disk, needs the bundle hash typed back, and writes `approval.json` for that exact version only. Don't hand-edit files in a run folder; a changed file makes `approve` refuse.
- **Event log and handoff**: `common.log_event` records each LLM call, token count, verification result and repair round (contextvars; prints when nothing is collecting). The orchestrator wraps every agent call, streams those lines to the UI and writes `handoff.json` into each version (hashed into the manifest): who passed which artifact (with SHA-256 and counts) to whom, feedback routed back, and per-agent run stats/events. Loops: schema retry in `call_tool`, draft→verify→repair (`MAX_REPAIRS`) in `generate_with_repair`, and orchestrator rework rounds. Evals are code verifiers plus `cross_check`; there is no LLM judge.
- **UI** (`app/ui.py`): Streamlit front end over the orchestrator (`start_run`, `write_approval`, `verify_version`) and `rag.ask.answer_question`; runs execute in a background thread with a live log. Uploads are saved to `backend/data/documents/`, chunked via `chunk_brd.ingest_file` (writes `<stem>_chunks.json`), embedded with `brd_vectors.upload_chunks`. The active BRD/NFR pair comes from `app/sources.py` (`backend/data/active_documents.json`, defaults to the XATA_TODO samples); agents and the orchestrator read chunks of the active sources, not hardcoded names.
- **Embedding** (`app/embeddings/brd_vectors.py`): embeds `"{heading}\n\n{text}"` with MiniLM (384-dim, cosine). `embed()` aborts if input exceeds the model's 256-token limit.
- **Storage**: `NexusChunk` nodes, `MERGE`d on `(source, chunk_id)`. The vector index `nexus_brd_embeddings` is created manually in Aura (Cypher in README) — no code creates it, and the search query depends on that exact name. Search uses a `CYPHER 25` `SEARCH ... IN (VECTOR INDEX ...)` clause.
- The committed `brd_chunks.json` is the reviewed artifact the embedding script reads; the `.docx` is only needed for (re)ingestion. Sample docs live in `backend/data/documents/` (copies in `docs/`).

## Gotchas

- `chunk_brd.py` splits sections over the model's 256-token limit automatically (balanced parts at paragraph/table-row boundaries, table header repeated, `-A`/`-B` suffix on the section's ID; section 6 of the BRD becomes `-008-A`/`-008-B`). It loads the local tokenizer from `backend/models`. Changing chunk boundaries means re-running `upload`.
- `upload` doesn't delete: renamed/removed chunk IDs leave stale nodes. Use a fresh database when comparing chunking strategies.
- Hardcoded assumptions: `read_brd.py` points at `XATA_TODO_BRD.docx`; `preview` embeds chunk index `[8]`.
- Model is loaded from `backend/models/all-MiniLM-L6-v2` (local, no Hugging Face access). The Aura URI in `.env` uses `neo4j+ssc://` because this machine doesn't trust Aura's SSL.com root; use `neo4j+s://` with a trusted CA for real data.
- Aura is cloud-hosted — only upload sample/approved material.
- `anthropic`, `pydantic`, and `pymupdf` are in the requirements but not yet used.

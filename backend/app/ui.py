"""Streamlit UI for the NEXUS orchestrator: upload documents, run the agents, review, approve, ask.

Run from the repo root:  streamlit run backend/app/ui.py
"""
import contextlib
import io
import json
import os
import re
import sys
import threading
import traceback
from datetime import date, datetime
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR))  # lets `app.*` imports work when run by path

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components
from dotenv import load_dotenv

from app import orchestrator as orch
from app.agents import project_manager as pm
from app.sources import DOCUMENTS_DIR, get_active, set_active

load_dotenv(BACKEND_DIR / ".env")

MAX_UPLOAD_MB = 10
MERMAID_FENCE = re.compile(r"```mermaid\n(.*?)```", re.S)
VERSION_FILES = {
    "Functional document": "functional_document.md",
    "Architecture": "architecture.md",
    "Project plan": "project_plan.md",
    "Traceability": "traceability.md",
}

st.set_page_config(page_title="NEXUS", page_icon="🧭", layout="wide")


# ---------------------------------------------------------------- helpers


@st.cache_resource
def embedding_model():
    from app.rag.retrieval import load_model
    return load_model()


@st.cache_resource
def job() -> dict:
    """Shared state of the (single) background run; a thread cannot touch st.session_state."""
    return {"thread": None, "log": [], "error": None, "manifests": None, "started": None}


def render_markdown(text: str) -> None:
    """st.markdown cannot draw Mermaid, so diagram fences are rendered through mermaid.js."""
    position = 0
    for match in MERMAID_FENCE.finditer(text):
        st.markdown(text[position:match.start()])
        diagram = match.group(1)
        # Diagram text comes from model output, so it goes in as a JSON string (never as markup) and is drawn
        # with mermaid.render(). HTML-escaping it instead turns "->>" into "-&gt;&gt;", which mermaid rejects.
        data = json.dumps(diagram).replace("<", "\\u003c")
        page = (
            '<div id="out" style="min-height:40px"></div>'
            '<script type="module">import mermaid from "https://cdn.jsdelivr.net/npm/mermaid@10.9.1/dist/mermaid.esm.min.mjs";'
            "mermaid.initialize({startOnLoad:false, securityLevel:'strict'});"
            "const out=document.getElementById('out');"
            # Streamlit renders every tab, so the frame may be hidden (zero size) until its tab is opened;
            # mermaid cannot measure text then. Wait until the frame is visible before drawing.
            "let n=0,busy=false;"
            "const tick=async()=>{"
            "if(busy||out.querySelector('svg')||out.getBoundingClientRect().width<=0)return;"
            "busy=true;"
            f"try{{const r=await mermaid.render('g'+(n++),{data});out.innerHTML=r.svg;}}"
            "catch(e){out.textContent=n>=5?'Diagram could not be drawn: '+e.message:'';}"
            "busy=false;};"
            "setInterval(tick,500);</script>"
        )
        height = max(300, 24 * diagram.count("\n") + 120)
        if hasattr(st, "iframe"):
            st.iframe(page, height=height)
        else:
            components.html(page, height=height, scrolling=True)
        position = match.end()
    st.markdown(text[position:])


def show_handoffs(path: Path) -> None:
    """Sequence diagram, table and per-agent stats from a version's handoff.json."""
    if not path.is_file():
        st.info("Handoffs were not recorded for this run (it predates handoff.json).")
        return
    data = json.loads(path.read_text(encoding="utf-8"))
    handoffs = data["handoffs"]

    def clean(text: str) -> str:
        return re.sub(r"[;#:()]", " ", str(text)).strip()

    names = list(dict.fromkeys(n for h in handoffs for n in (h["from"], h["to"])))
    lines = ["sequenceDiagram"] + [f"    participant P{i} as {clean(n)}" for i, n in enumerate(names)]
    for h in handoffs:
        if h["kind"] == "feedback":
            label = f"feedback, {len(h['items'])} item(s)"
        elif h["kind"] == "source":
            label = ", ".join(f"{v} {k}" for k, v in h["payload"].items())
        else:
            label = f"{h['artifact']} {h['sha256'][:8]}" + ("" if h["status"] == "ran" else " reused")
        lines.append(f"    P{names.index(h['from'])}->>P{names.index(h['to'])}: {clean(label)}")
    render_markdown("```mermaid\n" + "\n".join(lines) + "\n```\n")

    st.subheader("Handoffs")
    rows = []
    for h in handoffs:
        detail = h.get("summary") or h.get("payload") or {"feedback": "; ".join(h.get("items", []))[:200]}
        rows.append({"#": h["seq"], "from": h["from"], "to": h["to"], "what": h.get("artifact") or h["kind"],
                     "hash": h.get("sha256", "")[:8], "this pass": h.get("status", ""),
                     "content": ", ".join(f"{k}: {v}" for k, v in detail.items())})
    st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")

    st.subheader("Agent runs")
    if data["agent_runs"]:
        st.dataframe(pd.DataFrame([{k: v for k, v in r.items() if k not in ("events", "feedback_received", "problems_remaining")}
                                   | {"problems_remaining": len(r["problems_remaining"])} for r in data["agent_runs"]]),
                     hide_index=True, width="stretch")
        for r in data["agent_runs"]:
            with st.expander(f"{r['agent']}: event log"):
                st.json(r["events"])
    else:
        st.caption("No agent ran in this pass.")


def available_documents() -> list[str]:
    return sorted(p.name for p in DOCUMENTS_DIR.glob("*.docx") if not p.name.startswith("~$"))


def save_upload(upload) -> Path:
    name = Path(upload.name).name
    if not name.lower().endswith(".docx"):
        raise ValueError("Only .docx files are supported.")
    if upload.size > MAX_UPLOAD_MB * 1024 * 1024:
        raise ValueError(f"{name} is larger than {MAX_UPLOAD_MB} MB.")
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", name)
    DOCUMENTS_DIR.mkdir(parents=True, exist_ok=True)
    path = DOCUMENTS_DIR / name
    path.write_bytes(upload.getvalue())
    return path


def ingest(path: Path, id_pattern: str) -> tuple[list[dict], str]:
    """Chunk a document, embed it and store it in Neo4j. Returns (chunks, captured log)."""
    from app.embeddings.brd_vectors import upload_chunks
    from app.ingestion.chunk_brd import ingest_file

    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        _, chunks = ingest_file(path)
        upload_chunks(embedding_model(), chunks)
    if not any(re.search(id_pattern, c["text"]) for c in chunks):
        buffer.write(f"\nWARNING: no {id_pattern} identifiers found; the agents expect the sample document structure.")
    return chunks, buffer.getvalue()


def env_status() -> dict[str, bool]:
    neo = all(os.getenv(k) for k in ("NEO4J_URI", "NEO4J_USERNAME", "NEO4J_PASSWORD"))
    return {"Neo4j credentials": neo, "ANTHROPIC_API_KEY": bool(os.getenv("ANTHROPIC_API_KEY"))}


# ---------------------------------------------------------------- sidebar

with st.sidebar:
    st.title("🧭 NEXUS")
    active = get_active()
    st.caption("Active documents")
    st.write(f"**BRD:** {active['brd']}")
    st.write(f"**NFR:** {active['nfr']}")
    st.divider()
    for label, ok in env_status().items():
        st.write(f"{'✅' if ok else '❌'} {label}")
    st.info("Documents are embedded and stored in Neo4j Aura (cloud). Upload only sample or approved material.")

tab_docs, tab_run, tab_review, tab_approve, tab_ask = st.tabs(
    ["📄 Documents", "▶️ Run", "🔍 Review", "✅ Approve", "💬 Ask"]
)

# ---------------------------------------------------------------- documents

with tab_docs:
    st.subheader("Upload BRD and NFR")
    st.caption("Word (.docx) files with Heading styles and BR-/NFR- requirement IDs, like the sample documents.")
    col_brd, col_nfr = st.columns(2)
    for column, kind, pattern in ((col_brd, "BRD", r"BR-\d+"), (col_nfr, "NFR", r"NFR-[A-Z]+-\d+")):
        with column:
            upload = st.file_uploader(f"{kind} document", type=["docx"], key=f"upload_{kind}")
            if upload and st.button(f"Save and ingest {kind}", key=f"ingest_{kind}"):
                try:
                    with st.spinner("Chunking, embedding and uploading to Neo4j..."):
                        path = save_upload(upload)
                        chunks, log = ingest(path, pattern)
                    st.session_state[f"ingested_{kind}"] = path.name
                    st.success(f"Stored {len(chunks)} chunks from {path.name}.")
                    st.dataframe(pd.DataFrame(chunks)[["chunk_id", "heading", "text"]], hide_index=True)
                    with st.expander("Log"):
                        st.code(log)
                except (ValueError, SystemExit, Exception) as error:  # SystemExit: oversize paragraph, missing env
                    st.error(f"Ingestion failed: {error}")

    st.subheader("Choose the active documents")
    docs = available_documents()
    if docs:
        a, b = st.columns(2)
        brd_choice = a.selectbox("BRD", docs, index=docs.index(active["brd"]) if active["brd"] in docs else 0)
        nfr_choice = b.selectbox("NFR", docs, index=docs.index(active["nfr"]) if active["nfr"] in docs else 0)
        if st.button("Use these documents"):
            set_active(brd_choice, nfr_choice)
            st.rerun()
        st.caption("A document must be ingested (stored in Neo4j) before it can be used. The samples already are.")
    else:
        st.warning("No documents found.")

# ---------------------------------------------------------------- run

with tab_run:
    state = job()
    running = state["thread"] is not None and state["thread"].is_alive()

    st.subheader("Run the agents")
    st.caption("Functional Analyst → Solution Architect → Project Manager, with findings routed back for rework.")
    with st.form("run_form"):
        c1, c2, c3 = st.columns(3)
        use_start = c1.checkbox("Set a project start date", value=False)
        monday = pm.next_monday()
        start_date = c1.date_input("Start date", value=monday, min_value=date(2020, 1, 1),
                                   max_value=date(monday.year + 10, 12, 31))
        max_rounds = c2.number_input("Max rework rounds", 0, 5, 2)
        dry_run = c3.checkbox("Dry run (no Claude calls, nothing written)", value=False)

        with st.expander("Planning inputs (optional facts for the Project Manager)"):
            use_inputs = st.checkbox("Provide planning inputs (otherwise planning_inputs.json is used if present)")
            go_live = st.text_input("Target go-live (YYYY-MM-DD, optional)")
            people = st.data_editor(
                pd.DataFrame({"role": pd.Series(dtype="str"), "skills": pd.Series(dtype="str"),
                              "availability_percent": pd.Series(dtype="int")}),
                num_rows="dynamic", width='stretch', key="people",
                column_config={"skills": st.column_config.TextColumn(help="Comma-separated")},
            )
            constraints = st.text_area("Constraints (one per line)")

        with st.expander("Guidance to the agents (optional)"):
            guidance = {
                "fa": st.text_area("Functional Analyst", placeholder="e.g. Treat BR-004 as out of scope for release 1."),
                "sa": st.text_area("Solution Architect", placeholder="e.g. Prefer managed AWS services."),
                "pm": st.text_area("Project Manager", placeholder="e.g. Plan for two-week sprints."),
            }
        submitted = st.form_submit_button("Start run", disabled=running, type="primary")

    if submitted and not running:
        inputs = None
        if use_inputs:
            rows = [r for r in people.to_dict("records") if str(r.get("role") or "").strip()]
            inputs = {
                "start_date": start_date.isoformat() if use_start else None,
                "target_go_live": go_live.strip() or None,
                "available_people": [
                    {"role": r["role"], "skills": [s.strip() for s in str(r.get("skills") or "").split(",") if s.strip()],
                     "availability_percent": int(r["availability_percent"]) if pd.notna(r.get("availability_percent")) else 100}
                    for r in rows
                ],
                "constraints": [c.strip() for c in constraints.splitlines() if c.strip()],
            }
        feedback = {k: [f"User guidance: {v.strip()}"] for k, v in guidance.items() if v.strip()}
        options = dict(max_rounds=int(max_rounds), dry_run=dry_run,
                       start_date=start_date.isoformat() if use_start else None,
                       inputs=inputs, initial_feedback=feedback)

        state.update(log=[], error=None, manifests=None, started=datetime.now(), shown=False)

        def work() -> None:
            try:
                state["manifests"] = orch.start_run(progress=state["log"].append, **options)
            except BaseException as error:  # SystemExit from missing config must not die silently
                state["error"] = f"{type(error).__name__}: {error}"
                state["log"].append(traceback.format_exc())

        state["thread"] = threading.Thread(target=work, daemon=True)
        state["thread"].start()
        st.rerun()

    @st.fragment(run_every=2 if running else None)  # poll only while a run is active
    def progress_panel() -> None:
        def agent_blocks(lines: list[str]) -> None:
            """Group the flat log into one expandable block per agent run."""
            blocks: list[dict] = []
            for line in list(lines):
                starting = re.match(r"\[v(\d+)\] (.+?) starting(.*)", line)
                done = re.match(r"\[v(\d+)\] (.+?) done in (.*)", line)
                if starting:
                    blocks.append({"title": f"v{starting.group(1)} · {starting.group(2)}", "agent": starting.group(2),
                                   "lines": [line], "done": False})
                elif blocks and (line.startswith("    [") or (done and done.group(2) == blocks[-1]["agent"])):
                    blocks[-1]["lines"].append(line)
                    blocks[-1]["done"] = blocks[-1]["done"] or bool(done)
                else:
                    blocks.append({"title": None, "lines": [line]})
            for block in blocks:
                if block["title"] is None:
                    st.text("\n".join(block["lines"]))
                    continue
                state = "complete" if block["done"] else "running"
                with st.status(block["title"] + ("" if block["done"] else " — working…"), state=state,
                               expanded=not block["done"]):
                    st.code("\n".join(block["lines"]), language=None)

        s = job()
        alive = s["thread"] is not None and s["thread"].is_alive()
        if s["started"] is None:
            st.info("No run started in this session yet.")
            return
        if alive:
            st.warning("Run in progress… (this can take several minutes)")
        elif s["error"]:
            st.error(s["error"])
        else:
            last = (s["manifests"] or [None])[-1]
            if last and not s.get("shown"):
                # Point Review/Approve at the new run (widget state would otherwise keep the old selection)
                # and redraw the whole page, since only this fragment reruns on its own.
                s["shown"] = True
                for key in ("run_review", "run_approve", "ver_review", "ver_approve"):
                    st.session_state.pop(key, None)
                st.session_state["run_review"] = st.session_state["run_approve"] = last["run_id"]
                st.rerun()
            if last:
                st.success(f"Finished. Latest version v{last['version']}, {len(last['findings'])} open finding(s). "
                           "Review it in the Review and Approve tabs.")
            else:
                st.success("Dry run finished.")
        agent_blocks(s["log"])
        if not s["log"]:
            st.caption("(no output yet)")
        with st.expander("Raw log"):
            st.code("\n".join(s["log"]) or "(no output yet)")

    progress_panel()

# ---------------------------------------------------------------- review


def run_and_version(key: str):
    runs = [p.name for p in reversed(orch.run_dirs())]
    if not runs:
        st.info("No runs yet. Start one in the Run tab.")
        return None, None
    run_id = st.selectbox("Run", runs, key=f"run_{key}")
    versions = [int(p.name[1:]) for p in orch.versions_of(orch.RUNS_DIR / run_id)]
    if not versions:
        st.warning("This run has no versions.")
        return None, None
    version = st.selectbox("Version", versions, index=len(versions) - 1, key=f"ver_{key}")
    return run_id, version


with tab_review:
    run_id, version = run_and_version("review")
    if run_id:
        version_dir = orch.RUNS_DIR / run_id / f"v{version}"
        manifest, problems = orch.verify_version(version_dir)
        approval = orch.read_approval(orch.RUNS_DIR / run_id)
        approved = bool(approval and approval["version"] == manifest["version"]
                        and approval["bundle_hash"] == manifest["bundle_hash"])
        m1, m2, m3 = st.columns(3)
        m1.metric("Open findings", len(manifest["findings"]))
        m2.metric("Bundle hash", manifest["bundle_hash"][:8])
        m3.metric("Status", "Approved" if approved else "Not approved")
        if problems:
            st.error("INTEGRITY PROBLEM: " + "; ".join(problems))
        st.caption(f"Created {manifest['created_at']} · model {manifest['model']} · "
                   f"agents run: {', '.join(manifest['agents_run'])}")

        if manifest["findings"]:
            st.subheader("Open findings")
            st.dataframe(pd.DataFrame(manifest["findings"])[["owner", "requirement", "problem", "correction"]],
                         hide_index=True, width='stretch')
        names = list(VERSION_FILES) + ["Handoffs"]
        for tab, name in zip(st.tabs(names), names):
            with tab:
                if name == "Handoffs":
                    show_handoffs(version_dir / "handoff.json")
                    continue
                path = version_dir / VERSION_FILES[name]
                if path.is_file():
                    render_markdown(path.read_text(encoding="utf-8"))
                else:
                    st.write("Not available.")

# ---------------------------------------------------------------- approve

with tab_approve:
    st.caption("Approval applies to one exact version (identified by its bundle hash). Review it first, then approve.")
    run_id, version = run_and_version("approve")
    if run_id:
        try:
            _, version_dir, versions = orch.locate_version(run_id, version)
            manifest, problems = orch.verify_version(version_dir)
        except ValueError as error:
            st.error(str(error))
            st.stop()
        expected = manifest["bundle_hash"][:8]
        st.write(f"Bundle hash: `{manifest['bundle_hash']}`")
        st.write("Deliverables: " + ", ".join(sorted(manifest["files"])))
        if manifest["version"] != int(versions[-1].name[1:]):
            st.warning(f"{versions[-1].name} is a newer version of this run.")
        for f in manifest["findings"]:
            st.write(f"- **{orch.AGENT_NAMES[f['owner']]}**: {f['problem']}")
        if problems:
            st.error("Cannot approve: " + "; ".join(problems))

        existing = orch.read_approval(orch.RUNS_DIR / run_id)
        if existing:
            st.info(f"Approved: v{existing['version']} ({existing['bundle_hash'][:8]}) by "
                    f"{existing['approved_by']} at {existing['approved_at']}.")

        accept = st.checkbox("Accept the open findings", disabled=not manifest["findings"])
        if st.button("Approve this version", type="primary", disabled=bool(problems)):
            try:
                # The hash is checked against the files on disk inside write_approval, so a changed file still blocks it.
                result = orch.write_approval(run_id, manifest["version"], expected, accept)
                st.success(f"Approved v{result['version']} ({result['bundle_hash'][:8]}) as {result['approved_by']}.")
            except ValueError as error:
                st.error(str(error))

# ---------------------------------------------------------------- ask

with tab_ask:
    st.caption("Ask a question about the active BRD and NFR. Answers cite chunk IDs, which are checked against what was retrieved.")
    if "chat" not in st.session_state:
        st.session_state.chat = []
    dry = st.toggle("Dry run (show retrieved passages only, no Claude call)")
    for entry in st.session_state.chat:
        with st.chat_message("user"):
            st.write(entry["question"])
        with st.chat_message("assistant"):
            st.write(entry["answer"])
            if entry["invalid"]:
                st.warning("Cited chunks that were not retrieved: " + ", ".join(entry["invalid"]))
            with st.expander("Retrieved passages"):
                for c in entry["chunks"]:
                    st.markdown(f"**{c['chunk_id']}** · {c['heading']} · score {c['score']:.3f}")
                    st.text(c["text"])

    question = st.chat_input("Ask about the requirements…")
    if question:
        from app.rag.ask import answer_question
        try:
            with st.spinner("Searching and answering…"):
                active = get_active()
                result = answer_question(question, model=embedding_model(),
                                         sources=[active["brd"], active["nfr"]], dry_run=dry)
            text = result["answer"] or ("(dry run: no answer requested)" if result["chunks"] else "No indexed chunks found.")
            st.session_state.chat.append({**result, "question": question, "answer": text})
        except (SystemExit, Exception) as error:
            st.session_state.chat.append({"question": question, "answer": f"Error: {error}", "chunks": [], "invalid": []})
        st.rerun()

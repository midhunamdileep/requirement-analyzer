import argparse
import re
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BACKEND_DIR))  # lets `app.*` imports work when run by path

from pydantic import BaseModel, Field, ValidationError

from app.agents.common import cite, generate_with_repair, get_client, with_feedback
from app.agents.functional_analyst import FunctionalDocument, parse_requirements
from app.rag.retrieval import connect, fetch_chunks


BRD_SOURCE = "XATA_TODO_BRD.docx"
NFR_SOURCE = "XATA_TODO_NFR.docx"
OUTPUT_DIR = BACKEND_DIR / "data" / "output"
TOOL_NAME = "submit_architecture_design"
NFR_ROW = re.compile(r"^(NFR-[A-Z]+-\d+) \| (.+) \| (.+)$", re.MULTILINE)
CATEGORY_ROW = re.compile(r"^(.+?) \| (Must|Should|Could)\s*$", re.MULTILINE)
SAFE_ID = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")


class Decision(BaseModel):
    id: str = Field(description="Decision ID such as AD-01")
    title: str
    decision: str
    rationale: str
    alternatives_considered: list[str]
    requirement_ids: list[str] = Field(description="BR-xxx and NFR-xxx IDs this decision satisfies")
    citations: list[str] = Field(description="BRD/NFR chunk IDs supporting this decision")


class TechChoice(BaseModel):
    layer: str
    technology: str
    rationale: str
    assumptions: list[str]
    decision_ids: list[str] = Field(description="Decisions that justify this choice")


class Component(BaseModel):
    id: str = Field(description="Short identifier of letters, digits and underscores, starting with a letter")
    name: str
    layer: str = Field(description="Architecture layer used to group the component in the diagram")
    responsibility: str


class Flow(BaseModel):
    from_id: str
    to_id: str
    label: str


class NfrCoverage(BaseModel):
    category: str = Field(description="NFR category name exactly as listed in the checklist")
    approach: str
    decision_ids: list[str]
    citations: list[str]


class ArchitectureDesign(BaseModel):
    decisions: list[Decision]
    tech_stack: list[TechChoice]
    components: list[Component]
    flows: list[Flow]
    nfr_coverage: list[NfrCoverage]
    assumptions: list[str]
    risks: list[str]


SYSTEM_PROMPT = (
    "You are a solution architect. From the functional document and the non-functional "
    "requirements (NFR) provided, produce structured design decisions, a technology stack "
    "with rationale and assumptions, and the components and data flows of the architecture. "
    "Use only the facts provided and state every other fact as an assumption. Cite the "
    "chunk IDs (for example XATA_TODO_NFR-003) behind each decision. Every Must requirement "
    "and every numbered NFR target in the checklist must be referenced by at least one "
    "decision, and every Must NFR category must appear in nfr_coverage under its exact name. "
    "Every component must take part in at least one flow. Do not write diagram syntax; the "
    f"diagram is generated from components and flows. Submit the result by calling the {TOOL_NAME} tool."
)


def parse_nfr_targets(nfr_chunks: list[dict]) -> list[dict]:
    targets = []
    for chunk in nfr_chunks:
        for nfr_id, requirement, target in NFR_ROW.findall(chunk["text"]):
            targets.append({"id": nfr_id, "requirement": requirement, "target": target})
    return targets


def parse_categories(nfr_chunks: list[dict]) -> list[dict]:
    """Category priorities come from the NFR 'Priority' section (Category | Priority table)."""
    for chunk in nfr_chunks:
        if chunk["heading"].lower().rstrip(".").endswith("priority"):
            return [{"category": c.strip(), "priority": p} for c, p in CATEGORY_ROW.findall(chunk["text"])]
    return []


def build_prompt(doc: FunctionalDocument, chunks: list[dict], targets: list[dict], categories: list[dict]) -> str:
    sections = "\n\n".join(f"[{c['chunk_id']}] ({c['source']} | {c['heading']})\n{c['text']}" for c in chunks)
    requirements = "\n".join(f"- {r.id}: {r.description}" for r in doc.requirements)
    rules = "\n".join(f"- {r.text}" for r in doc.business_rules)
    workflows = "\n".join(f"- {w.name}: " + "; ".join(w.steps) for w in doc.workflows)
    questions = "\n".join(f"- {q.question}" for q in doc.open_questions)
    target_list = "\n".join(f"- {t['id']}: {t['requirement']} -> {t['target']}" for t in targets)
    category_list = "\n".join(f"- {c['category']} ({c['priority']})" for c in categories)
    return (
        f"Functional document\n\nRequirements:\n{requirements}\n\nBusiness rules:\n{rules}\n\n"
        f"Workflows:\n{workflows}\n\nOpen questions (treat unresolved ones as assumptions or risks):\n{questions}\n\n"
        f"Source sections (cite these chunk IDs):\n\n{sections}\n\n"
        f"Numbered NFR targets (each must be referenced by a decision):\n{target_list}\n\n"
        f"NFR categories (use these exact names in nfr_coverage; Must ones are mandatory):\n{category_list}"
    )


def normalize(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", name.lower()).strip()


def verify(
    design: ArchitectureDesign,
    doc: FunctionalDocument,
    chunks: list[dict],
    targets: list[dict],
    categories: list[dict],
    must_ids: list[str],
) -> list[str]:
    problems = []
    chunk_ids = {c["chunk_id"] for c in chunks}
    decision_ids = [d.id for d in design.decisions]
    known_requirements = {r.id for r in doc.requirements}
    known_nfrs = {t["id"] for t in targets}

    for dup in sorted({i for i in decision_ids if decision_ids.count(i) > 1}):
        problems.append(f"decision ID {dup} is used more than once")

    for d in design.decisions:
        if not d.citations:
            problems.append(f"decision {d.id} has no citations")
        for c in d.citations:
            if c not in chunk_ids:
                problems.append(f"decision {d.id} cites {c}, which is not a BRD/NFR chunk")
        for r in d.requirement_ids:
            if r not in known_requirements | known_nfrs:
                problems.append(f"decision {d.id} references {r}, which is not a known BR or NFR ID")

    referenced = {r for d in design.decisions for r in d.requirement_ids}
    for req_id in must_ids:
        if req_id not in referenced:
            problems.append(f"Must requirement {req_id} is not referenced by any decision")
    for target in sorted(known_nfrs - referenced):
        problems.append(f"NFR target {target} is not referenced by any decision")

    for tech in design.tech_stack:
        if not tech.decision_ids:
            problems.append(f"tech stack item '{tech.technology}' has no decision_ids")
        for ref in tech.decision_ids:
            if ref not in decision_ids:
                problems.append(f"tech stack item '{tech.technology}' references unknown decision {ref}")

    covered = {normalize(n.category) for n in design.nfr_coverage}
    for category in categories:
        if category["priority"] == "Must" and normalize(category["category"]) not in covered:
            problems.append(f"Must NFR category '{category['category']}' is missing from nfr_coverage")
    for n in design.nfr_coverage:
        if not n.decision_ids:
            problems.append(f"nfr_coverage '{n.category}' has no decision_ids")
        for ref in n.decision_ids:
            if ref not in decision_ids:
                problems.append(f"nfr_coverage '{n.category}' references unknown decision {ref}")
        for c in n.citations:
            if c not in chunk_ids:
                problems.append(f"nfr_coverage '{n.category}' cites {c}, which is not a BRD/NFR chunk")
        if not any("_NFR-" in c for c in n.citations):
            problems.append(f"nfr_coverage '{n.category}' cites no NFR chunk")

    component_ids = [c.id for c in design.components]
    for dup in sorted({i for i in component_ids if component_ids.count(i) > 1}):
        problems.append(f"component ID {dup} is used more than once")
    for c in design.components:
        if not SAFE_ID.match(c.id):
            problems.append(f"component ID '{c.id}' must use letters, digits and underscores and start with a letter")
    used = set()
    for flow in design.flows:
        for end in (flow.from_id, flow.to_id):
            used.add(end)
            if end not in component_ids:
                problems.append(f"flow '{flow.label}' uses unknown component {end}")
    for c in design.components:
        if c.id not in used:
            problems.append(f"component {c.id} takes part in no flow")

    return problems


def render_mermaid(design: ArchitectureDesign) -> str:
    def label(text: str) -> str:
        return text.replace('"', "'")

    layers: dict[str, list[Component]] = {}
    for component in design.components:
        layers.setdefault(component.layer, []).append(component)

    lines = ["flowchart LR"]
    for index, (layer, components) in enumerate(layers.items(), 1):
        lines.append(f'    subgraph layer_{index}["{label(layer)}"]')
        lines += [f'        {c.id}["{label(c.name)}"]' for c in components]
        lines.append("    end")
    for flow in design.flows:
        lines.append(f'    {flow.from_id} -->|"{label(flow.label)}"| {flow.to_id}')
    return "\n".join(lines)


def cell(text: str) -> str:
    return text.replace("|", "/").replace("\n", " ")


def render_markdown(design: ArchitectureDesign, problems: list[str]) -> str:
    lines = ["# Solution Architecture: XATA TODO Management Platform", "", "## Design decisions", ""]
    for d in design.decisions:
        lines += [
            f"### {d.id}: {d.title} {cite(d.citations)}",
            "",
            d.decision,
            "",
            f"- **Rationale:** {d.rationale}",
            f"- **Alternatives considered:** {'; '.join(d.alternatives_considered) or 'none stated'}",
            f"- **Satisfies:** {', '.join(d.requirement_ids)}",
            "",
        ]

    lines += ["## Technology stack", "", "| Layer | Technology | Rationale | Assumptions | Decisions |", "|---|---|---|---|---|"]
    for t in design.tech_stack:
        lines.append(
            f"| {cell(t.layer)} | {cell(t.technology)} | {cell(t.rationale)} | "
            f"{cell('; '.join(t.assumptions))} | {', '.join(t.decision_ids)} |"
        )

    lines += ["", "## Architecture diagram", "", "```mermaid", render_mermaid(design), "```", ""]
    lines += ["### Components", "", "| Component | Layer | Responsibility |", "|---|---|---|"]
    lines += [f"| {cell(c.name)} | {cell(c.layer)} | {cell(c.responsibility)} |" for c in design.components]

    lines += ["", "## NFR coverage", "", "| Category | Approach | Decisions | Sources |", "|---|---|---|---|"]
    for n in design.nfr_coverage:
        lines.append(f"| {cell(n.category)} | {cell(n.approach)} | {', '.join(n.decision_ids)} | {cite(n.citations)} |")

    lines += ["", "## Assumptions", ""] + [f"- {a}" for a in design.assumptions]
    lines += ["", "## Risks", ""] + [f"- {r}" for r in design.risks]

    if problems:
        lines += ["", "## Unresolved verification problems", ""] + [f"- {p}" for p in problems]

    return "\n".join(lines) + "\n"


def generate(client, doc: FunctionalDocument, chunks: list[dict], targets: list[dict], categories: list[dict], must_ids: list[str], feedback: list[str] | None = None):
    return generate_with_repair(
        client,
        SYSTEM_PROMPT,
        TOOL_NAME,
        "Submit the completed architecture design.",
        ArchitectureDesign,
        with_feedback(build_prompt(doc, chunks, targets, categories), feedback),
        lambda design: verify(design, doc, chunks, targets, categories, must_ids),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Produce architecture decisions, tech stack and diagram from the functional document and NFRs.")
    parser.add_argument("--dry-run", action="store_true", help="Show the checklists and prompt size without calling Claude.")
    args = parser.parse_args()

    functional_path = OUTPUT_DIR / "functional_document.json"
    if not functional_path.is_file():
        raise SystemExit(f"{functional_path} not found. Run functional_analyst.py first.")
    doc = FunctionalDocument.model_validate_json(functional_path.read_text(encoding="utf-8"))

    with connect() as driver:
        brd_chunks = fetch_chunks(driver, BRD_SOURCE)
        nfr_chunks = fetch_chunks(driver, NFR_SOURCE)
    if not brd_chunks or not nfr_chunks:
        raise SystemExit("BRD or NFR chunks missing from Neo4j. Run the upload first.")

    chunks = brd_chunks + nfr_chunks
    targets = parse_nfr_targets(nfr_chunks)
    categories = parse_categories(nfr_chunks)
    must_ids = [r["id"] for r in parse_requirements(brd_chunks) if r["priority"] == "Must"]
    must = [c["category"] for c in categories if c["priority"] == "Must"]
    print(f"Loaded {len(brd_chunks)} BRD and {len(nfr_chunks)} NFR sections; {len(doc.requirements)} functional requirements.")
    print(f"NFR targets ({len(targets)}): " + ", ".join(t["id"] for t in targets))
    print(f"Must categories ({len(must)}): " + ", ".join(must))
    print("Other categories: " + ", ".join(f"{c['category']} ({c['priority']})" for c in categories if c["priority"] != "Must"))

    if args.dry_run:
        print(f"Prompt size: {len(build_prompt(doc, chunks, targets, categories))} characters")
        return

    try:
        design, problems = generate(get_client(), doc, chunks, targets, categories, must_ids)
    except ValidationError as error:
        raise SystemExit(f"Claude returned a design that does not match the schema:\n{error}")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUTPUT_DIR / "architecture.json").write_text(design.model_dump_json(indent=2), encoding="utf-8")
    (OUTPUT_DIR / "architecture.md").write_text(render_markdown(design, problems), encoding="utf-8")
    print(f"Wrote architecture.md and .json to {OUTPUT_DIR}")
    if problems:
        print(f"WARNING: {len(problems)} verification problem(s) remain:")
        for problem in problems:
            print(f"- {problem}")
    else:
        print("Verification passed: citations valid, Must requirements and NFR targets referenced, Must categories covered.")


if __name__ == "__main__":
    main()

import argparse
import re
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BACKEND_DIR))  # lets `app.*` imports work when run by path

from pydantic import BaseModel, Field, ValidationError

from app.agents.common import cite, generate_with_repair, get_client, with_feedback
from app.rag.retrieval import connect, fetch_chunks


BRD_SOURCE = "XATA_TODO_BRD.docx"
OUTPUT_DIR = BACKEND_DIR / "data" / "output"
TOOL_NAME = "submit_functional_document"
REQUIREMENT_ROW = re.compile(r"^(BR-\d+) \| (.+) \| (Must|Should|Could|Won't)\s*$", re.MULTILINE)


class Requirement(BaseModel):
    id: str = Field(description="Requirement ID such as BR-001")
    description: str = Field(description="Functional description of what the system must do")
    citations: list[str] = Field(description="Chunk IDs supporting this item")


class BusinessRule(BaseModel):
    text: str
    citations: list[str]


class Workflow(BaseModel):
    name: str
    steps: list[str]
    citations: list[str]


class AcceptanceCriterion(BaseModel):
    requirement_ids: list[str] = Field(description="BR IDs this criterion verifies")
    given: str
    when: str
    then: str
    citations: list[str]


class OpenQuestion(BaseModel):
    question: str
    why_it_matters: str
    citations: list[str] = Field(description="Chunk IDs where the gap or ambiguity was found")


class Assumption(BaseModel):
    text: str
    citations: list[str]


class FunctionalDocument(BaseModel):
    requirements: list[Requirement]
    business_rules: list[BusinessRule]
    workflows: list[Workflow]
    acceptance_criteria: list[AcceptanceCriterion]
    open_questions: list[OpenQuestion]
    assumptions: list[Assumption]


SYSTEM_PROMPT = (
    "You are a functional analyst. You turn a business requirements document (BRD) into a "
    "functional document. Use only the BRD sections provided; never invent facts. Every item "
    "must cite the chunk IDs (for example XATA_TODO_BRD-012) of the sections it comes from. "
    "Cover every requirement in the checklist, and every Must requirement must also have at "
    "least one acceptance criterion written as Given/When/Then. Record genuine gaps, "
    "ambiguities and contradictions in the BRD as open questions rather than resolving them "
    f"yourself. Submit the result by calling the {TOOL_NAME} tool."
)


def parse_requirements(chunks: list[dict]) -> list[dict]:
    """Read the requirement table rows (ID | text | priority) out of the BRD chunks."""
    requirements = []
    for chunk in chunks:
        for req_id, text, priority in REQUIREMENT_ROW.findall(chunk["text"]):
            requirements.append({"id": req_id, "text": text, "priority": priority})
    return requirements


def build_prompt(chunks: list[dict], requirements: list[dict]) -> str:
    sections = "\n\n".join(
        f"[{c['chunk_id']}] {c['heading']}\n{c['text']}" for c in chunks
    )
    checklist = "\n".join(
        f"- {r['id']} ({r['priority']}): {r['text']}" for r in requirements
    )
    return (
        f"BRD sections:\n\n{sections}\n\n"
        f"Requirement checklist (every item must appear in requirements; "
        f"Must items also need acceptance criteria):\n{checklist}"
    )


def verify(doc: FunctionalDocument, chunks: list[dict], requirements: list[dict]) -> list[str]:
    """Deterministic checks on a draft; returns a list of problems (empty means clean)."""
    problems = []
    chunk_ids = {c["chunk_id"] for c in chunks}
    known = {r["id"] for r in requirements}
    must = [r["id"] for r in requirements if r["priority"] == "Must"]

    items = (
        [("requirement " + r.id, r.citations) for r in doc.requirements]
        + [(f"business rule '{r.text[:50]}'", r.citations) for r in doc.business_rules]
        + [(f"workflow '{w.name}'", w.citations) for w in doc.workflows]
        + [(f"acceptance criterion for {','.join(a.requirement_ids)}", a.citations) for a in doc.acceptance_criteria]
        + [(f"open question '{q.question[:50]}'", q.citations) for q in doc.open_questions]
        + [(f"assumption '{a.text[:50]}'", a.citations) for a in doc.assumptions]
    )
    for label, citations in items:
        if not citations:
            problems.append(f"{label} has no citations")
        for cited in citations:
            if cited not in chunk_ids:
                problems.append(f"{label} cites {cited}, which is not a BRD chunk")

    described = {r.id for r in doc.requirements}
    for req_id in sorted(known - described):
        problems.append(f"requirement {req_id} is missing from requirements")
    for req_id in sorted(described - known):
        problems.append(f"requirements lists {req_id}, which is not in the BRD checklist")

    tested = {req_id for a in doc.acceptance_criteria for req_id in a.requirement_ids}
    for req_id in must:
        if req_id not in tested:
            problems.append(f"Must requirement {req_id} has no acceptance criterion")
    for req_id in sorted(tested - known):
        problems.append(f"an acceptance criterion references {req_id}, which is not in the BRD checklist")

    return problems


def generate(client, chunks: list[dict], requirements: list[dict], feedback: list[str] | None = None) -> tuple[FunctionalDocument, list[str]]:
    return generate_with_repair(
        client,
        SYSTEM_PROMPT,
        TOOL_NAME,
        "Submit the completed functional document.",
        FunctionalDocument,
        with_feedback(build_prompt(chunks, requirements), feedback),
        lambda doc: verify(doc, chunks, requirements),
    )


def render_markdown(doc: FunctionalDocument, requirements: list[dict], problems: list[str]) -> str:
    covered = {r.id for r in doc.requirements}
    tested = {req_id for a in doc.acceptance_criteria for req_id in a.requirement_ids}
    lines = ["# Functional Document: XATA TODO Management Platform", ""]

    lines += ["## Mandatory requirement coverage", "", "| ID | Priority | Described | Acceptance criterion |", "|---|---|---|---|"]
    for r in requirements:
        lines.append(
            f"| {r['id']} | {r['priority']} | {'yes' if r['id'] in covered else 'NO'} | "
            f"{'yes' if r['id'] in tested else 'NO'} |"
        )
    lines.append("")

    lines += ["## Requirements", ""]
    for r in doc.requirements:
        lines.append(f"- **{r.id}**: {r.description} {cite(r.citations)}")

    lines += ["", "## Business rules", ""]
    for rule in doc.business_rules:
        lines.append(f"- {rule.text} {cite(rule.citations)}")

    lines += ["", "## Workflows", ""]
    for w in doc.workflows:
        lines += [f"### {w.name} {cite(w.citations)}", ""]
        lines += [f"{i}. {step}" for i, step in enumerate(w.steps, 1)]
        lines.append("")

    lines += ["## Acceptance criteria", ""]
    for a in doc.acceptance_criteria:
        lines += [
            f"- **{', '.join(a.requirement_ids)}** {cite(a.citations)}",
            f"  - Given {a.given}",
            f"  - When {a.when}",
            f"  - Then {a.then}",
        ]

    lines += ["", "## Open questions", ""]
    for q in doc.open_questions:
        lines.append(f"- {q.question} — *{q.why_it_matters}* {cite(q.citations)}")

    lines += ["", "## Assumptions", ""]
    for a in doc.assumptions:
        lines.append(f"- {a.text} {cite(a.citations)}")

    if problems:
        lines += ["", "## Unresolved verification problems", ""]
        lines += [f"- {p}" for p in problems]

    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description="Produce a cited functional document from the stored BRD.")
    parser.add_argument("--dry-run", action="store_true", help="Show the checklist and prompt size without calling Claude.")
    args = parser.parse_args()

    with connect() as driver:
        chunks = fetch_chunks(driver, BRD_SOURCE)
    if not chunks:
        raise SystemExit(f"No chunks found for {BRD_SOURCE}. Run the upload first.")

    requirements = parse_requirements(chunks)
    must = [r["id"] for r in requirements if r["priority"] == "Must"]
    print(f"Loaded {len(chunks)} BRD sections; {len(requirements)} requirements, {len(must)} Must.")
    print("Must: " + ", ".join(must))
    print("Other: " + ", ".join(f"{r['id']} ({r['priority']})" for r in requirements if r["priority"] != "Must"))

    if args.dry_run:
        print(f"Prompt size: {len(build_prompt(chunks, requirements))} characters")
        return

    try:
        doc, problems = generate(get_client(), chunks, requirements)
    except ValidationError as error:
        raise SystemExit(f"Claude returned a document that does not match the schema:\n{error}")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUTPUT_DIR / "functional_document.json").write_text(doc.model_dump_json(indent=2), encoding="utf-8")
    (OUTPUT_DIR / "functional_document.md").write_text(render_markdown(doc, requirements, problems), encoding="utf-8")
    print(f"Wrote functional_document.md and .json to {OUTPUT_DIR}")
    if problems:
        print(f"WARNING: {len(problems)} verification problem(s) remain:")
        for problem in problems:
            print(f"- {problem}")
    else:
        print("Verification passed: all citations valid, every Must requirement covered and tested.")


if __name__ == "__main__":
    main()

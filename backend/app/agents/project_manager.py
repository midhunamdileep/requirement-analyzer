import argparse
import json
import re
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Literal

BACKEND_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BACKEND_DIR))  # lets `app.*` imports work when run by path

from pydantic import BaseModel, Field, ValidationError

from app.agents.common import cite, generate_with_repair, get_client, with_feedback
from app.agents.functional_analyst import FunctionalDocument, parse_requirements
from app.agents.solution_architect import ArchitectureDesign
from app.rag.retrieval import connect, fetch_chunks


BRD_SOURCE = "XATA_TODO_BRD.docx"
NFR_SOURCE = "XATA_TODO_NFR.docx"
OUTPUT_DIR = BACKEND_DIR / "data" / "output"
INPUTS_FILE = BACKEND_DIR / "data" / "inputs" / "planning_inputs.json"
TOOL_NAME = "submit_project_plan"
SAFE_ID = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")


class Item(BaseModel):
    text: str
    citations: list[str] = Field(description="BRD/NFR chunk IDs the statement comes from")


class Dependency(BaseModel):
    id: str = Field(description="Dependency ID such as DEP-1")
    description: str
    owner: str
    citations: list[str]


class Review(BaseModel):
    objectives: list[Item]
    scope_in: list[Item]
    scope_out: list[Item]
    assumptions: list[Item]
    constraints: list[Item]
    stakeholders: list[Item]
    dependencies: list[Dependency]
    timeline_statements: list[Item] = Field(description="Only timeline facts the documents state; may be empty")
    information_gaps: list[str] = Field(description="Facts needed for planning that the documents do not give")


class Role(BaseModel):
    role: str
    skills: list[str]
    fte: float = Field(gt=0, description="Full-time-equivalent staffing while the role is active")
    phase_ids: list[str] = Field(description="Phases in which the role works")
    covers_tech_layers: list[str] = Field(description="Architecture tech-stack layers this role covers")
    rationale: str
    source: Literal["inputs", "assumed"] = Field(description="'inputs' only if the planning inputs name this person or role")


class Onboarding(BaseModel):
    item: str
    owner: str
    needed_before_phase: str = Field(description="Phase ID")
    dependency_ids: list[str] = Field(description="DEP IDs this item resolves; may be empty")


class Resourcing(BaseModel):
    roles: list[Role]
    onboarding: list[Onboarding]
    availability_assumptions: list[str]
    resource_risks: list[str]


class Phase(BaseModel):
    id: str = Field(description="Letters, digits and underscores, starting with a letter, such as p1_mobilize")
    name: str
    duration_weeks: int = Field(ge=1)
    depends_on: list[str] = Field(description="Phase IDs that must finish first")
    objectives: list[str]
    deliverables: list[str]
    requirement_ids: list[str] = Field(description="BR IDs delivered in this phase")
    dependency_ids: list[str] = Field(description="External DEP IDs this phase relies on")
    estimate_basis: str
    confidence: Literal["low", "medium", "high"]


class Milestone(BaseModel):
    id: str = Field(description="Letters, digits and underscores, starting with a letter, such as m1_kickoff")
    name: str
    phase_id: str
    at: Literal["start", "end"]
    criteria: str


class Release(BaseModel):
    name: str
    milestone_id: str
    requirement_ids: list[str]
    exit_criteria: str


class Deferred(BaseModel):
    requirement_id: str
    reason: str


class Roadmap(BaseModel):
    phases: list[Phase]
    milestones: list[Milestone]
    releases: list[Release]
    deferred: list[Deferred] = Field(description="Requirements not in any release; Must requirements cannot be deferred")
    risks: list[str]


class ProjectPlan(BaseModel):
    review: Review
    resourcing: Resourcing
    roadmap: Roadmap


SYSTEM_PROMPT = (
    "You are a project manager preparing a high-level delivery plan for the delivery partner (XBS). "
    "Do three things. 1) Review the proposal documents: objectives, scope, assumptions, constraints, "
    "stakeholders, dependencies and any stated timelines, each cited with chunk IDs. 2) Determine the "
    "skills, roles, team size, availability and onboarding the architecture needs. 3) Prepare phases, "
    "milestones, releases and a delivery roadmap. Use only facts from the documents or the planning "
    "inputs; the documents give no dates, budget, team or availability unless the planning inputs do. "
    "Everything else is your estimate: say so in estimate_basis, set confidence honestly, mark roles "
    "as 'assumed', and list the missing facts in information_gaps. Do not compute calendar dates or "
    "week numbers; give phase durations and dependencies and the schedule is calculated for you. "
    "Every Must requirement must be in a release; Should requirements may be deferred with a reason. "
    "Every external dependency must be resolved by an onboarding item or relied on by a phase, and "
    "every architecture tech-stack layer must be covered by a role. "
    f"Submit the result by calling the {TOOL_NAME} tool."
)


def load_inputs() -> dict | None:
    if not INPUTS_FILE.is_file():
        return None
    return json.loads(INPUTS_FILE.read_text(encoding="utf-8"))


def next_monday(today: date | None = None) -> date:
    today = today or date.today()
    return today + timedelta(days=(7 - today.weekday()) % 7 or 7)


def compute_schedule(phases: list[Phase]) -> tuple[dict[str, tuple[int, int]], list[str]]:
    """Return ({phase_id: (start_week, end_week)} as week offsets, critical path).

    Raises ValueError for unknown dependencies or cycles."""
    by_id = {p.id: p for p in phases}
    for p in phases:
        for dep in p.depends_on:
            if dep not in by_id:
                raise ValueError(f"phase {p.id} depends on unknown phase {dep}")

    schedule: dict[str, tuple[int, int]] = {}
    previous: dict[str, str | None] = {}
    remaining = list(phases)
    while remaining:
        ready = [p for p in remaining if all(d in schedule for d in p.depends_on)]
        if not ready:
            raise ValueError("phase dependencies contain a cycle: " + ", ".join(p.id for p in remaining))
        for p in ready:
            start, driver = 0, None
            for dep in p.depends_on:
                if schedule[dep][1] > start:
                    start, driver = schedule[dep][1], dep
            schedule[p.id] = (start, start + p.duration_weeks)
            previous[p.id] = driver
            remaining.remove(p)

    path, current = [], max(schedule, key=lambda i: schedule[i][1]) if schedule else None
    while current:
        path.append(current)
        current = previous[current]
    return schedule, list(reversed(path))


def peak_team(plan: ProjectPlan) -> dict[str, float]:
    return {
        p.id: sum(r.fte for r in plan.resourcing.roles if p.id in r.phase_ids)
        for p in plan.roadmap.phases
    }


def verify(
    plan: ProjectPlan,
    chunk_ids: set[str],
    must_ids: list[str],
    all_ids: list[str],
    tech_layers: list[str],
    has_inputs: bool,
) -> list[str]:
    problems = []
    review, resourcing, roadmap = plan.review, plan.resourcing, plan.roadmap

    cited = (
        [("objective", i) for i in review.objectives]
        + [("scope_in item", i) for i in review.scope_in]
        + [("scope_out item", i) for i in review.scope_out]
        + [("assumption", i) for i in review.assumptions]
        + [("constraint", i) for i in review.constraints]
        + [("stakeholder", i) for i in review.stakeholders]
        + [("timeline statement", i) for i in review.timeline_statements]
        + [(f"dependency {d.id}", d) for d in review.dependencies]
    )
    for label, item in cited:
        name = getattr(item, "text", None) or getattr(item, "description", "")
        if not item.citations:
            problems.append(f"{label} '{name[:50]}' has no citations")
        for c in item.citations:
            if c not in chunk_ids:
                problems.append(f"{label} '{name[:50]}' cites {c}, which is not a BRD/NFR chunk")

    if not has_inputs and not review.information_gaps:
        problems.append("no planning inputs were supplied, so information_gaps must list the missing facts")

    phase_ids = [p.id for p in roadmap.phases]
    for dup in sorted({i for i in phase_ids if phase_ids.count(i) > 1}):
        problems.append(f"phase ID {dup} is used more than once")
    for p in roadmap.phases:
        if not SAFE_ID.match(p.id):
            problems.append(f"phase ID '{p.id}' must use letters, digits and underscores and start with a letter")
        if not p.estimate_basis.strip():
            problems.append(f"phase {p.id} has no estimate basis")
        for r in p.requirement_ids:
            if r not in all_ids:
                problems.append(f"phase {p.id} references unknown requirement {r}")
    try:
        compute_schedule(roadmap.phases)
    except ValueError as error:
        problems.append(str(error))

    dependency_ids = [d.id for d in review.dependencies]
    for dup in sorted({i for i in dependency_ids if dependency_ids.count(i) > 1}):
        problems.append(f"dependency ID {dup} is used more than once")
    referenced_deps = {d for o in resourcing.onboarding for d in o.dependency_ids} | {
        d for p in roadmap.phases for d in p.dependency_ids
    }
    for dep in sorted(referenced_deps - set(dependency_ids)):
        problems.append(f"{dep} is referenced but not listed in review.dependencies")
    for dep in dependency_ids:
        if dep not in referenced_deps:
            problems.append(f"dependency {dep} is not resolved by any onboarding item or phase")

    milestone_ids = [m.id for m in roadmap.milestones]
    for m in roadmap.milestones:
        if not SAFE_ID.match(m.id):
            problems.append(f"milestone ID '{m.id}' must use letters, digits and underscores and start with a letter")
        if m.phase_id not in phase_ids:
            problems.append(f"milestone {m.id} references unknown phase {m.phase_id}")
    if not roadmap.milestones:
        problems.append("the roadmap has no milestones")

    in_releases = set()
    for rel in roadmap.releases:
        if rel.milestone_id not in milestone_ids:
            problems.append(f"release '{rel.name}' references unknown milestone {rel.milestone_id}")
        for r in rel.requirement_ids:
            in_releases.add(r)
            if r not in all_ids:
                problems.append(f"release '{rel.name}' references unknown requirement {r}")
    if not roadmap.releases:
        problems.append("the roadmap has no releases")
    deferred = {d.requirement_id for d in roadmap.deferred}
    for req_id in must_ids:
        if req_id in deferred:
            problems.append(f"Must requirement {req_id} cannot be deferred")
        elif req_id not in in_releases:
            problems.append(f"Must requirement {req_id} is not in any release")
    for req_id in all_ids:
        if req_id not in in_releases and req_id not in deferred:
            problems.append(f"requirement {req_id} is neither in a release nor deferred")

    covered_layers = {layer.lower() for r in resourcing.roles for layer in r.covers_tech_layers}
    for layer in tech_layers:
        if layer.lower() not in covered_layers:
            problems.append(f"tech-stack layer '{layer}' is not covered by any role")
    for r in resourcing.roles:
        if not r.phase_ids:
            problems.append(f"role '{r.role}' works in no phase")
        for pid in r.phase_ids:
            if pid not in phase_ids:
                problems.append(f"role '{r.role}' references unknown phase {pid}")
        if r.source == "inputs" and not has_inputs:
            problems.append(f"role '{r.role}' is marked 'inputs' but no planning inputs were supplied")
    for o in resourcing.onboarding:
        if o.needed_before_phase not in phase_ids:
            problems.append(f"onboarding item '{o.item[:50]}' references unknown phase {o.needed_before_phase}")

    return problems


def build_prompt(
    chunks: list[dict],
    doc: FunctionalDocument,
    architecture: ArchitectureDesign,
    requirements: list[dict],
    inputs: dict | None,
) -> str:
    sections = "\n\n".join(f"[{c['chunk_id']}] ({c['source']} | {c['heading']})\n{c['text']}" for c in chunks)
    checklist = "\n".join(f"- {r['id']} ({r['priority']}): {r['text']}" for r in requirements)
    stack = "\n".join(f"- {t.layer}: {t.technology}" for t in architecture.tech_stack)
    components = "\n".join(f"- {c.name} ({c.layer})" for c in architecture.components)
    architecture_risks = "\n".join(f"- {r}" for r in architecture.risks)
    gaps = "\n".join(f"- {q.question}" for q in doc.open_questions)
    planning = (
        "Planning inputs (facts supplied by the user):\n" + json.dumps(inputs, indent=2)
        if inputs
        else "Planning inputs: none supplied. Start date, deadline, budget and team availability are unknown."
    )
    return (
        f"Proposal documents (cite these chunk IDs):\n\n{sections}\n\n"
        f"Requirements to schedule (every Must needs a release):\n{checklist}\n\n"
        f"Architecture tech stack (each layer needs a covering role):\n{stack}\n\n"
        f"Architecture components:\n{components}\n\nArchitecture risks:\n{architecture_risks}\n\n"
        f"Open questions from the functional analysis:\n{gaps}\n\n{planning}"
    )


def week_label(offset: int, at: str) -> str:
    """Offsets count elapsed weeks: a start is the first day of week offset+1, an end is the close of week offset."""
    return f"start of Week {offset + 1}" if at == "start" else f"end of Week {offset}"


def render_gantt(plan: ProjectPlan, schedule: dict[str, tuple[int, int]], critical: list[str], start: date) -> str:
    def clean(text: str) -> str:
        return re.sub(r"[:;#]", " ", text)

    lines = ["gantt", "    title Delivery roadmap", "    dateFormat YYYY-MM-DD", "    axisFormat %d %b", "    section Phases"]
    for p in plan.roadmap.phases:
        begin, _ = schedule[p.id]
        flag = "crit, " if p.id in critical else ""
        lines.append(f"    {clean(p.name)} :{flag}{p.id}, {start + timedelta(weeks=begin)}, {p.duration_weeks}w")
    lines.append("    section Milestones")
    for m in plan.roadmap.milestones:
        begin, end = schedule[m.phase_id]
        day = start + timedelta(weeks=begin if m.at == "start" else end)
        lines.append(f"    {clean(m.name)} :milestone, {m.id}, {day}, 0d")
    return "\n".join(lines)


def cell(text: str) -> str:
    return str(text).replace("|", "/").replace("\n", " ")


def bullets(items: list[Item]) -> list[str]:
    return [f"- {i.text} {cite(i.citations)}" for i in items] or ["- none stated"]


def render_markdown(plan: ProjectPlan, problems: list[str], start: date, start_is_given: bool, has_inputs: bool) -> str:
    review, resourcing, roadmap = plan.review, plan.resourcing, plan.roadmap
    schedule, critical = compute_schedule(roadmap.phases)
    peak = peak_team(plan)
    total_fte = sum(r.fte for r in resourcing.roles)
    end_week = max((e for _, e in schedule.values()), default=0)

    lines = ["# Project Plan: XATA TODO Management Platform", ""]
    lines += [
        "> Durations, team size and dates are **estimates**"
        + (" informed by the supplied planning inputs." if has_inputs else "; no planning inputs were supplied.")
        + " Weeks are calculated from phase durations and dependencies.",
        "",
        "## 1. Proposal review",
        "",
    ]
    for title, items in [
        ("Business objectives", review.objectives),
        ("Scope: in", review.scope_in),
        ("Scope: out", review.scope_out),
        ("Assumptions", review.assumptions),
        ("Constraints", review.constraints),
        ("Stakeholders", review.stakeholders),
        ("Timelines stated in the documents", review.timeline_statements),
    ]:
        lines += [f"### {title}", ""] + bullets(items) + [""]
    lines += ["### Dependencies", ""]
    lines += [f"- **{d.id}** {d.description} (owner: {d.owner}) {cite(d.citations)}" for d in review.dependencies] or ["- none"]
    lines += ["", "### Information gaps", ""] + [f"- {g}" for g in review.information_gaps] + [""]

    lines += [
        "## 2. Resourcing and onboarding",
        "",
        f"Total team: **{total_fte:g} FTE** across {len(resourcing.roles)} roles; "
        f"peak concurrent staffing {max(peak.values(), default=0):g} FTE.",
        "",
        "| Role | FTE | Skills | Phases | Covers | Source | Rationale |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in resourcing.roles:
        lines.append(
            f"| {cell(r.role)} | {r.fte:g} | {cell(', '.join(r.skills))} | {', '.join(r.phase_ids)} | "
            f"{cell(', '.join(r.covers_tech_layers))} | {r.source} | {cell(r.rationale)} |"
        )
    lines += ["", "### Onboarding checklist", ""]
    lines += [
        f"- [ ] {o.item} (owner: {o.owner}; before {o.needed_before_phase}"
        + (f"; resolves {', '.join(o.dependency_ids)}" if o.dependency_ids else "")
        + ")"
        for o in resourcing.onboarding
    ]
    lines += ["", "### Availability assumptions", ""] + [f"- {a}" for a in resourcing.availability_assumptions]
    lines += ["", "### Resource risks", ""] + [f"- {r}" for r in resourcing.resource_risks]

    lines += [
        "",
        "## 3. Delivery roadmap",
        "",
        f"Total duration: **{end_week} weeks**. Critical path: {' → '.join(critical)}. "
        + (f"Start date {start}." if start_is_given else f"Start date {start} is a nominal placeholder, not a commitment."),
        "",
        "| Phase | Weeks | Duration | Depends on | Staffing (FTE) | Confidence | Estimate basis |",
        "|---|---|---|---|---|---|---|",
    ]
    for p in roadmap.phases:
        s, e = schedule[p.id]
        lines.append(
            f"| {cell(p.name)} (`{p.id}`) | {s + 1}-{e} | {p.duration_weeks} | {', '.join(p.depends_on) or '-'} | "
            f"{peak[p.id]:g} | {p.confidence} | {cell(p.estimate_basis)} |"
        )
    lines += ["", "### Phase deliverables", ""]
    for p in roadmap.phases:
        lines.append(f"- **{p.name}**: {'; '.join(p.deliverables)}")

    lines += ["", "### Milestones", "", "| Milestone | Week | Phase | Criteria |", "|---|---|---|---|"]
    for m in roadmap.milestones:
        s, e = schedule[m.phase_id]
        lines.append(f"| {cell(m.name)} | {week_label(s if m.at == 'start' else e, m.at)} | {m.phase_id} | {cell(m.criteria)} |")

    milestone_week = {
        m.id: week_label(schedule[m.phase_id][0 if m.at == "start" else 1], m.at)
        for m in roadmap.milestones
        if m.phase_id in schedule
    }
    lines += ["", "### Releases", "", "| Release | Week | Requirements | Exit criteria |", "|---|---|---|---|"]
    for rel in roadmap.releases:
        week = milestone_week.get(rel.milestone_id, "?")
        lines.append(f"| {cell(rel.name)} | {week} | {', '.join(rel.requirement_ids)} | {cell(rel.exit_criteria)} |")
    if roadmap.deferred:
        lines += ["", "Deferred: " + "; ".join(f"{d.requirement_id} ({d.reason})" for d in roadmap.deferred)]

    lines += ["", "### Gantt chart", "", "```mermaid", render_gantt(plan, schedule, critical, start), "```", ""]
    lines += ["### Delivery risks", ""] + [f"- {r}" for r in roadmap.risks]

    if problems:
        lines += ["", "## Unresolved verification problems", ""] + [f"- {p}" for p in problems]
    return "\n".join(lines) + "\n"


def generate(client, chunks, doc, architecture, requirements, inputs, feedback: list[str] | None = None):
    chunk_ids = {c["chunk_id"] for c in chunks}
    must_ids = [r["id"] for r in requirements if r["priority"] == "Must"]
    all_ids = [r["id"] for r in requirements]
    layers = [t.layer for t in architecture.tech_stack]
    return generate_with_repair(
        client,
        SYSTEM_PROMPT,
        TOOL_NAME,
        "Submit the completed project plan.",
        ProjectPlan,
        with_feedback(build_prompt(chunks, doc, architecture, requirements, inputs), feedback),
        lambda plan: verify(plan, chunk_ids, must_ids, all_ids, layers, inputs is not None),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Review the proposal and produce resourcing and a delivery roadmap.")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be sent without calling Claude.")
    parser.add_argument("--start-date", help="Project start date, YYYY-MM-DD (overrides planning inputs).")
    args = parser.parse_args()

    for name in ("functional_document.json", "architecture.json"):
        if not (OUTPUT_DIR / name).is_file():
            raise SystemExit(f"{OUTPUT_DIR / name} not found. Run the Functional Analyst and Solution Architect first.")
    doc = FunctionalDocument.model_validate_json((OUTPUT_DIR / "functional_document.json").read_text(encoding="utf-8"))
    architecture = ArchitectureDesign.model_validate_json((OUTPUT_DIR / "architecture.json").read_text(encoding="utf-8"))
    inputs = load_inputs()

    with connect() as driver:
        brd_chunks = fetch_chunks(driver, BRD_SOURCE)
        nfr_chunks = fetch_chunks(driver, NFR_SOURCE)
    if not brd_chunks or not nfr_chunks:
        raise SystemExit("BRD or NFR chunks missing from Neo4j. Run the upload first.")

    chunks = brd_chunks + nfr_chunks
    requirements = parse_requirements(brd_chunks)
    must = [r["id"] for r in requirements if r["priority"] == "Must"]
    layers = [t.layer for t in architecture.tech_stack]
    start_text = args.start_date or (inputs or {}).get("start_date")
    start_is_given = bool(start_text)
    start = date.fromisoformat(start_text) if start_text else next_monday()

    print(f"Loaded {len(chunks)} BRD/NFR sections, {len(requirements)} requirements ({len(must)} Must), {len(layers)} tech-stack layers.")
    print("Tech-stack layers: " + ", ".join(layers))
    print(f"Planning inputs: {'found at ' + str(INPUTS_FILE) if inputs else 'not supplied (plan will rely on flagged assumptions)'}")
    print(f"Start date: {start} ({'given' if start_is_given else 'nominal placeholder'})")

    if args.dry_run:
        print(f"Prompt size: {len(build_prompt(chunks, doc, architecture, requirements, inputs))} characters")
        return

    try:
        plan, problems = generate(get_client(), chunks, doc, architecture, requirements, inputs)
    except ValidationError as error:
        raise SystemExit(f"Claude returned a plan that does not match the schema:\n{error}")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUTPUT_DIR / "project_plan.json").write_text(plan.model_dump_json(indent=2), encoding="utf-8")
    try:
        markdown = render_markdown(plan, problems, start, start_is_given, inputs is not None)
    except (ValueError, KeyError) as error:
        raise SystemExit(f"Could not render the plan ({error}). Problems found: {problems}")
    (OUTPUT_DIR / "project_plan.md").write_text(markdown, encoding="utf-8")
    print(f"Wrote project_plan.md and .json to {OUTPUT_DIR}")
    if problems:
        print(f"WARNING: {len(problems)} verification problem(s) remain:")
        for problem in problems:
            print(f"- {problem}")
    else:
        print("Verification passed: citations valid, Must requirements released, dependencies resolved, tech layers staffed.")


if __name__ == "__main__":
    main()

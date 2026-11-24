import argparse
import getpass
import hashlib
import json
import os
import re
import sys
from datetime import date, datetime
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR))  # lets `app.*` imports work when run by path

from pydantic import ValidationError

from app.agents import functional_analyst as fa
from app.agents import project_manager as pm
from app.agents import solution_architect as sa
from app.agents.common import DEFAULT_MODEL, get_client
from app.rag.retrieval import connect, fetch_chunks


RUNS_DIR = BACKEND_DIR / "data" / "runs"
AGENT_NAMES = {"fa": "Functional Analyst", "sa": "Solution Architect", "pm": "Project Manager"}
ARTIFACT_FILES = {
    "fa": "functional_document.json",
    "sa": "architecture.json",
    "pm": "project_plan.json",
}
ID_PATTERN = re.compile(r"\b(BR-\d+|NFR-[A-Z]+-\d+)\b")


# ---------------------------------------------------------------- findings


def finding(owner: str, problem: str, correction: str, requirement: str | None = None) -> dict:
    found = ID_PATTERN.search(problem)
    return {
        "owner": owner,
        "artifact": ARTIFACT_FILES[owner],
        "requirement": requirement or (found.group(1) if found else "-"),
        "problem": problem,
        "correction": correction,
    }


def cross_check(plan: pm.ProjectPlan) -> list[dict]:
    """Checks that span agents and that no single agent verifies on its own."""
    findings = []
    try:
        schedule, _ = pm.compute_schedule(plan.roadmap.phases)
    except ValueError:
        return findings  # the Project Manager's own verification already reports this

    milestone_end = {
        m.id: schedule[m.phase_id][0 if m.at == "start" else 1]
        for m in plan.roadmap.milestones
        if m.phase_id in schedule
    }
    delivered_by: dict[str, list[int]] = {}
    for phase in plan.roadmap.phases:
        for req_id in phase.requirement_ids:
            delivered_by.setdefault(req_id, []).append(schedule[phase.id][1])

    for release in plan.roadmap.releases:
        due = milestone_end.get(release.milestone_id)
        if due is None:
            continue
        for req_id in release.requirement_ids:
            ends = delivered_by.get(req_id)
            if not ends:
                findings.append(finding(
                    "pm",
                    f"{req_id} is in release '{release.name}' but no phase lists it as delivered",
                    f"Add {req_id} to the requirement_ids of the phase that builds it, before the release milestone.",
                ))
            elif min(ends) > due:
                findings.append(finding(
                    "pm",
                    f"{req_id} is in release '{release.name}' (due end of week {due}) but its earliest delivering phase ends in week {min(ends)}",
                    f"Move {req_id} to a later release or deliver it in a phase that finishes by week {due}.",
                ))
    return findings


def collect_findings(problems: dict[str, list[str]], plan: pm.ProjectPlan | None) -> list[dict]:
    findings = []
    for owner, items in problems.items():
        for problem in items:
            findings.append(finding(owner, problem, "Resolve this verification problem in the revised artifact."))
    if plan is not None:
        findings += cross_check(plan)
    return findings


# ------------------------------------------------------------- traceability


def traceability_markdown(requirements: list[dict], doc: fa.FunctionalDocument, design: sa.ArchitectureDesign,
                          plan: pm.ProjectPlan, targets: list[dict]) -> str:
    try:
        schedule, _ = pm.compute_schedule(plan.roadmap.phases)
    except ValueError:
        schedule = {}
    milestone_end = {m.id: schedule[m.phase_id][0 if m.at == "start" else 1] for m in plan.roadmap.milestones if m.phase_id in schedule}

    lines = [
        "# Traceability",
        "",
        "## Requirements",
        "",
        "| Requirement | Priority | Acceptance criteria | Decisions | Phases | Releases |",
        "|---|---|---|---|---|---|",
    ]
    for req in requirements:
        rid = req["id"]
        criteria = sum(1 for a in doc.acceptance_criteria if rid in a.requirement_ids)
        decisions = [d.id for d in design.decisions if rid in d.requirement_ids]
        phases = [p.id for p in plan.roadmap.phases if rid in p.requirement_ids]
        releases = [
            f"{r.name} (end of week {milestone_end[r.milestone_id]})" if r.milestone_id in milestone_end else r.name
            for r in plan.roadmap.releases
            if rid in r.requirement_ids
        ]
        deferred = [d.reason for d in plan.roadmap.deferred if d.requirement_id == rid]
        lines.append(
            f"| {rid} | {req['priority']} | {criteria or '-'} | {', '.join(decisions) or '-'} | "
            f"{', '.join(phases) or '-'} | {', '.join(releases) or ('deferred: ' + deferred[0] if deferred else '-')} |"
        )

    lines += ["", "## NFR targets", "", "| Target | Requirement | Decisions |", "|---|---|---|"]
    for target in targets:
        decisions = [d.id for d in design.decisions if target["id"] in d.requirement_ids]
        lines.append(f"| {target['id']} | {target['requirement']} → {target['target']} | {', '.join(decisions) or '-'} |")
    return "\n".join(lines).replace("|  |", "| - |") + "\n"


# ------------------------------------------------------------- persistence


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def bundle_hash(file_hashes: dict[str, str]) -> str:
    return sha256("\n".join(f"{name}:{file_hashes[name]}" for name in sorted(file_hashes)).encode("utf-8"))


def write_version(run_dir: Path, number: int, files: dict[str, str], manifest: dict) -> dict:
    version_dir = run_dir / f"v{number}"
    version_dir.mkdir(parents=True, exist_ok=False)  # versions are never overwritten
    hashes = {}
    for name, content in files.items():
        data = content.encode("utf-8")
        (version_dir / name).write_bytes(data)
        hashes[name] = sha256(data)
    manifest = {**manifest, "version": number, "files": hashes, "bundle_hash": bundle_hash(hashes)}
    (version_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest


def verify_version(version_dir: Path) -> tuple[dict, list[str]]:
    """Recompute hashes from disk; returns (manifest, list of problems)."""
    manifest = json.loads((version_dir / "manifest.json").read_text(encoding="utf-8"))
    problems = []
    current = {}
    for name, expected in manifest["files"].items():
        path = version_dir / name
        if not path.is_file():
            problems.append(f"{name} is missing")
            continue
        current[name] = sha256(path.read_bytes())
        if current[name] != expected:
            problems.append(f"{name} has changed since the version was written")
    if not problems and bundle_hash(current) != manifest["bundle_hash"]:
        problems.append("bundle hash does not match the manifest")
    return manifest, problems


# ---------------------------------------------------------------- pipeline


def run_pipeline(client, brd_chunks: list[dict], nfr_chunks: list[dict], inputs: dict | None,
                 start: date, start_is_given: bool, run_dir: Path | None, max_rounds: int = 2) -> list[dict]:
    """Run the three agents, cross-check, route findings back for rework, and persist each pass."""
    chunks = brd_chunks + nfr_chunks
    requirements = fa.parse_requirements(brd_chunks)
    must_ids = [r["id"] for r in requirements if r["priority"] == "Must"]
    targets = sa.parse_nfr_targets(nfr_chunks)
    categories = sa.parse_categories(nfr_chunks)

    artifacts: dict[str, object] = {}
    problems: dict[str, list[str]] = {"fa": [], "sa": [], "pm": []}
    feedback: dict[str, list[str]] = {"fa": [], "sa": [], "pm": []}
    dirty = {"fa", "sa", "pm"}
    manifests = []

    for round_number in range(max_rounds + 1):
        sent = {k: list(v) for k, v in feedback.items() if v}
        ran = []
        if "fa" in dirty:
            print(f"[v{round_number + 1}] Functional Analyst")
            artifacts["fa"], problems["fa"] = fa.generate(client, brd_chunks, requirements, feedback["fa"])
            dirty |= {"sa", "pm"}
            ran.append("fa")
        if "sa" in dirty:
            print(f"[v{round_number + 1}] Solution Architect")
            artifacts["sa"], problems["sa"] = sa.generate(client, artifacts["fa"], chunks, targets, categories, must_ids, feedback["sa"])
            dirty |= {"pm"}
            ran.append("sa")
        if "pm" in dirty:
            print(f"[v{round_number + 1}] Project Manager")
            artifacts["pm"], problems["pm"] = pm.generate(client, chunks, artifacts["fa"], artifacts["sa"], requirements, inputs, feedback["pm"])
            ran.append("pm")

        findings = collect_findings(problems, artifacts["pm"])
        number = round_number + 1
        if run_dir is not None:
            files = {
                "functional_document.json": artifacts["fa"].model_dump_json(indent=2),
                "functional_document.md": fa.render_markdown(artifacts["fa"], requirements, problems["fa"]),
                "architecture.json": artifacts["sa"].model_dump_json(indent=2),
                "architecture.md": sa.render_markdown(artifacts["sa"], problems["sa"]),
                "project_plan.json": artifacts["pm"].model_dump_json(indent=2),
                "project_plan.md": pm.render_markdown(artifacts["pm"], problems["pm"], start, start_is_given, inputs is not None),
                "traceability.md": traceability_markdown(requirements, artifacts["fa"], artifacts["sa"], artifacts["pm"], targets),
            }
            manifest = write_version(run_dir, number, files, {
                "run_id": run_dir.name,
                "created_at": datetime.now().isoformat(timespec="seconds"),
                "model": os.getenv("ANTHROPIC_MODEL", DEFAULT_MODEL),
                "agents_run": [AGENT_NAMES[a] for a in ran],
                "agent_problems": problems,
                "findings": findings,
                "feedback_sent": sent,
            })
        else:
            manifest = {"version": number, "findings": findings, "agents_run": [AGENT_NAMES[a] for a in ran]}
        manifests.append(manifest)
        print(f"[v{number}] saved; {len(findings)} open finding(s)")

        if not findings or round_number == max_rounds:
            break

        feedback = {k: [] for k in feedback}
        for f in findings:
            feedback[f["owner"]].append(f"{f['problem']}. Correction: {f['correction']}")
        dirty = {owner for owner, items in feedback.items() if items}

    return manifests


# ---------------------------------------------------------------- commands


def load_context() -> tuple[list[dict], list[dict], dict | None]:
    with connect() as driver:
        brd = fetch_chunks(driver, fa.BRD_SOURCE)
        nfr = fetch_chunks(driver, sa.NFR_SOURCE)
    if not brd or not nfr:
        raise SystemExit("BRD or NFR chunks missing from Neo4j. Run the upload first.")
    return brd, nfr, pm.load_inputs()


def cmd_run(args: argparse.Namespace) -> None:
    brd, nfr, inputs = load_context()
    start_text = args.start_date or (inputs or {}).get("start_date")
    start = date.fromisoformat(start_text) if start_text else pm.next_monday()

    print(f"Loaded {len(brd)} BRD and {len(nfr)} NFR sections. Planning inputs: {'found' if inputs else 'none'}.")
    print("Order: Functional Analyst -> Solution Architect -> Project Manager; "
          f"up to {args.max_rounds} rework round(s) when findings are routed back to the owning agent.")
    if args.dry_run:
        print("Dry run: nothing was sent to Claude and no files were written.")
        return

    run_id = datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir = RUNS_DIR / run_id
    manifests = run_pipeline(get_client(), brd, nfr, inputs, start, bool(start_text), run_dir, args.max_rounds)

    last = manifests[-1]
    print(f"\nRun {run_id}: {len(manifests)} version(s) in {run_dir}")
    print(f"Latest version v{last['version']}, bundle hash {last['bundle_hash']}")
    if last["findings"]:
        print(f"{len(last['findings'])} finding(s) remain open:")
        for f in last["findings"]:
            print(f"- [{f['owner']}] {f['problem']}")
    print(f"\nReview {run_dir / ('v' + str(last['version']))}, then approve with:")
    print(f"  python backend/app/orchestrator.py approve {run_id} --version {last['version']}"
          + (" --accept-open-issues" if last["findings"] else ""))


def run_dirs() -> list[Path]:
    return sorted(p for p in RUNS_DIR.iterdir() if p.is_dir()) if RUNS_DIR.is_dir() else []


def versions_of(run_dir: Path) -> list[Path]:
    return sorted((p for p in run_dir.glob("v*") if p.is_dir()), key=lambda p: int(p.name[1:]))


def read_approval(run_dir: Path) -> dict | None:
    path = run_dir / "approval.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None


def cmd_status(args: argparse.Namespace) -> None:
    runs = [RUNS_DIR / args.run_id] if args.run_id else run_dirs()
    if not runs:
        print("No runs yet.")
        return
    for run_dir in runs:
        if not run_dir.is_dir():
            raise SystemExit(f"Run {run_dir.name} not found.")
        approval = read_approval(run_dir)
        print(f"Run {run_dir.name}")
        for version_dir in versions_of(run_dir):
            manifest, problems = verify_version(version_dir)
            approved = approval and approval["version"] == manifest["version"] and approval["bundle_hash"] == manifest["bundle_hash"]
            print(f"  v{manifest['version']}  {manifest['bundle_hash'][:8]}  findings: {len(manifest['findings'])}  "
                  f"{'APPROVED' if approved else 'not approved'}{'  INTEGRITY PROBLEM: ' + '; '.join(problems) if problems else ''}")


def cmd_approve(args: argparse.Namespace) -> None:
    run_dir = RUNS_DIR / args.run_id
    if not run_dir.is_dir():
        raise SystemExit(f"Run {args.run_id} not found.")
    versions = versions_of(run_dir)
    if not versions:
        raise SystemExit("This run has no versions.")
    version_dir = run_dir / f"v{args.version}" if args.version else versions[-1]
    if not version_dir.is_dir():
        raise SystemExit(f"Version {version_dir.name} not found.")

    manifest, problems = verify_version(version_dir)
    if problems:
        raise SystemExit("Refusing to approve; the version does not match its manifest: " + "; ".join(problems))

    findings = manifest["findings"]
    expected = manifest["bundle_hash"][:8]
    print(f"Run {args.run_id}, version v{manifest['version']} (created {manifest['created_at']})")
    print("Deliverables: " + ", ".join(sorted(manifest["files"])))
    print(f"Bundle hash: {manifest['bundle_hash']}")
    print(f"Open findings: {len(findings)}")
    for f in findings:
        print(f"- [{AGENT_NAMES[f['owner']]}] {f['problem']}")
    if manifest["version"] != int(versions[-1].name[1:]):
        print(f"Note: v{versions[-1].name[1:]} is a newer version of this run.")
    if findings and not args.accept_open_issues:
        raise SystemExit("This version has open findings. Fix them in a new run, or approve with --accept-open-issues.")

    typed = args.confirm
    if typed is None:
        if not sys.stdin.isatty():
            raise SystemExit(f"Not interactive. Re-run with --confirm {expected} after reviewing the files.")
        typed = input(f"Type the first 8 characters of the bundle hash ({expected}) to approve this exact version: ").strip()
    if typed != expected:
        raise SystemExit("Confirmation does not match the bundle hash; nothing was approved.")

    approval = {
        "run_id": args.run_id,
        "version": manifest["version"],
        "bundle_hash": manifest["bundle_hash"],
        "approved_by": getpass.getuser(),
        "approved_at": datetime.now().isoformat(timespec="seconds"),
        "open_issues_accepted": len(findings) if findings else 0,
    }
    (run_dir / "approval.json").write_text(json.dumps(approval, indent=2), encoding="utf-8")
    print(f"Approved v{manifest['version']} ({manifest['bundle_hash'][:8]}) as {approval['approved_by']}.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the NEXUS agents with versioned output and a human approval gate.")
    commands = parser.add_subparsers(dest="command", required=True)

    run = commands.add_parser("run", help="Run the three agents, route rework, and save versions.")
    run.add_argument("--dry-run", action="store_true")
    run.add_argument("--max-rounds", type=int, default=2, help="Rework rounds after the first pass (default 2).")
    run.add_argument("--start-date", help="Project start date YYYY-MM-DD (overrides planning inputs).")

    status = commands.add_parser("status", help="Show runs, versions and approval state.")
    status.add_argument("run_id", nargs="?")

    approve = commands.add_parser("approve", help="Approve one exact version of a run.")
    approve.add_argument("run_id")
    approve.add_argument("--version", type=int, help="Version number (default: latest).")
    approve.add_argument("--confirm", help="First 8 characters of the bundle hash (otherwise asked interactively).")
    approve.add_argument("--accept-open-issues", action="store_true")

    args = parser.parse_args()
    try:
        {"run": cmd_run, "status": cmd_status, "approve": cmd_approve}[args.command](args)
    except ValidationError as error:
        raise SystemExit(f"An agent returned data that does not match its schema:\n{error}")


if __name__ == "__main__":
    main()

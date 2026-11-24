import json
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
DOCUMENTS_DIR = BACKEND_DIR / "data" / "documents"
ACTIVE_FILE = BACKEND_DIR / "data" / "active_documents.json"

DEFAULT_BRD = "XATA_TODO_BRD.docx"
DEFAULT_NFR = "XATA_TODO_NFR.docx"


def get_active() -> dict[str, str]:
    """The BRD/NFR file names the agents read from Neo4j (defaults to the sample documents)."""
    active = {"brd": DEFAULT_BRD, "nfr": DEFAULT_NFR}
    if ACTIVE_FILE.is_file():
        try:
            saved = json.loads(ACTIVE_FILE.read_text(encoding="utf-8"))
            active.update({k: saved[k] for k in ("brd", "nfr") if saved.get(k)})
        except (ValueError, OSError):
            pass
    return active


def set_active(brd: str, nfr: str) -> None:
    ACTIVE_FILE.parent.mkdir(parents=True, exist_ok=True)
    ACTIVE_FILE.write_text(json.dumps({"brd": brd, "nfr": nfr}, indent=2), encoding="utf-8")


def brd_source() -> str:
    return get_active()["brd"]


def nfr_source() -> str:
    return get_active()["nfr"]


def project_title() -> str:
    """Project name for document headings, taken from the 'RFP: ...' line on the BRD cover page."""
    import re

    stem = Path(brd_source()).stem
    candidates = [BACKEND_DIR / "data" / "output" / f"{stem}_chunks.json"]
    if brd_source() == DEFAULT_BRD:
        candidates.append(BACKEND_DIR / "data" / "output" / "brd_chunks.json")
    for path in candidates:
        if path.is_file():
            try:
                for chunk in json.loads(path.read_text(encoding="utf-8")):
                    found = re.search(r"RFP:\s*(.+)", chunk["text"])
                    if found:
                        return found.group(1).strip()
            except (ValueError, KeyError, OSError):
                pass
    return stem.replace("_", " ")

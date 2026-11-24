import argparse
import json
import math
from pathlib import Path

from docx import Document
from docx.table import Table
from docx.text.paragraph import Paragraph
from transformers import AutoTokenizer


BACKEND_DIR = Path(__file__).resolve().parents[2]
MODEL_DIR = BACKEND_DIR / "models" / "all-MiniLM-L6-v2"

DOCUMENTS = {
    "brd": "XATA_TODO_BRD.docx",
    "nfr": "XATA_TODO_NFR.docx",
}


class TokenCounter:
    """Counts tokens exactly as the embedding step does: heading + text, with special tokens."""

    def __init__(self) -> None:
        self.tokenizer = AutoTokenizer.from_pretrained(str(MODEL_DIR), local_files_only=True)
        config = json.loads((MODEL_DIR / "sentence_bert_config.json").read_text(encoding="utf-8"))
        self.limit = config["max_seq_length"]

    def count(self, heading: str, text: str) -> int:
        return len(self.tokenizer(f"{heading}\n\n{text}", truncation=False)["input_ids"])


def render(units: list[dict]) -> str:
    """Join units: rows of one table with newlines, everything else with blank lines."""
    text = ""
    previous = None
    for unit in units:
        if previous is not None:
            same_table = unit["table"] is not None and unit["table"] == previous["table"]
            text += "\n" if same_table else "\n\n"
        text += unit["text"]
        previous = unit
    return text


def with_header(units: list[dict]) -> list[dict]:
    """Repeat the table header row when a part starts in the middle of a table."""
    first = units[0]
    if first["table"] is not None and first["row"] > 0:
        header = {"text": first["header"], "table": first["table"], "row": 0, "header": first["header"]}
        return [header, *units]
    return units


def split_units(units: list[dict], heading: str, counter: TokenCounter) -> list[list[dict]]:
    total = counter.count(heading, render(units))
    parts = max(1, math.ceil(total / counter.limit))

    while parts <= len(units):
        target = total / parts
        groups, current = [], []
        for unit in units:
            if current and len(groups) < parts - 1:
                if counter.count(heading, render(with_header(current + [unit]))) > target:
                    groups.append(current)
                    current = []
            current.append(unit)
        groups.append(current)

        if all(counter.count(heading, render(with_header(g))) <= counter.limit for g in groups):
            return groups
        parts += 1

    raise SystemExit(
        f"Section '{heading}' cannot be split to fit {counter.limit} tokens; "
        "a single paragraph or table row is too long. Shorten it in the source document."
    )


def chunk_document(path: Path, counter: TokenCounter) -> list[dict]:
    document = Document(str(path))
    chunks = []
    sections = 0
    heading = "Introduction"
    units: list[dict] = []
    tables = 0

    def save_section():
        nonlocal sections
        if not units:
            return

        sections += 1
        base_id = f"{path.stem}-{sections:03d}"
        groups = split_units(units, heading, counter)

        for index, group in enumerate(groups):
            suffix = f"-{chr(ord('A') + index)}" if len(groups) > 1 else ""
            text = render(with_header(group))
            chunks.append({
                "chunk_id": base_id + suffix,
                "source": path.name,
                "heading": heading,
                "text": text,
            })
            print(f"{base_id + suffix}: {counter.count(heading, text)} tokens")

    for block in document.iter_inner_content():
        if isinstance(block, Paragraph):
            text = block.text.strip()
            if not text:
                continue

            style = block.style.name if block.style else ""
            if style.startswith("Heading "):
                save_section()
                heading = text
                units = []
            else:
                units.append({"text": text, "table": None, "row": 0, "header": None})

        elif isinstance(block, Table):
            tables += 1
            rows = [
                " | ".join(cell.text.strip() for cell in row.cells)
                for row in block.rows
            ]
            for index, row in enumerate(rows):
                units.append({"text": row, "table": tables, "row": index, "header": rows[0]})

    save_section()
    return chunks


def ingest_file(path: Path, counter: TokenCounter | None = None) -> tuple[Path, list[dict]]:
    """Chunk any .docx and write data/output/<stem>_chunks.json (used for uploaded documents)."""
    chunks = chunk_document(path, counter or TokenCounter())
    output = BACKEND_DIR / "data" / "output" / f"{path.stem}_chunks.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(chunks, indent=2, ensure_ascii=False), encoding="utf-8")
    return output, chunks


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Chunk a Word document by heading, splitting sections that exceed the embedding model's token limit.")
    parser.add_argument("document", nargs="?", default="brd", choices=DOCUMENTS)
    args = parser.parse_args()

    source = BACKEND_DIR / "data" / "documents" / DOCUMENTS[args.document]
    output = BACKEND_DIR / "data" / "output" / f"{args.document}_chunks.json"

    chunks = chunk_document(source, TokenCounter())
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(chunks, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    print(f"Wrote {len(chunks)} chunks to {output}")

import os
import re

from app.rag.retrieval import connect, load_model, search_chunks


DEFAULT_MODEL = "claude-sonnet-5-5"
CITATION = re.compile(r"\[([A-Za-z0-9_\-]+)\]")

SYSTEM_PROMPT = (
    "You answer questions about a business requirements document (BRD) and a "
    "non-functional requirements document (NFR). Use only the passages provided. "
    "Cite every claim with the chunk ID of its passage in square brackets, for "
    "example [<document>-007]. If the passages do not answer the question, "
    "say 'Not stated in the documents.' and cite nothing."
)


def build_prompt(question: str, chunks: list[dict]) -> str:
    passages = "\n\n".join(
        f"[{c['chunk_id']}] ({c['source']} | {c['heading']})\n{c['text']}"
        for c in chunks
    )
    return f"Passages:\n\n{passages}\n\nQuestion: {question}"


def verify_citations(answer: str, chunks: list[dict]) -> tuple[list[str], list[str]]:
    """Return (cited chunk IDs, cited IDs that were not retrieved)."""
    retrieved = {c["chunk_id"] for c in chunks}
    cited = list(dict.fromkeys(CITATION.findall(answer)))
    invalid = [chunk_id for chunk_id in cited if chunk_id not in retrieved]
    return cited, invalid


def ask(question: str, chunks: list[dict], client=None) -> str:
    if client is None:
        if not os.getenv("ANTHROPIC_API_KEY"):
            raise SystemExit(
                "ANTHROPIC_API_KEY is not set. Add it to backend/.env or use --dry-run."
            )
        from anthropic import Anthropic

        client = Anthropic()

    response = client.messages.create(
        model=os.getenv("ANTHROPIC_MODEL", DEFAULT_MODEL),
        max_tokens=1024,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": build_prompt(question, chunks)}],
    )
    return "".join(block.text for block in response.content if block.type == "text")


def answer_question(question: str, model=None, limit: int = 5, sources: list[str] | None = None,
                    dry_run: bool = False) -> dict:
    """Retrieve, answer and verify citations. Returns chunks, answer (None on dry run), cited, invalid."""
    model = model or load_model()
    with connect() as driver:
        chunks = search_chunks(driver, model, question, limit=limit, sources=sources)
    if not chunks or dry_run:
        return {"chunks": chunks, "answer": None, "cited": [], "invalid": [],
                "prompt": build_prompt(question, chunks) if chunks else ""}
    answer = ask(question, chunks)
    cited, invalid = verify_citations(answer, chunks)
    return {"chunks": chunks, "answer": answer, "cited": cited, "invalid": invalid, "prompt": ""}


def run(question: str, dry_run: bool = False, limit: int = 5) -> None:
    model = load_model()
    with connect() as driver:
        chunks = search_chunks(driver, model, question, limit=limit)

    if not chunks:
        print("No indexed chunks found.")
        return

    if dry_run:
        print(f"System prompt:\n{SYSTEM_PROMPT}\n")
        print(build_prompt(question, chunks))
        return

    answer = ask(question, chunks)
    cited, invalid = verify_citations(answer, chunks)
    print(answer)
    print(f"\nRetrieved: {', '.join(c['chunk_id'] for c in chunks)}")
    print(f"Cited: {', '.join(cited) or 'none'}")
    if invalid:
        print(f"WARNING: cited chunks that were not retrieved: {', '.join(invalid)}")
    if not cited and not answer.startswith("Not stated"):
        print("WARNING: the answer contains no citations.")

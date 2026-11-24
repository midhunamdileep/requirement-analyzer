import argparse
import json
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BACKEND_DIR))  # lets `app.*` imports work when run by path

from sentence_transformers import SentenceTransformer

from app.rag.ask import run as ask_question
from app.rag.retrieval import (
    connect,
    database_name,
    embed,
    load_model,
    search_chunks,
)


CHUNKS_DIR = BACKEND_DIR / "data" / "output"
CHUNK_FILES = ["brd_chunks.json", "nfr_chunks.json"]
MODEL_NAME = "sentence-transformers/all-MiniLM-L6-v2"


def read_chunks() -> list[dict]:
    files = [CHUNKS_DIR / name for name in CHUNK_FILES]
    chunks = []
    for file in files:
        if not file.is_file():
            raise SystemExit(f"Chunk file not found: {file}")
        chunks.extend(json.loads(file.read_text(encoding="utf-8")))

    if not chunks:
        raise SystemExit(f"No chunks found in {CHUNKS_DIR}")

    return chunks


def embed_chunk(model: SentenceTransformer, chunk: dict) -> list[float]:
    text = f"{chunk['heading']}\n\n{chunk['text']}"
    return embed(model, text, label=chunk["chunk_id"])


def upload(model: SentenceTransformer) -> None:
    rows = []
    for chunk in read_chunks():
        print(f"Embedding {chunk['chunk_id']}")
        rows.append({**chunk, "embedding": embed_chunk(model, chunk)})

    with connect() as driver:
        records, _, _ = driver.execute_query(
            """
            UNWIND $rows AS row
            MERGE (c:NexusChunk {
                source: row.source,
                chunk_id: row.chunk_id
            })
            SET c.heading = row.heading,
                c.text = row.text,
                c.embedding = row.embedding,
                c.embedding_model = $model
            RETURN count(c) AS stored
            """,
            rows=rows,
            model=MODEL_NAME,
            database_=database_name(),
        )
    print(f"Stored {records[0]['stored']} chunks in Neo4j")


def search(model: SentenceTransformer, question: str) -> None:
    with connect() as driver:
        chunks = search_chunks(driver, model, question)

    if not chunks:
        print("No indexed chunks found.")
        return

    for chunk in chunks:
        print(
            f"\n{chunk['score']:.3f} | {chunk['source']} | "
            f"{chunk['chunk_id']} | {chunk['heading']}"
        )
        print(chunk["text"])


def main() -> None:
    parser = argparse.ArgumentParser(description="Embed, search and ask NEXUS BRD/NFR chunks.")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("preview", help="Embed the first chunk and print a sample.")
    commands.add_parser("upload", help="Embed all chunks and store them in Neo4j.")
    search_cmd = commands.add_parser("search", help="Find passages relevant to a question.")
    search_cmd.add_argument("question")
    ask_cmd = commands.add_parser("ask", help="Answer a question from retrieved passages with Claude.")
    ask_cmd.add_argument("question")
    ask_cmd.add_argument("--dry-run", action="store_true", help="Show retrieved passages and prompt without calling Claude.")
    args = parser.parse_args()

    if args.command == "ask":
        ask_question(args.question, dry_run=args.dry_run)
        return

    model = load_model()

    if args.command == "preview":
        chunk = read_chunks()[8]  # Preview the eighth chunk
        vector = embed_chunk(model, chunk)

        print(f"Chunk: {chunk['chunk_id']} | {chunk['heading']}")
        print(f"Dimensions: {len(vector)}")
        print(f"First five values: {vector[:5]}")
    elif args.command == "upload":
        upload(model)
    else:
        search(model, args.question)


if __name__ == "__main__":
    main()

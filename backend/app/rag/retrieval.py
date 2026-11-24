import os
from pathlib import Path

from dotenv import load_dotenv
from neo4j import Driver, GraphDatabase
from sentence_transformers import SentenceTransformer


BACKEND_DIR = Path(__file__).resolve().parents[2]
MODEL_DIR = BACKEND_DIR / "models" / "all-MiniLM-L6-v2"


def load_model() -> SentenceTransformer:
    return SentenceTransformer(str(MODEL_DIR), device="cpu", local_files_only=True)


def embed(model: SentenceTransformer, text: str, label: str) -> list[float]:
    token_ids = model.tokenizer(text, truncation=False)["input_ids"]
    limit = model.max_seq_length
    print(f"{label} has {len(token_ids)} tokens (limit: {limit})")

    if len(token_ids) > limit:
        raise SystemExit(
            f"{label} has {len(token_ids)} tokens, but the model limit is {limit}. "
            "Shorten the input before embedding it."
        )

    return model.encode(text).tolist()


def database_name() -> str:
    return os.getenv("NEO4J_DATABASE", "neo4j")


def connect() -> Driver:
    load_dotenv(BACKEND_DIR / ".env")
    auth = (os.environ["NEO4J_USERNAME"], os.environ["NEO4J_PASSWORD"])
    return GraphDatabase.driver(os.environ["NEO4J_URI"], auth=auth)


def fetch_chunks(driver: Driver, source: str) -> list[dict]:
    """Return every stored chunk of one source document, in document order."""
    records, _, _ = driver.execute_query(
        """
        MATCH (c:NexusChunk {source: $source})
        RETURN c.chunk_id AS chunk_id, c.source AS source,
               c.heading AS heading, c.text AS text
        ORDER BY c.chunk_id
        """,
        source=source,
        database_=database_name(),
    )
    return [record.data() for record in records]


def search_chunks(
    driver: Driver, model: SentenceTransformer, question: str, limit: int = 10
) -> list[dict]:
    vector = embed(model, question, label="Search question")
    records, _, _ = driver.execute_query(
        """
        CYPHER 25
        MATCH (c:NexusChunk)
        SEARCH c IN (
            VECTOR INDEX nexus_brd_embeddings
            FOR $embedding
            LIMIT $limit
        ) SCORE AS score
        RETURN c.chunk_id AS chunk_id, c.source AS source,
               c.heading AS heading, c.text AS text, score
        ORDER BY score DESC
        """,
        embedding=vector,
        limit=limit,
        database_=database_name(),
    )
    return [record.data() for record in records]

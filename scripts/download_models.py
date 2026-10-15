"""Script to download and pre-warm HuggingFace embedding models into HF_HOME cache volume.

Run during Docker image builds or setup: python scripts/download_models.py
Can be skipped if EMBEDDING_PROVIDER=fake or if network downloads are blocked.
"""

import os
import sys

MODEL_NAME = os.getenv("EMBEDDING_MODEL", "BAAI/bge-small-en-v1.5")


def download_model() -> None:
    """Pre-warm sentence-transformers model into cache."""
    print(f"Pre-warming embedding model '{MODEL_NAME}' into HF_HOME='{os.getenv('HF_HOME', '~/.cache/huggingface')}'...")
    try:
        from sentence_transformers import SentenceTransformer  # type: ignore[import-untyped]
        from transformers import AutoTokenizer  # type: ignore[import-untyped]

        AutoTokenizer.from_pretrained(MODEL_NAME)
        SentenceTransformer(MODEL_NAME, device="cpu")
        print(f"Successfully downloaded and verified '{MODEL_NAME}'.")
    except Exception as err:
        print(f"WARNING: Could not download model '{MODEL_NAME}': {err}")
        print("Note: In environments with blocked downloads, set EMBEDDING_PROVIDER=fake to run without downloading.")
        sys.exit(0)


if __name__ == "__main__":
    download_model()

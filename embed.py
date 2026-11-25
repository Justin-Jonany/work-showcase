"""Static model2vec embeddings shared by ingest (write) and the ranker (query)."""
import numpy as np

MODEL_NAME = "minishlab/potion-base-8M"

_model = None


def _get_model():
    global _model
    if _model is None:
        from model2vec import StaticModel
        _model = StaticModel.from_pretrained(MODEL_NAME)
    return _model


def embed(texts: list[str]) -> np.ndarray:
    """Embed texts as unit-length float32 rows, shape (len(texts), dim).

    Each row is scaled to length 1 so that a plain dot product between two rows
    equals their cosine similarity; the ranker relies on this.
    """
    vecs = np.asarray(_get_model().encode(list(texts)), dtype=np.float32)
    if vecs.ndim == 1:
        vecs = vecs[None, :]
    norms = np.linalg.norm(vecs, axis=1, keepdims=True)
    # An all-OOV text encodes to a zero vector; leave it zero instead of dividing by 0.
    norms[norms == 0] = 1.0
    return vecs / norms


def to_blob(vec) -> bytes:
    """Serialize a vector to raw bytes for a SQLite BLOB column.

    Little-endian float32 is pinned explicitly so the bytes mean the same thing
    on any machine. No shape or dtype header is stored, so the caller must save
    the dimension alongside the blob for from_blob to check against.
    """
    return np.asarray(vec, dtype="<f4").tobytes()


def from_blob(blob: bytes, dim: int) -> np.ndarray:
    """Decode a blob written by to_blob back into a float32 vector.

    Raw bytes carry no length or type, so a blob from another model, a
    truncated write or a float64 vector would decode silently into garbage;
    the dim check turns that into an error. astype copies out of the read-only
    frombuffer view and converts to native byte order.
    """
    vec = np.frombuffer(blob, dtype="<f4")
    if vec.shape[0] != dim:
        raise ValueError(f"blob has {vec.shape[0]} floats, expected {dim}")
    return vec.astype(np.float32)

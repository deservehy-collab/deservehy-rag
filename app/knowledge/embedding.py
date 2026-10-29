import logging
import os
import threading
from pathlib import Path
from typing import Protocol

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DIMENSION = 1024


class Embedder(Protocol):
    def encode(self, texts: list[str]) -> list[list[float]]: ...


class BgeM3Embedder:
    """BGE-M3 dense 向量：懒加载模型，首次调用时才占显存。"""

    def __init__(self, model_name: str, *, device: str = "cuda", batch_size: int = 16):
        self.model_name = model_name
        self.device = device
        self.batch_size = batch_size
        self._model = None
        self._lock = threading.Lock()

    def local_model_path(self) -> Path | None:
        path = Path(self.model_name)
        if not path.is_absolute():
            path = PROJECT_ROOT / path
        return path if path.exists() else None

    def _load(self):
        if self._model is not None:
            return self._model
        with self._lock:
            if self._model is not None:
                return self._model
            os.environ.setdefault("HF_HOME", str(PROJECT_ROOT / ".cache" / "huggingface"))
            model_path = self.local_model_path() or Path(self.model_name)
            try:
                import torch
                from sentence_transformers import SentenceTransformer
            except ImportError as exc:
                raise RuntimeError("缺少 sentence-transformers 或 torch，无法加载嵌入模型。") from exc
            device = self.device
            if device.startswith("cuda") and not torch.cuda.is_available():
                logger.warning("CUDA 不可用，嵌入模型回退到 CPU。")
                device = "cpu"
            self._model = SentenceTransformer(str(model_path), device=device)
            return self._model

    def warmup(self) -> None:
        self.encode(["预热"])

    def encode(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        model = self._load()
        with self._lock:
            vectors = model.encode(
                texts,
                batch_size=self.batch_size,
                normalize_embeddings=True,
                convert_to_numpy=True,
                show_progress_bar=False,
            )
        result = vectors.tolist()
        if any(len(vector) != DEFAULT_DIMENSION for vector in result):
            raise ValueError(f"嵌入维度不是 {DEFAULT_DIMENSION}，请确认使用 BGE-M3 模型。")
        return result

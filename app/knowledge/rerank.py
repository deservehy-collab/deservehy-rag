import logging
import threading
from pathlib import Path

logger = logging.getLogger(__name__)
PROJECT_ROOT = Path(__file__).resolve().parents[2]


class RerankerUnavailable(RuntimeError):
    pass


class BgeReranker:
    def __init__(self, model_name: str, *, device: str = "cuda", batch_size: int = 8):
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
            path = self.local_model_path()
            if path is None and not Path(self.model_name).exists():
                raise RerankerUnavailable(
                    "重排模型不存在。请下载 bge-reranker-v2-m3 到 .cache/models/bge-reranker-v2-m3。"
                )
            try:
                import torch
                from sentence_transformers import CrossEncoder
            except ImportError as exc:
                raise RerankerUnavailable("缺少 sentence-transformers 或 torch，无法加载重排模型。") from exc
            device = self.device
            if device.startswith("cuda") and not torch.cuda.is_available():
                logger.warning("CUDA 不可用，重排模型回退到 CPU。")
                device = "cpu"
            try:
                model = CrossEncoder(str(path or self.model_name), device=device)
                if device == "cuda":
                    model.model.half()
            except Exception as exc:
                raise RerankerUnavailable(f"重排模型加载失败：{exc}") from exc
            self.device = device
            self._model = model
            return model

    def warmup(self) -> None:
        if self.local_model_path() is not None:
            self.score("预热", ["预热"])

    def score(self, query: str, passages: list[str]) -> list[float]:
        if not passages:
            return []
        import torch

        try:
            model = self._load()
            pairs = [(query, passage) for passage in passages]
            # bge-reranker-v2-m3 的 num_labels=1，predict 默认使用 torch.nn.Sigmoid。
            values = model.predict(pairs, batch_size=self.batch_size, show_progress_bar=False)
            return [float(value) for value in values]
        except torch.cuda.OutOfMemoryError:
            logger.warning("重排显存不足，回退 CPU。")
            return self._score_on_cpu(query, passages)

    def _score_on_cpu(self, query: str, passages: list[str]) -> list[float]:
        self._model = None
        self.device = "cpu"
        try:
            import torch

            torch.cuda.empty_cache()
            model = self._load()
            values = model.predict(
                [(query, passage) for passage in passages], batch_size=4, show_progress_bar=False
            )
            return [float(value) for value in values]
        except Exception as exc:
            raise RerankerUnavailable(f"重排失败：{exc}") from exc

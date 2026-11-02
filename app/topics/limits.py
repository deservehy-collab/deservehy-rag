"""批量推理的单次预算；worker 与旁路推理服务共用同一口径。"""

from __future__ import annotations

# 与 POST /classify 的单次上限保持一致。
MAX_BATCH = 64
MAX_CHARS = 20000

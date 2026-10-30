import logging
from pathlib import Path

FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"


def configure_file_logging(path: str) -> None:
    """把根日志接到文件上；`:memory:` 或空值表示不落盘（测试用）。重复调用不叠加 handler。"""
    if not path or path == ":memory:":
        return
    target = Path(path).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    if any(isinstance(handler, logging.FileHandler)
           and Path(handler.baseFilename).resolve() == target for handler in root.handlers):
        return
    handler = logging.FileHandler(target, encoding="utf-8")
    handler.setLevel(logging.INFO)
    handler.setFormatter(logging.Formatter(FORMAT))
    root.addHandler(handler)
    # 根 logger 默认 WARNING，INFO 记录会被级别先挡掉，文件里就什么都看不到。
    if root.level == logging.NOTSET or root.level > logging.INFO:
        root.setLevel(logging.INFO)

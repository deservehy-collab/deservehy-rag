import re
import sqlite3

import httpx
from openai import APITimeoutError, RateLimitError
from sqlalchemy.exc import SQLAlchemyError

from app.knowledge.quality import CitationGuard

MAX_TOOL_CALLS = 5
DB_FAILURE_MESSAGE = "会话存储暂时不可用，请稍后重试。"
LEAK_MARKERS = ("DSML", "<｜", "<|", "｜DSML")
HOLD_SUFFIXES = ("DSM", "DS", "D", "<｜", "<", "｜")
FALLBACK_TOOL = "抱歉，刚才的查询结果没能整理成回答，请再问一次，或转人工客服。"
FALLBACK_PLAIN = "抱歉，刚才没能生成有效回答，请再问一次。"
BUDGET_TEXT = "这段对话需要的处理步骤太多，我先停在这里。你可以补充更具体的信息，或转人工客服继续。"
ACTION_LABELS = {"human": "转人工", "ticket": "建工单"}
ACTION_PATTERN = re.compile(r"\[\[actions:([A-Za-z]+(?:,[A-Za-z]+)*)\]\]")
ACTION_PREFIX = "[[actions:"


class ReplyGuard:
    """拦截上游漏进正文的 DeepSeek DSML 工具标记，流式转发前先扣住疑似标记的尾巴。"""

    def __init__(self) -> None:
        self.pending = ""
        self.clean = ""
        self.blocked = False

    def feed(self, text: str) -> str:
        if self.blocked:
            return ""
        self.pending += text
        cut = self._cut()
        if cut != -1:
            self.blocked = True
            safe, self.pending = self.pending[:cut], ""
        else:
            hold = 0
            for suffix in HOLD_SUFFIXES:
                if self.pending.endswith(suffix):
                    hold = len(suffix)
                    break
            if len(self.pending) <= hold:
                return ""
            if hold:
                safe, self.pending = self.pending[:-hold], self.pending[-hold:]
            else:
                safe, self.pending = self.pending, ""
        self.clean += safe
        return safe

    def flush(self) -> str:
        if not self.blocked:
            self.clean += self.pending
            self.pending = ""
        return self.clean

    def flush_tail(self) -> str:
        """只交出还没发送的尾巴；clean 已经通过 feed 逐段发出去了。"""
        if self.blocked:
            self.pending = ""
            return ""
        tail, self.pending = self.pending, ""
        self.clean += tail
        return tail

    def _cut(self) -> int:
        cut = -1
        for marker in LEAK_MARKERS:
            index = self.pending.find(marker)
            if index != -1 and (cut == -1 or index < cut):
                cut = index
        return cut


class ActionGuard:
    """流式剥离行尾 [[actions:...]] 操作标记，半截标记先扣住，引用角标不受影响。"""

    def __init__(self) -> None:
        self.pending = ""
        self.actions: list[str] = []

    def feed(self, text: str) -> str:
        self.pending += text
        output = []
        while self.pending:
            start = self.pending.find("[[")
            if start == -1:
                if self.pending.endswith("["):
                    output.append(self.pending[:-1])
                    self.pending = "["
                else:
                    output.append(self.pending)
                    self.pending = ""
                break
            output.append(self.pending[:start])
            self.pending = self.pending[start:]
            match = ACTION_PATTERN.match(self.pending)
            if match:
                self._record(match.group(1))
                self.pending = self.pending[match.end():]
                continue
            if self._partial():
                break
            output.append("[")
            self.pending = self.pending[1:]
        return "".join(output)

    def flush(self) -> str:
        remainder, self.pending = self.pending, ""
        if not remainder:
            return ""
        if remainder.startswith("[[") and (
            ACTION_PREFIX.startswith(remainder)
            or (remainder.startswith(ACTION_PREFIX) and "]]" not in remainder)
        ):
            return ""
        return remainder

    def _partial(self) -> bool:
        return self.pending.startswith("[[") and (
            ACTION_PREFIX.startswith(self.pending)
            or (self.pending.startswith(ACTION_PREFIX) and "]]" not in self.pending)
        )

    def _record(self, raw: str) -> None:
        for name in raw.lower().split(","):
            if name and name not in self.actions:
                self.actions.append(name)


class AgentStream:
    """模型正文依次过 DSML 防泄漏、引用角标、操作标记三道流式关卡。"""

    def __init__(self) -> None:
        self.reply = ReplyGuard()
        self.citations: CitationGuard | None = None
        self.actions = ActionGuard()
        self.text = ""

    def enable_citations(self, valid: set[int]) -> None:
        """空证据集等于不开角标闸门，沿用 service 旧逻辑 `CitationGuard(valid) if valid else None`。"""
        if not valid:
            return
        if self.citations is None:
            self.citations = CitationGuard(set(valid))
        else:
            self.citations.valid |= set(valid)

    def feed(self, chunk: str) -> str:
        safe = self.reply.feed(chunk)
        if not safe:
            return ""
        if self.citations is not None:
            safe = self.citations.feed(safe)
            if not safe:
                return ""
        safe = self.actions.feed(safe)
        if safe:
            self.text += safe
        return safe

    def finish(self) -> str:
        parts: list[str] = []
        tail = self.reply.flush_tail()
        if tail:
            if self.citations is not None:
                tail = self.citations.feed(tail)
            if tail:
                tail = self.actions.feed(tail)
                if tail:
                    parts.append(tail)
        if self.citations is not None:
            rest = self.citations.flush()
            if rest:
                rest = self.actions.feed(rest)
                if rest:
                    parts.append(rest)
        rest = self.actions.flush()
        if rest:
            parts.append(rest)
        output = "".join(parts)
        self.text += output
        return output

    @property
    def blocked(self) -> bool:
        return self.reply.blocked


class ModelFailure(Exception):
    def __init__(self, code: str, message: str, status_code: int = 502):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code

    def payload(self) -> dict:
        return {"code": self.code, "message": self.message}


def model_failure(exc: Exception) -> ModelFailure:
    if isinstance(exc, (TimeoutError, APITimeoutError, httpx.TimeoutException)):
        return ModelFailure("upstream_timeout", "模型响应超时，请稍后重试。", 504)
    if isinstance(exc, (SQLAlchemyError, sqlite3.Error)):
        return ModelFailure("database_unavailable", DB_FAILURE_MESSAGE, 503)
    if isinstance(exc, RateLimitError):
        return ModelFailure("upstream_rate_limited", "模型服务额度不足或请求过于频繁，请检查上游额度后重试。")
    if isinstance(exc, ValueError):
        return ModelFailure("invalid_model_output", "模型没有返回符合要求的结果，请重试或检查模型的 JSON 输出能力。")
    return ModelFailure("upstream_error", "模型服务暂时不可用，请检查上游配置后重试。")

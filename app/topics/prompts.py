"""生成、独立预标、改写、错别字清洗四组 Prompt。"""

from __future__ import annotations

import json
import re

from app.topics.taxonomy import BOUNDARIES, DESCRIPTIONS, LABELS


def taxonomy_block() -> str:
    return "\n".join(f"- {label}：{DESCRIPTIONS[label]}" for label in LABELS)


def boundary_block() -> str:
    return "\n".join(f"- {rule}" for rule in BOUNDARIES)


GENERATE_SYSTEM = (
    "你是电商客服语料工程师。只输出一个 JSON 对象，不要解释，不要 Markdown 代码块。"
    "问题必须是顾客真实会说出口的中文口语，8~40 字，不得包含真实姓名、电话、地址、订单号。"
)


def generate_prompt(labels: list[str], count: int) -> str:
    base = (
        f"{GENERATE_SYSTEM}\n\n主题术语表：\n{taxonomy_block()}\n\n"
        f"近邻规则：\n{boundary_block()}\n\n"
        f"本批只写同时表达这些诉求的问题：{'、'.join(labels)}。\n"
        f"共 {count} 条，每条只表达以上诉求，不要额外添加其他主题，条与条之间不得重复。\n"
    )
    if len(labels) > 1:
        base += (
            "这是多标签批次：每一句都必须同时、明确地写到上述两个诉求，"
            "缺一个就算不合格；不要用一句只写一个诉求再加一句只写另一个诉求的方式凑数。\n"
            "合格示例（主题为「运费」「物流」）：『这个订单还要补运费吗，快递什么时候能到？』\n"
            "不合格示例：『快递什么时候能到？』（只写了物流）。\n"
        )
    return base + "输出格式：{\"items\":[{\"question\":\"...\"}]}"


def prelabel_prompt(items: list[tuple[str, str]]) -> str:
    payload = json.dumps([{"id": item_id, "text": text} for item_id, text in items],
                         ensure_ascii=False)
    return (
        "你是电商客服主题标注员。按术语表给每条问题打多标签：字面提到几个诉求就打几个，"
        "一个不多一个不少；只按字面，不做意图推断。没有具体诉求、纯单号或闲聊打「其他」。\n\n"
        f"术语表：\n{taxonomy_block()}\n\n近邻规则：\n{boundary_block()}\n\n"
        f"待标注：{payload}\n\n"
        "输出格式：{\"items\":[{\"id\":\"...\",\"labels\":[\"...\"]}]}；labels 只能取术语表里的名字。"
    )


def paraphrase_prompt(items: list[tuple[str, str]]) -> str:
    payload = json.dumps([{"id": item_id, "text": text} for item_id, text in items],
                         ensure_ascii=False)
    return (
        "把下面的客服问题换一种口语说法，诉求完全不变，不改标签含义，8~40 字，每条只输出一个改写。\n"
        f"原句：{payload}\n"
        "输出格式：{\"items\":[{\"id\":\"...\",\"text\":\"...\"}]}"
    )


def typo_prompt(items: list[tuple[str, str]]) -> str:
    payload = json.dumps([{"id": item_id, "text": text} for item_id, text in items],
                         ensure_ascii=False)
    return (
        "只改下面的错别字和明显病句，不改变诉求，不扩写；没有错就原样返回。\n"
        f"原句：{payload}\n"
        "输出格式：{\"items\":[{\"id\":\"...\",\"text\":\"...\"}]}"
    )


def parse_payload(content: str) -> dict:
    text = content.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", text)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("模型输出里没有 JSON 对象")
    payload = json.loads(text[start:end + 1])
    if not isinstance(payload, dict):
        raise ValueError("JSON 顶层必须是对象")
    return payload

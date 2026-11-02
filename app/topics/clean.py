import re
import unicodedata

_PHONE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
_ID_CARD = re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)")
_ORDER = re.compile(
    r"(快递单号|运单号|订单号|订单|(?<!订)单号)(?!号)(\s*[:：]?\s*)([A-Za-z0-9-]{4,})")
# 边界只认 ASCII 字母数字：`\w` 含 CJK，会让「银行卡号是6222...」这类贴写逃逸；
# 也不含 `-`/`_`，因为 NFKC 会把全角 `－`/`＿` 折成它们，`卡号－6222...` 会漏。
# PII 安全优先于「把紧贴中文的 6 位以上数量误伤」的极小代价。
_BARE_NUMBER = re.compile(r"(?<![A-Za-z0-9])\d{6,}(?![A-Za-z0-9])")
_ZERO_WIDTH = re.compile(r"[\u200b-\u200f\u2028-\u202f\u2060\ufeff]")
_PUNCT = str.maketrans({"！": "!", "？": "?", "，": ",", "。": ".", "：": ":", "；": ";",
                       "（": "(", "）": ")"})


def _mask_order(match: re.Match) -> str:
    prefix, separator = match.group(1), match.group(2)
    # 「订单 1001」贴在一起写；「订单号 998877」「运单号：SF...」保留原有分隔。
    return f"{prefix}{'' if prefix == '订单' else separator}[单号]"


def mask_pii(text: str) -> str:
    # 先把全角数字/字母/标点折成 ASCII，否则 `１３８１２３４５６７８` 全不中，
    # 而后续 clean_text 的 NFKC 会把它还原成明文手机号。
    text = unicodedata.normalize("NFKC", text)
    text = _ID_CARD.sub("[证件号]", text)
    text = _PHONE.sub("[手机号]", text)
    text = _ORDER.sub(_mask_order, text)
    return _BARE_NUMBER.sub("[单号]", text)


def clean_text(text: str | None) -> str:
    text = unicodedata.normalize("NFKC", text or "")
    text = _ZERO_WIDTH.sub("", text)
    text = "".join(ch if unicodedata.category(ch)[0] != "C" else " " for ch in text)
    text = text.translate(_PUNCT)
    text = re.sub(r"\s+", " ", text).strip()
    return text.strip(" \"'“”‘’")

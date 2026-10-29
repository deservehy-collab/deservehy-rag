SYNONYM_GROUPS = (
    ("邮费", "运费", "快递费", "配送费"),
    ("退款", "退钱", "返钱"),
    ("退货", "退回", "退掉"),
    ("换货", "换一个", "调换"),
    ("保修", "质保", "包修"),
    ("发票", "开票", "票据"),
    ("到账", "入账", "到账时间"),
    ("包邮", "免运费", "不收运费"),
    ("时效", "几天到", "多久到"),
    ("型号", "规格", "是哪款"),
    ("适配", "兼容", "通用"),
    ("配件", "零件", "杯盖", "蒸笼"),
)


def expand_for_bm25(query: str, *, limit: int = 6) -> str:
    text = query.strip()
    if not text:
        return ""
    additions: list[str] = []
    for group in SYNONYM_GROUPS:
        if any(term in text for term in group):
            additions.extend(term for term in group if term not in text and term not in additions)
    return " ".join([text, *additions[:limit]])

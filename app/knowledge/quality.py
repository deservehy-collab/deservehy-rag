import re

REFUSAL_TEXT = (
    "这个问题我在店铺知识库里没有找到可靠依据，先不猜测回答。"
    "你可以换个说法，或者告诉我更多信息，我帮你转人工核实。"
)
CITATION_PATTERN = re.compile(r"\[(\d{1,2})\]")


def order_evidence(items):
    return list(items[::2]) + list(reversed(items[1::2]))


def number_evidence(items):
    numbered = []
    for index, item in enumerate(order_evidence(items), 1):
        numbered.append({**item, "n": index})
    return numbered


def strip_invalid_citations(text, valid):
    return CITATION_PATTERN.sub(lambda m: m.group(0) if int(m.group(1)) in valid else "", text)


class CitationGuard:
    def __init__(self, valid):
        self.valid = set(valid)
        self.pending = ""
        self.dropped = 0

    def feed(self, text):
        self.pending += text
        output = []
        while self.pending:
            start = self.pending.find("[")
            if start == -1:
                output.append(self.pending)
                self.pending = ""
                break
            output.append(self.pending[:start])
            self.pending = self.pending[start:]
            match = re.match(r"\[(\d{1,2})\]", self.pending)
            if match:
                if int(match.group(1)) in self.valid:
                    output.append(match.group(0))
                else:
                    self.dropped += 1
                self.pending = self.pending[match.end():]
                continue
            if re.fullmatch(r"\[\d{0,2}", self.pending):
                break
            output.append("[")
            self.pending = self.pending[1:]
        return "".join(output)

    def flush(self):
        remainder = "" if re.fullmatch(r"\[\d{0,2}", self.pending) else self.pending
        if self.pending and not remainder:
            self.dropped += 1
        self.pending = ""
        return remainder

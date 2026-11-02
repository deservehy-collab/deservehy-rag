import contextlib
import json
import math
import sys
import types

import pytest

from app.topics.model import ModelNotReady, TopicModel, apply_thresholds
from app.topics.taxonomy import LABELS, OTHER


def probs(**hits):
    return [hits.get(label, 0.1) for label in LABELS]


def test_apply_thresholds_multi_fallback_and_boundary():
    assert apply_thresholds(probs(退换货=0.9, 尺码=0.8), [0.5] * 17) == ["退换货", "尺码"]
    assert apply_thresholds(probs(), [0.5] * 17) == [OTHER]
    # 等于阈值算命中
    assert apply_thresholds(probs(物流=0.5), [0.5] * 17) == ["物流"]
    assert apply_thresholds(probs(物流=0.4999), [0.5] * 17) == [OTHER]


def test_apply_thresholds_rejects_wrong_length():
    with pytest.raises(ValueError):
        apply_thresholds([0.1], [0.5])
    with pytest.raises(ValueError):
        apply_thresholds([0.1] * 17, [0.5])


def test_is_ready_requires_labels_thresholds_and_weights(tmp_path):
    assert TopicModel.is_ready(tmp_path) is False
    (tmp_path / "labels.json").write_text("[]", encoding="utf-8")
    (tmp_path / "thresholds.json").write_text("{}", encoding="utf-8")
    assert TopicModel.is_ready(tmp_path) is False
    (tmp_path / "model.safetensors").write_text("", encoding="utf-8")
    assert TopicModel.is_ready(tmp_path) is True
    (tmp_path / "model.safetensors").unlink()
    (tmp_path / "pytorch_model.bin").write_text("", encoding="utf-8")
    assert TopicModel.is_ready(tmp_path) is True


def test_missing_artifacts_raise_model_not_ready(tmp_path):
    with pytest.raises(ModelNotReady):
        TopicModel(tmp_path)


# ---------------------------------------------------------------------------
# 假 torch / tokenizer / model：不装 torch、不下载权重、不碰 GPU
# ---------------------------------------------------------------------------


class FakeTensor:
    """够用的张量替身：只实现 predict 用到的 .to() / .cpu() / .tolist()。"""

    def __init__(self, data):
        self.data = data
        self.devices = []

    def to(self, device):
        self.devices.append(device)
        return self

    def cpu(self):
        return self

    def tolist(self):
        return self.data


class FakeTorch:
    """只提供 no_grad 与真 sigmoid，用来验证四舍五入链路。"""

    def __init__(self):
        self.no_grad_entries = 0
        self.sigmoid_calls = 0

    def as_module(self):
        module = types.ModuleType("torch")
        module.no_grad = self.no_grad
        module.sigmoid = self.sigmoid
        return module

    @contextlib.contextmanager
    def no_grad(self):
        self.no_grad_entries += 1
        yield

    def sigmoid(self, logits):
        self.sigmoid_calls += 1
        return FakeTensor([[1.0 / (1.0 + math.exp(-value)) for value in row]
                           for row in logits])


class FakeTokenizer:
    def __init__(self):
        self.batches = []
        self.kwargs = []
        self.encoded = []

    def __call__(self, texts, **kwargs):
        self.batches.append(list(texts))
        self.kwargs.append(kwargs)
        input_ids = FakeTensor([[index] for index, _ in enumerate(texts)])
        self.encoded.append(input_ids)
        return {"input_ids": input_ids, "attention_mask": FakeTensor([[1] * len(texts)])}


class FakeModel:
    """按顺序吐出预置 logits，并记录 .to() / .eval() 调用。"""

    def __init__(self, rows=None):
        self.rows = [list(row) for row in (rows or [])]
        self.calls = []
        self.devices = []
        self.eval_calls = 0

    def __call__(self, **kwargs):
        count = len(kwargs["input_ids"].data)
        rows = self.rows[:count]
        del self.rows[:count]
        self.calls.append(kwargs)
        return types.SimpleNamespace(logits=rows)

    def to(self, device):
        self.devices.append(device)
        return self

    def eval(self):
        self.eval_calls += 1
        return self


class FakeBackend:
    """把假 torch / transformers 塞进 sys.modules，并记录构造期调用。"""

    def __init__(self, monkeypatch, *, rows=None, tokenizer_exc=None, model_exc=None):
        self.torch = FakeTorch()
        self.tokenizer = FakeTokenizer()
        self.model = FakeModel(rows)
        self.tokenizer_calls = []
        self.model_calls = []
        self._tokenizer_exc = tokenizer_exc
        self._model_exc = model_exc
        backend = self

        class AutoTokenizer:
            @staticmethod
            def from_pretrained(path, **kwargs):
                backend.tokenizer_calls.append((path, kwargs))
                if backend._tokenizer_exc is not None:
                    raise backend._tokenizer_exc
                return backend.tokenizer

        class AutoModelForSequenceClassification:
            @staticmethod
            def from_pretrained(path, **kwargs):
                backend.model_calls.append((path, kwargs))
                if backend._model_exc is not None:
                    raise backend._model_exc
                return backend.model

        transformers = types.ModuleType("transformers")
        transformers.AutoTokenizer = AutoTokenizer
        transformers.AutoModelForSequenceClassification = AutoModelForSequenceClassification
        monkeypatch.setitem(sys.modules, "transformers", transformers)
        monkeypatch.setitem(sys.modules, "torch", self.torch.as_module())


THRESHOLDS = [0.5] * len(LABELS)


def logits_row(**hits):
    """未点名的类别给 -5，sigmoid 后约 0.0067，低于默认阈值。"""
    return [hits.get(label, -5.0) for label in LABELS]


def hand_built_model(rows, *, thresholds=None, device="cpu", max_len=128):
    """绕过 __init__（不导 torch、不读盘），只注入 predict 需要的字段。"""
    model = TopicModel.__new__(TopicModel)
    fake_torch = FakeTorch()
    model._torch = fake_torch.as_module()
    model.tokenizer = FakeTokenizer()
    model.model = FakeModel(rows)
    model.device = device
    model.max_len = max_len
    model.thresholds = list(thresholds if thresholds is not None else THRESHOLDS)
    return model, fake_torch


def write_artifacts(root, *, labels_json=None, thresholds_json=None):
    (root / "labels.json").write_text(
        labels_json if labels_json is not None
        else json.dumps(list(LABELS), ensure_ascii=False),
        encoding="utf-8")
    (root / "thresholds.json").write_text(
        thresholds_json if thresholds_json is not None
        else json.dumps({label: 0.5 for label in LABELS}, ensure_ascii=False),
        encoding="utf-8")
    (root / "model.safetensors").write_text("", encoding="utf-8")
    return root


# ---------------------------------------------------------------------------
# predict：条数校验、16 条分批、sigmoid、round(...,4)、顺序与键
# ---------------------------------------------------------------------------


def test_predict_rejects_empty_and_oversized_inputs():
    model, fake_torch = hand_built_model([logits_row()])

    with pytest.raises(ValueError, match="1~64"):
        model.predict([])
    with pytest.raises(ValueError, match="1~64"):
        model.predict(["x"] * 65)

    assert model.tokenizer.batches == []
    assert fake_torch.no_grad_entries == 0


def test_predict_splits_16_plus_1_and_keeps_row_alignment():
    texts = [f"t{index}" for index in range(17)]
    rows = [logits_row(**{LABELS[index]: 2.0}) for index in range(17)]
    model, fake_torch = hand_built_model(rows)

    predictions = model.predict(texts)

    assert [len(batch) for batch in model.tokenizer.batches] == [16, 1]
    assert model.tokenizer.batches[0] == texts[:16]
    assert model.tokenizer.batches[1] == texts[16:]
    assert model.tokenizer.kwargs[0]["max_length"] == 128
    assert fake_torch.no_grad_entries == 1
    assert fake_torch.sigmoid_calls == 2
    assert len(predictions) == 17
    for index, prediction in enumerate(predictions):
        assert prediction.labels == [LABELS[index]]


def test_predict_rounds_scores_to_four_decimals_and_orders_labels():
    model, _ = hand_built_model([logits_row(尺码=2.0, 退换货=1.0)],
                                device="cuda:1", max_len=96)

    [prediction] = model.predict(["快递一直不动，尺码也不对"])

    # order_labels 按 17 类术语表顺序返回，而不是概率高低
    assert prediction.labels == ["退换货", "尺码"]
    assert list(prediction.scores) == list(LABELS)
    assert len(prediction.scores) == 17
    assert set(prediction.scores) == set(LABELS)
    assert prediction.scores["退换货"] == 0.7311
    assert prediction.scores["尺码"] == 0.8808
    assert prediction.scores["物流"] == 0.0067
    assert all(isinstance(score, float) for score in prediction.scores.values())
    assert model.tokenizer.kwargs[0] == {
        "padding": True, "truncation": True,
        "max_length": 96, "return_tensors": "pt"}
    assert model.tokenizer.encoded[0].devices == ["cuda:1"]


def test_predict_falls_back_to_other_when_nothing_hits():
    model, _ = hand_built_model([logits_row()])

    [prediction] = model.predict(["谢谢"])

    assert prediction.labels == [OTHER]


# ---------------------------------------------------------------------------
# 构造：local_files_only、eval()、labels/thresholds 校验与失败契约
# ---------------------------------------------------------------------------


def test_constructor_loads_locally_runs_eval_and_predicts(tmp_path, monkeypatch):
    backend = FakeBackend(monkeypatch, rows=[logits_row(退换货=2.0), logits_row()])
    write_artifacts(tmp_path)

    model = TopicModel(tmp_path, device="cuda:1", max_len=64)

    assert backend.tokenizer_calls == [(tmp_path, {"local_files_only": True})]
    assert backend.model_calls == [(tmp_path, {"local_files_only": True})]
    assert backend.model.devices == ["cuda:1"]
    assert backend.model.eval_calls == 1
    assert model.thresholds == [0.5] * len(LABELS)
    assert model.max_len == 64

    predictions = model.predict(["我要退货", "谢谢"])

    assert [prediction.labels for prediction in predictions] == [["退换货"], [OTHER]]
    assert backend.tokenizer.kwargs[0]["max_length"] == 64
    assert backend.tokenizer.encoded[0].devices == ["cuda:1"]


def test_constructor_rejects_labels_not_matching_taxonomy(tmp_path, monkeypatch):
    backend = FakeBackend(monkeypatch)
    write_artifacts(tmp_path,
                    labels_json=json.dumps(list(reversed(LABELS)), ensure_ascii=False))

    with pytest.raises(ModelNotReady, match="术语表"):
        TopicModel(tmp_path)

    assert backend.tokenizer_calls == []


def test_constructor_wraps_broken_labels_json(tmp_path, monkeypatch):
    FakeBackend(monkeypatch)
    write_artifacts(tmp_path, labels_json="{不是 JSON")

    with pytest.raises(ModelNotReady) as excinfo:
        TopicModel(tmp_path)

    assert isinstance(excinfo.value.__cause__, json.JSONDecodeError)


@pytest.mark.parametrize(
    "thresholds_json",
    [
        "{不是 JSON",
        json.dumps({label: 0.5 for label in LABELS if label != "会员积分"},
                   ensure_ascii=False),
        json.dumps({label: "高" for label in LABELS}, ensure_ascii=False),
    ],
    ids=["broken-json", "missing-label", "non-numeric"],
)
def test_constructor_wraps_bad_thresholds(tmp_path, monkeypatch, thresholds_json):
    FakeBackend(monkeypatch)
    write_artifacts(tmp_path, thresholds_json=thresholds_json)

    with pytest.raises(ModelNotReady) as excinfo:
        TopicModel(tmp_path)

    assert isinstance(excinfo.value.__cause__, (ValueError, KeyError))


@pytest.mark.parametrize("value", [1.5, -0.1, float("nan")], ids=["high", "low", "nan"])
def test_constructor_rejects_out_of_range_thresholds(tmp_path, monkeypatch, value):
    FakeBackend(monkeypatch)
    write_artifacts(tmp_path,
                    thresholds_json=json.dumps({label: value for label in LABELS}))

    with pytest.raises(ModelNotReady, match="0~1"):
        TopicModel(tmp_path)


@pytest.mark.parametrize(
    ("tokenizer_exc", "model_exc"),
    [(OSError("tokenizer 半拷贝"), None), (None, OSError("权重文件损坏"))],
    ids=["tokenizer", "model"],
)
def test_constructor_wraps_weight_load_errors(tmp_path, monkeypatch,
                                              tokenizer_exc, model_exc):
    backend = FakeBackend(monkeypatch, tokenizer_exc=tokenizer_exc,
                          model_exc=model_exc)
    write_artifacts(tmp_path)

    with pytest.raises(ModelNotReady) as excinfo:
        TopicModel(tmp_path)

    assert isinstance(excinfo.value.__cause__, OSError)
    assert backend.model.eval_calls == 0

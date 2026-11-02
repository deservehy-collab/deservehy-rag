"""ch10 全参微调 RoBERTa-wwm-ext 多标签主题分类器（手写 PyTorch 循环）。"""

from __future__ import annotations

import argparse
import copy
import json
import random
import sys
import time
from datetime import datetime
from pathlib import Path

import numpy as np
import torch
from sklearn.metrics import f1_score
from torch.utils.data import DataLoader, Dataset
from transformers import (
    AutoTokenizer, BertForSequenceClassification, get_linear_schedule_with_warmup,
)

from app.topics.dataset import read_jsonl
from app.topics.taxonomy import LABELS


class TopicDataset(Dataset):
    def __init__(self, samples, tokenizer, max_len: int):
        encoded = tokenizer([item.text for item in samples], padding="max_length",
                            truncation=True, max_length=max_len, return_tensors="pt")
        self.input_ids = encoded["input_ids"]
        self.attention_mask = encoded["attention_mask"]
        self.labels = torch.tensor([
            [1.0 if label in item.labels else 0.0 for label in LABELS] for item in samples
        ])

    def __len__(self):
        return len(self.labels)

    def __getitem__(self, index):
        return {"input_ids": self.input_ids[index],
                "attention_mask": self.attention_mask[index],
                "labels": self.labels[index]}


def evaluate(model, loader, device):
    model.eval()
    truths, probs = [], []
    with torch.no_grad():
        for batch in loader:
            batch = {key: value.to(device) for key, value in batch.items()}
            logits = model(input_ids=batch["input_ids"],
                           attention_mask=batch["attention_mask"]).logits
            probs.append(torch.sigmoid(logits).cpu().numpy())
            truths.append(batch["labels"].cpu().numpy())
    return np.concatenate(truths), np.concatenate(probs)


def macro_f1(truths, probs, threshold: float = 0.5) -> float:
    return float(f1_score(truths, (probs >= threshold).astype(int),
                          average="macro", zero_division=0))


def tune_thresholds(truths, probs) -> list[float]:
    thresholds = []
    for index in range(len(LABELS)):
        if truths[:, index].sum() == 0:
            thresholds.append(0.5)
            continue
        best_threshold, best_score = 0.5, -1.0
        for step in range(4, 17):  # 0.20 ~ 0.80
            threshold = round(step * 0.05, 2)
            score = f1_score(truths[:, index],
                             (probs[:, index] >= threshold).astype(int), zero_division=0)
            if score > best_score:
                best_threshold, best_score = threshold, score
        thresholds.append(best_threshold)
    return thresholds


def write_report(report_dir: Path, history: list[dict], best_epoch: int,
                 thresholds: list[float], meta: dict) -> Path:
    report_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = report_dir / f"ch10-train-{stamp}.md"
    lines = ["# ch10 训练报告", "", f"- 最佳轮次：{best_epoch}",
             f"- 最佳验证 macro-F1（阈值 0.5）：{max(row['val_macro_f1'] for row in history):.4f}",
             f"- 设备：{meta['device']}，显存峰值：{meta['peak_gb']:.2f} GB",
             f"- 耗时：{meta['seconds']:.1f}s", "",
             "| 轮次 | train loss | val macro-F1 |", "|---|---|---|"]
    lines += [f"| {row['epoch']} | {row['train_loss']:.4f} | {row['val_macro_f1']:.4f} |"
              for row in history]
    lines += ["", "## 每类阈值（验证集调优）", "", "| 类目 | 阈值 |", "|---|---|"]
    lines += [f"| {label} | {threshold} |" for label, threshold in zip(LABELS, thresholds)]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def train(args) -> int:
    started = time.monotonic()
    torch.manual_seed(args.seed)
    random.seed(args.seed)
    np.random.seed(args.seed)
    if args.device == "cuda" and not torch.cuda.is_available():
        print("--device cuda 不可用，回落到 cpu。", file=sys.stderr)
    device = "cpu" if args.device == "cpu" or not torch.cuda.is_available() else "cuda"
    model_dir = Path(args.model_dir)
    tokenizer = AutoTokenizer.from_pretrained(model_dir, local_files_only=True)
    train_samples = read_jsonl(args.train)
    val_samples = read_jsonl(args.val)
    if args.smoke:
        train_samples, val_samples = train_samples[:40], val_samples[:10]
        args.epochs, args.max_len, args.batch_size = 1, 64, 4
    train_set = TopicDataset(train_samples, tokenizer, args.max_len)
    val_set = TopicDataset(val_samples, tokenizer, args.max_len)
    train_loader = DataLoader(train_set, batch_size=args.batch_size, shuffle=True)
    val_loader = DataLoader(val_set, batch_size=args.batch_size)
    model = BertForSequenceClassification.from_pretrained(
        model_dir, num_labels=len(LABELS), problem_type="multi_label_classification",
        local_files_only=True).to(device)
    optimizer = torch.optim.AdamW(model.parameters(), lr=args.lr,
                                  weight_decay=args.weight_decay)
    total_steps = max(1, len(train_loader) * args.epochs // args.grad_accum)
    scheduler = get_linear_schedule_with_warmup(
        optimizer, int(total_steps * args.warmup_ratio), total_steps)
    best_state, best_f1, best_epoch, stale = None, -1.0, 0, 0
    history = []
    peak_gb = 0.0
    if device == "cuda":
        torch.cuda.reset_peak_memory_stats()
    for epoch in range(1, args.epochs + 1):
        model.train()
        total_loss = 0.0
        optimizer.zero_grad()
        for step, batch in enumerate(train_loader, 1):
            batch = {key: value.to(device) for key, value in batch.items()}
            try:
                with torch.autocast(device_type="cuda", dtype=torch.bfloat16,
                                    enabled=device == "cuda"):
                    loss = model(**batch).loss
                (loss / args.grad_accum).backward()
            except torch.cuda.OutOfMemoryError:
                print("显存不足：把 --batch-size 降到 4、--max-len 降到 96 后重跑。",
                      file=sys.stderr)
                return 2
            total_loss += loss.item()
            if step % args.grad_accum == 0:
                torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
                optimizer.step()
                scheduler.step()
                optimizer.zero_grad()
        if len(train_loader) % args.grad_accum:
            torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
            optimizer.step()
            scheduler.step()
            optimizer.zero_grad()
        truths, probs = evaluate(model, val_loader, device)
        score = macro_f1(truths, probs)
        history.append({"epoch": epoch, "train_loss": total_loss / len(train_loader),
                        "val_macro_f1": score})
        print(f"epoch {epoch}: train_loss={total_loss / len(train_loader):.4f} "
              f"val_macro_f1={score:.4f}")
        if score > best_f1:
            best_f1, best_epoch, stale = score, epoch, 0
            best_state = copy.deepcopy(model.state_dict())
        else:
            stale += 1
            if stale >= args.patience:
                print(f"验证集 {args.patience} 轮未提升，早停。")
                break
    model.load_state_dict(best_state)
    truths, probs = evaluate(model, val_loader, device)
    thresholds = tune_thresholds(truths, probs)
    if device == "cuda":
        peak_gb = torch.cuda.max_memory_allocated() / 1024 ** 3
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    model.save_pretrained(out)
    tokenizer.save_pretrained(out)
    (out / "labels.json").write_text(json.dumps(list(LABELS), ensure_ascii=False),
                                     encoding="utf-8")
    (out / "thresholds.json").write_text(
        json.dumps(dict(zip(LABELS, thresholds)), ensure_ascii=False), encoding="utf-8")
    meta = {"device": device, "peak_gb": peak_gb, "best_epoch": best_epoch,
            "history": history, "thresholds": thresholds,
            "seconds": time.monotonic() - started,
            "params": vars(args), "smoke": args.smoke}
    (out / "train_meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2,
                                                    default=str), encoding="utf-8")
    if not args.smoke:
        path = write_report(Path(args.report_dir), history, best_epoch, thresholds, meta)
        print(f"训练报告：{path}")
    print(f"模型已保存：{out}")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="ch10 全参微调主题分类器")
    parser.add_argument("--train", type=Path, default=Path(".cache/topics/splits/train.jsonl"))
    parser.add_argument("--val", type=Path, default=Path(".cache/topics/splits/val.jsonl"))
    parser.add_argument("--model-dir", type=Path,
                        default=Path(".cache/models/chinese-roberta-wwm-ext"))
    parser.add_argument("--out", type=Path, default=Path(".cache/models/topic-classifier"))
    parser.add_argument("--report-dir", type=Path, default=Path("evals/reports"))
    parser.add_argument("--epochs", type=int, default=8)
    parser.add_argument("--batch-size", type=int, default=8)
    parser.add_argument("--grad-accum", type=int, default=2)
    parser.add_argument("--max-len", type=int, default=128)
    parser.add_argument("--lr", type=float, default=2e-5)
    parser.add_argument("--weight-decay", type=float, default=0.01)
    parser.add_argument("--warmup-ratio", type=float, default=0.1)
    parser.add_argument("--patience", type=int, default=2)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--device", choices=("auto", "cpu", "cuda"), default="auto")
    parser.add_argument("--smoke", action="store_true")
    args = parser.parse_args(argv)
    if args.smoke and args.out == Path(".cache/models/topic-classifier"):
        args.out = Path(".cache/models/topic-classifier-smoke")
    return train(args)


if __name__ == "__main__":
    raise SystemExit(main())

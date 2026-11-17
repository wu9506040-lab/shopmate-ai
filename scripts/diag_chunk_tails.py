#!/usr/bin/env python3
"""诊断：找出「尾块过短且无表头」的切片，确认影响范围。

用法: backend/.venv/Scripts/python.exe scripts/diag_chunk_tails.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from doc_parsers import parse_file, semantic_chunks  # noqa: E402

KB_DIR = Path(__file__).parent.parent / "docs" / "fashion_kb"
MAX_CHARS = 600


def looks_like_table(text: str) -> bool:
    """是否像 markdown 表格：至少两行含 | 分隔。"""
    lines = [l for l in text.splitlines() if l.strip()]
    pipes = sum(1 for l in lines if l.count("|") >= 2)
    return pipes >= 2


def has_header(text: str) -> bool:
    """是否含表头分隔行（|---|）或首行像表头。"""
    if "---" in text.replace(" ", ""):
        return True
    first = next((l for l in text.splitlines() if l.strip()), "")
    return first.count("|") >= 2 and not first.lstrip().startswith("| 3XL")


def main() -> None:
    total_docs = 0
    total_sections = 0
    short_tails = []
    for f in sorted(KB_DIR.glob("*.md")):
        try:
            _cat, items = parse_file(f)
        except Exception as e:  # noqa: BLE001
            print(f"[跳过] {f.name}: {e}")
            continue
        total_docs += 1
        for it in items:
            text = it.get("text") or ""
            if len(text) <= MAX_CHARS:
                continue
            total_sections += 1
            chunks = semantic_chunks(text, max_chars=MAX_CHARS)
            if len(chunks) < 2:
                continue
            tail = chunks[-1]
            if len(tail) < MAX_CHARS * 0.3:
                short_tails.append({
                    "file": f.name,
                    "source": it.get("source"),
                    "n_chunks": len(chunks),
                    "lens": [len(c) for c in chunks],
                    "tail_len": len(tail),
                    "tail_is_table": looks_like_table(tail),
                    "tail_has_header": has_header(tail),
                    "tail_head": tail[:90].replace("\n", "⏎"),
                })
    print(f"扫描 {total_docs} 个文档，{total_sections} 个超长 section\n")
    if not short_tails:
        print("没有过短尾块。")
        return
    print(f"发现 {len(short_tails)} 处过短尾块（< {int(MAX_CHARS * 0.3)} 字）：\n")
    for s in short_tails:
        flag = "【表格无表头·该合】" if (s["tail_is_table"] and not s["tail_has_header"]) else ""
        print(f"{flag}{s['file']} / {s['source']}")
        print(f"   块长 {s['lens']} 尾块 {s['tail_len']} 字")
        print(f"   尾块开头: {s['tail_head']}\n")


if __name__ == "__main__":
    main()

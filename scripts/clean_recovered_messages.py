#!/usr/bin/env python3
"""Create readable exports from recover_qq_bak.py output."""

from __future__ import annotations

import argparse
import csv
import re
import shutil
from collections import defaultdict
from pathlib import Path


CONTROL_WHITELIST = {"\n", "\t"}
FONT_NAMES = ("宋体", "SimSun")
FONT_CODE_RE = re.compile(
    r"(?<![0-9A-Fa-f])"
    r"[ \t]*"
    r"[0-9A-Fa-f]{8}0086[0-9A-Fa-f]{2}"
    r"[\u4e00-\u9fffA-Za-z@]"
    r"[\u4e00-\u9fffA-Za-z0-9_ @().+-]{0,40}"
    r"(?=[ \t]*(?:$|\n))",
    re.MULTILINE,
)
IMAGE_REF_RE = re.compile(
    r"(?:"
    r"6\s+[^\u4e00-\u9fff\r\n]{0,320}?\.(?:gif|jpe?g|png|bmp)[A-Za-z0-9]*"
    r"|"
    r"[^\s\u4e00-\u9fff]{1,260}\.(?:gif|jpe?g|png|bmp)[A-Za-z0-9]*"
    r")",
    re.IGNORECASE,
)
IMAGE_FILENAME_RE = re.compile(
    r"[^\\/:*?\"<>|\s，。、“”‘’（）()]+?\.(?:gif|jpe?g|png|bmp)",
    re.IGNORECASE,
)


def strip_font_runs(text: str) -> str:
    """Remove old QQ rich-text font markers such as \\x13\\x150A...宋体."""
    marker = "\x13\x15"
    while marker in text:
        start = text.find(marker)
        end_candidates = []
        for font_name in FONT_NAMES:
            pos = text.find(font_name, start)
            if pos != -1:
                end_candidates.append(pos + len(font_name))

        if not end_candidates:
            text = text[:start] + text[start + len(marker) :]
            continue

        end = min(end_candidates)
        while end < len(text) and text[end] in "\r\n ":
            end += 1
        text = text[:start] + text[end:]

    return text


def clean_text(value: str) -> str:
    text = value.replace("\r\n", "\n").replace("\r", "\n")
    text = strip_font_runs(text)
    text = "".join(
        char for char in text if ord(char) >= 32 or char in CONTROL_WHITELIST
    )
    text = FONT_CODE_RE.sub("", text)
    text = IMAGE_REF_RE.sub("[图片表情]", text)
    text = IMAGE_FILENAME_RE.sub("[图片表情]", text)

    cleaned = []
    for char in text:
        if ord(char) >= 32 or char in CONTROL_WHITELIST:
            cleaned.append(char)
    text = "".join(cleaned)

    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text.strip()


def safe_name(name: str) -> str:
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)


def write_conversations(rows: list[dict[str, str]], output_dir: Path) -> None:
    if output_dir.exists():
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    grouped: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        grouped[(row["kind"], row["peer_id"])].append(row)

    for (kind, peer_id), conv_rows in sorted(grouped.items()):
        path = output_dir / f"{safe_name(kind)}_{safe_name(peer_id)}.txt"
        with path.open("w", encoding="utf-8", newline="\n") as handle:
            for row in conv_rows:
                handle.write(f"[{row['when']}] {row['sender']}\n")
                content = row["content"]
                if content:
                    handle.write(content)
                else:
                    handle.write("[空消息或仅含旧格式控制数据]")
                handle.write("\n\n")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Clean old QQ rich-text markers from recovered messages."
    )
    parser.add_argument(
        "--dir",
        type=Path,
        default=Path("recovered_messages"),
        help="Directory produced by recover_qq_bak.py",
    )
    args = parser.parse_args()

    base_dir = args.dir
    source_csv = base_dir / "messages.csv"
    clean_csv = base_dir / "messages_clean.csv"
    clean_conversations = base_dir / "conversations_clean"

    rows: list[dict[str, str]] = []
    with source_csv.open("r", encoding="utf-8-sig", newline="") as source:
        reader = csv.DictReader(source)
        if not reader.fieldnames or "content" not in reader.fieldnames:
            raise SystemExit(f"{source_csv} does not look like a recovered messages CSV")

        fieldnames = reader.fieldnames
        for row in reader:
            row = dict(row)
            row["content"] = clean_text(row.get("content", ""))
            row["sender"] = clean_text(row.get("sender", ""))
            rows.append(row)

    with clean_csv.open("w", encoding="utf-8-sig", newline="") as target:
        writer = csv.DictWriter(target, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    write_conversations(rows, clean_conversations)

    print(f"cleaned_messages={len(rows)}")
    print(f"clean_csv={clean_csv}")
    print(f"clean_conversations={clean_conversations}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

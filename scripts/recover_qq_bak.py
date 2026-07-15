#!/usr/bin/env python3
"""
Recover old Tencent QQ MsgEx-style OLE backups.

The 2010 .bak file is an OLE/Compound File container. Each conversation has
an Index.msj stream containing little-endian offsets into Data.msj; each
Data.msj slice is encrypted with QQ's TEA/CBC variant.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import re
import shutil
import struct
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from itertools import islice
from pathlib import Path
from typing import Iterable

import olefile


MASK32 = 0xFFFFFFFF


@dataclass(frozen=True)
class StreamPair:
    kind: str
    peer_id: str
    index_path: tuple[str, ...]
    data_path: tuple[str, ...]
    message_count: int
    data_size: int


@dataclass
class ParsedMessage:
    kind: str
    peer_id: str
    index: int
    timestamp: int
    when: str
    sender: str
    content: str
    raw_len: int
    plain_len: int


def tea_decipher_block(block: bytes, key: bytes) -> bytes:
    """Decrypt one 8-byte QQ TEA block."""
    if len(block) != 8 or len(key) != 16:
        raise ValueError("TEA block must be 8 bytes and key must be 16 bytes")

    y, z = struct.unpack(">II", block)
    a, b, c, d = struct.unpack(">IIII", key)
    delta = 0x9E3779B9
    total = 0xE3779B90
    for _ in range(16):
        z = (z - (((y << 4) + c) ^ (y + total) ^ ((y >> 5) + d))) & MASK32
        y = (y - (((z << 4) + a) ^ (z + total) ^ ((z >> 5) + b))) & MASK32
        total = (total - delta) & MASK32
    return struct.pack(">II", y, z)


def qq_tea_decrypt(ciphertext: bytes, key: bytes) -> bytes:
    """Decrypt QQ's TEA/CBC variant, ported from the old RedQ QQCrypt code."""
    if len(ciphertext) < 16 or len(ciphertext) % 8 != 0 or len(key) != 16:
        return b""

    pre_plain = bytearray(tea_decipher_block(ciphertext[:8], key))
    pos = pre_plain[0] & 7
    count = len(ciphertext) - pos - 10
    if count <= 0:
        return b""

    out = bytearray()
    crypt = 8
    pre_crypt = 0
    context_start = 8
    pos += 1
    padding = 1
    m = bytearray(8)

    def decrypt8() -> bool:
        nonlocal pre_plain, crypt, context_start, pos
        pos = 0
        for j in range(8):
            if context_start + j > len(ciphertext) - 1:
                return True
            pre_plain[j] ^= ciphertext[crypt + j]
        try:
            pre_plain = bytearray(tea_decipher_block(bytes(pre_plain), key))
        except Exception:
            return False
        context_start += 8
        crypt += 8
        pos = 0
        return True

    while padding < 3:
        if pos < 8:
            pos += 1
            padding += 1
        elif pos == 8:
            m[:] = ciphertext[:8]
            if not decrypt8():
                return b""

    guard = 0
    while count:
        guard += 1
        if guard > len(ciphertext) * 4:
            return b""
        if pos < 8:
            out.append(m[pre_crypt + pos] ^ pre_plain[pos])
            pos += 1
            count -= 1
        elif pos == 8:
            m = bytearray(ciphertext)
            pre_crypt = crypt - 8
            if not decrypt8():
                return b""

    for _ in range(7):
        if pos < 8:
            if (m[pre_crypt + pos] ^ pre_plain[pos]) != 0:
                return b""
            pos += 1
        elif pos == 8:
            m = bytearray(ciphertext)
            pre_crypt = crypt
            if not decrypt8():
                return b""

    return bytes(out)


def list_pairs(ole: olefile.OleFileIO) -> list[StreamPair]:
    streams = {
        tuple(component.lower() for component in path): tuple(path)
        for path in ole.listdir(streams=True)
    }
    pairs: list[StreamPair] = []
    for normalized_path, index_path in sorted(streams.items()):
        if len(normalized_path) >= 4 and normalized_path[-1] == "index.msj":
            normalized_data_path = normalized_path[:-1] + ("data.msj",)
            data_path = streams.get(normalized_data_path)
            if data_path is None:
                continue
            kind = index_path[-3]
            peer_id = index_path[-2]
            index_size = ole.get_size(index_path)
            data_size = ole.get_size(data_path)
            pairs.append(
                StreamPair(
                    kind=kind,
                    peer_id=peer_id,
                    index_path=index_path,
                    data_path=data_path,
                    message_count=index_size // 4,
                    data_size=data_size,
                )
            )
    return pairs


def read_offsets(ole: olefile.OleFileIO, pair: StreamPair) -> list[int]:
    data = ole.openstream(pair.index_path).read()
    offsets = list(struct.unpack("<" + "I" * (len(data) // 4), data))
    offsets.append(pair.data_size)
    return offsets


def iter_cipher_slices(ole: olefile.OleFileIO, pair: StreamPair) -> Iterable[tuple[int, bytes]]:
    offsets = read_offsets(ole, pair)
    data = ole.openstream(pair.data_path).read()
    for i, (start, end) in enumerate(zip(offsets, offsets[1:])):
        if 0 <= start < end <= len(data):
            yield i, data[start:end]


def read_i32_le(buf: bytes, pos: int) -> tuple[int, int] | None:
    if pos + 4 > len(buf):
        return None
    return struct.unpack_from("<i", buf, pos)[0], pos + 4


def decode_gbk(data: bytes) -> str:
    return data.decode("gb18030", errors="replace")


def parse_message(kind: str, peer_id: str, index: int, raw: bytes, plain: bytes) -> ParsedMessage | None:
    if len(plain) < 16:
        return None
    item = read_i32_le(plain, 0)
    if item is None:
        return None
    timestamp, pos = item
    if not (946684800 <= timestamp <= 1893456000):  # 2000-01-01 .. 2030-01-01
        return None

    if kind == "C2CMsg":
        pos += 1
    elif kind == "GroupMsg":
        pos += 8
    elif kind == "SysMsg":
        pos += 4
    elif kind == "MobileMsg":
        pos += 1
    elif kind == "TempSessionMsg":
        pos += 9
        item = read_i32_le(plain, pos)
        if item is None:
            return None
        group_len, pos = item
        if group_len < 0 or group_len > len(plain) - pos:
            return None
        pos += group_len

    item = read_i32_le(plain, pos)
    if item is None:
        return None
    sender_len, pos = item
    if sender_len < 0 or sender_len > len(plain) - pos:
        return None
    sender = decode_gbk(plain[pos : pos + sender_len])
    pos += sender_len

    item = read_i32_le(plain, pos)
    if item is None:
        return None
    content_len, pos = item
    if content_len < 0 or content_len > len(plain) - pos:
        return None
    content = decode_gbk(plain[pos : pos + content_len]).replace("\\n", "\n")
    when = datetime.fromtimestamp(timestamp, tz=timezone.utc).astimezone().isoformat(sep=" ", timespec="seconds")
    return ParsedMessage(kind, peer_id, index, timestamp, when, sender, content, len(raw), len(plain))


def printable_score(text: str) -> float:
    if not text:
        return 0.0
    good = 0
    for ch in text:
        o = ord(ch)
        if ch in "\r\n\t" or 0x20 <= o <= 0x7E or "\u4e00" <= ch <= "\u9fff":
            good += 1
    return good / max(len(text), 1)


def score_plain(kind: str, peer_id: str, idx: int, raw: bytes, plain: bytes) -> tuple[float, ParsedMessage | None]:
    msg = parse_message(kind, peer_id, idx, raw, plain)
    if msg is None:
        return 0.0, None
    text_score = printable_score(msg.sender + msg.content)
    length_score = 1.0 if msg.sender and msg.content else 0.3
    return 4.0 + text_score + length_score + min(len(msg.content), 80) / 80.0, msg


def md5_ascii(s: str) -> bytes:
    return hashlib.md5(s.encode("ascii")).digest()


def md5_utf16le(s: str) -> bytes:
    return hashlib.md5(s.encode("utf-16le")).digest()


def candidate_keys(pairs: list[StreamPair], extra_ids: Iterable[str]) -> list[tuple[str, bytes]]:
    ids = {p.peer_id for p in pairs if re.fullmatch(r"\d{5,12}", p.peer_id)}
    ids.update(x for x in extra_ids if re.fullmatch(r"\d{5,12}", x))
    # Some MsgEx backups use "402" as a version/root component. Include it as
    # a low-cost compatibility candidate, not as a user identifier.
    ids.add("402")

    candidates: list[tuple[str, bytes]] = []
    seen: set[bytes] = set()
    for value in sorted(ids, key=lambda x: (len(x), x)):
        for label, key in (
            (f"md5-ascii:{value}", md5_ascii(value)),
            (f"md5-utf16le:{value}", md5_utf16le(value)),
        ):
            if key not in seen:
                seen.add(key)
                candidates.append((label, key))
    return candidates


def build_sample_pairs(pairs: list[StreamPair], limit: int) -> list[StreamPair]:
    ranked = sorted(pairs, key=lambda p: (p.message_count, p.data_size), reverse=True)
    return ranked[:limit]


def test_key(ole: olefile.OleFileIO, pairs: list[StreamPair], key: bytes, per_pair: int) -> tuple[float, list[ParsedMessage]]:
    score = 0.0
    found: list[ParsedMessage] = []
    for pair in pairs:
        for idx, raw in islice(iter_cipher_slices(ole, pair), per_pair):
            plain = qq_tea_decrypt(raw, key)
            item_score, msg = score_plain(pair.kind, pair.peer_id, idx, raw, plain)
            score += item_score
            if msg:
                found.append(msg)
    return score, found


def choose_key(ole: olefile.OleFileIO, pairs: list[StreamPair], extra_ids: Iterable[str], sample_pairs: int, per_pair: int):
    sample = build_sample_pairs(pairs, sample_pairs)
    results = []
    for label, key in candidate_keys(pairs, extra_ids):
        score, found = test_key(ole, sample, key, per_pair)
        if score > 0 or found:
            results.append((score, label, key, len(found), found[:5]))
    results.sort(reverse=True, key=lambda x: x[0])
    return results


def export_all(
    ole: olefile.OleFileIO,
    pairs: list[StreamPair],
    key: bytes,
    progress_every: int = 0,
) -> tuple[list[ParsedMessage], dict[str, int]]:
    messages: list[ParsedMessage] = []
    stats = Counter()
    for pair in pairs:
        for idx, raw in iter_cipher_slices(ole, pair):
            stats["slices"] += 1
            if progress_every and stats["slices"] % progress_every == 0:
                print(
                    f"progress slices={stats['slices']} parsed={stats['parsed']} "
                    f"decrypt_empty={stats['decrypt_empty']} parse_failed={stats['parse_failed']}",
                    flush=True,
                )
            plain = qq_tea_decrypt(raw, key)
            if not plain:
                stats["decrypt_empty"] += 1
                continue
            stats["decrypted"] += 1
            msg = parse_message(pair.kind, pair.peer_id, idx, raw, plain)
            if msg is None:
                stats["parse_failed"] += 1
                continue
            stats["parsed"] += 1
            messages.append(msg)
    messages.sort(key=lambda m: (m.timestamp, m.kind, m.peer_id, m.index))
    return messages, dict(stats)


def write_outputs(out_dir: Path, messages: list[ParsedMessage], meta: dict) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "summary.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    with (out_dir / "messages.csv").open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=["kind", "peer_id", "index", "when", "sender", "content", "raw_len", "plain_len"],
        )
        writer.writeheader()
        for m in messages:
            writer.writerow(
                {
                    "kind": m.kind,
                    "peer_id": m.peer_id,
                    "index": m.index,
                    "when": m.when,
                    "sender": m.sender,
                    "content": m.content,
                    "raw_len": m.raw_len,
                    "plain_len": m.plain_len,
                }
            )

    grouped: dict[tuple[str, str], list[ParsedMessage]] = defaultdict(list)
    for m in messages:
        grouped[(m.kind, m.peer_id)].append(m)
    safe_re = re.compile(r"[^0-9A-Za-z_.-]+")
    conv_dir = out_dir / "conversations"
    if conv_dir.exists():
        shutil.rmtree(conv_dir)
    conv_dir.mkdir()
    for (kind, peer_id), items in grouped.items():
        safe = safe_re.sub("_", f"{kind}_{peer_id}")
        with (conv_dir / f"{safe}.txt").open("w", encoding="utf-8") as f:
            for m in items:
                f.write(f"[{m.when}] {m.sender}\n{m.content}\n\n")


def entropy(data: bytes) -> float:
    if not data:
        return 0.0
    counts = Counter(data)
    n = len(data)
    return -sum((c / n) * math.log2(c / n) for c in counts.values())


def command_inspect(args: argparse.Namespace) -> int:
    with olefile.OleFileIO(str(args.bak)) as ole:
        pairs = list_pairs(ole)
        total_messages = sum(p.message_count for p in pairs)
        print(f"pairs={len(pairs)} messages(indexed)={total_messages}")
        for pair in sorted(pairs, key=lambda p: p.data_size, reverse=True)[: args.top]:
            head = ole.openstream(pair.data_path).read(256)
            print(
                f"{pair.kind}/{pair.peer_id}: messages={pair.message_count} "
                f"data={pair.data_size} entropy_head={entropy(head):.3f}"
            )
    return 0


def command_probe(args: argparse.Namespace) -> int:
    with olefile.OleFileIO(str(args.bak)) as ole:
        pairs = list_pairs(ole)
        results = choose_key(ole, pairs, args.id or [], args.sample_pairs, args.per_pair)
        if not results:
            print("No candidate key produced parseable messages.")
            return 2
        for score, label, key, count, examples in results[: args.top]:
            key_text = f"\t{key.hex()}" if args.show_key else ""
            print(f"{score:.3f}\t{count:4d}\t{label}{key_text}")
            for m in examples:
                snippet = m.content.replace("\n", " ")[:80]
                print(f"    {m.kind}/{m.peer_id} {m.when} {m.sender}: {snippet}")
    return 0


def parse_key_arg(key_arg: str | None, key_label: str | None) -> tuple[str, bytes] | None:
    if key_arg:
        key = bytes.fromhex(key_arg)
        if len(key) != 16:
            raise SystemExit("--key-hex must decode to exactly 16 bytes")
        return "key-hex", key
    if key_label:
        if key_label.startswith("md5-ascii:"):
            return key_label, md5_ascii(key_label.split(":", 1)[1])
        if key_label.startswith("md5-utf16le:"):
            return key_label, md5_utf16le(key_label.split(":", 1)[1])
        raise SystemExit("--key-label must look like md5-ascii:123456 or md5-utf16le:123456")
    return None


def command_export(args: argparse.Namespace) -> int:
    with olefile.OleFileIO(str(args.bak)) as ole:
        pairs = list_pairs(ole)
        selected = parse_key_arg(args.key_hex, args.key_label)
        if selected is None:
            results = choose_key(ole, pairs, args.id or [], args.sample_pairs, args.per_pair)
            if not results:
                print("No candidate key produced parseable messages; export aborted.")
                return 2
            _, label, key, count, _ = results[0]
        else:
            label, key = selected
            count = -1

        messages, stats = export_all(ole, pairs, key, args.progress_every)
        meta = {
            "backup_name": args.bak.name,
            "key_derivation": label.split(":", 1)[0],
            "probe_parse_count": count,
            "pairs": len(pairs),
            "indexed_messages": sum(p.message_count for p in pairs),
            "exported_messages": len(messages),
            "stats": stats,
            "by_kind": Counter(m.kind for m in messages),
        }
        if args.include_key_metadata:
            meta["key_label"] = label
            meta["key_hex"] = key.hex()
        write_outputs(args.out, messages, meta)
        if args.show_key:
            print(f"key={label} {key.hex()}")
        else:
            print(f"key_derivation={label.split(':', 1)[0]} (key hidden)")
        print(f"exported={len(messages)} out={args.out}")
        print(json.dumps(stats, ensure_ascii=False, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bak", type=Path, default=Path("backup.bak"))
    sub = parser.add_subparsers(dest="command", required=True)

    inspect_p = sub.add_parser("inspect")
    inspect_p.add_argument("--top", type=int, default=20)
    inspect_p.set_defaults(func=command_inspect)

    probe_p = sub.add_parser("probe")
    probe_p.add_argument("--id", action="append", help="extra QQ/self ID candidate")
    probe_p.add_argument("--sample-pairs", type=int, default=20)
    probe_p.add_argument("--per-pair", type=int, default=30)
    probe_p.add_argument("--top", type=int, default=20)
    probe_p.add_argument(
        "--show-key",
        action="store_true",
        help="include key hex values in probe output; keep the terminal log private",
    )
    probe_p.set_defaults(func=command_probe)

    export_p = sub.add_parser("export")
    export_p.add_argument("--id", action="append", help="extra QQ/self ID candidate")
    export_p.add_argument("--sample-pairs", type=int, default=20)
    export_p.add_argument("--per-pair", type=int, default=30)
    export_p.add_argument("--key-hex")
    export_p.add_argument("--key-label")
    export_p.add_argument("--out", type=Path, default=Path("recovered_messages"))
    export_p.add_argument("--progress-every", type=int, default=5000)
    export_p.add_argument(
        "--show-key",
        action="store_true",
        help="print the selected key label and hex value to the local terminal",
    )
    export_p.add_argument(
        "--include-key-metadata",
        action="store_true",
        help="store key label and hex in summary.json; avoid for shareable outputs",
    )
    export_p.set_defaults(func=command_export)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())

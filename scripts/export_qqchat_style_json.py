#!/usr/bin/env python3
"""Export recovered legacy QQ messages as QQChatExporter-style JSON."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import shutil
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SELF_UIN = ""
SELF_NAME = ""
SELF_UID = "u_legacy_self"
SELF_NAME_ALIASES: set[str] = set()
COLLECTION_NAME = "Recovered legacy QQ messages"


def configure_identity(
    self_uin: str,
    self_name: str,
    aliases: list[str],
    collection_name: str,
) -> None:
    global SELF_UIN, SELF_NAME, SELF_UID, SELF_NAME_ALIASES, COLLECTION_NAME
    SELF_UIN = self_uin.strip()
    SELF_NAME = self_name.strip()
    SELF_UID = f"u_legacy_{SELF_UIN}" if SELF_UIN else "u_legacy_self"
    SELF_NAME_ALIASES = {alias.strip() for alias in aliases if alias.strip()}
    if SELF_NAME:
        SELF_NAME_ALIASES.add(SELF_NAME)
    COLLECTION_NAME = collection_name.strip() or "Recovered legacy QQ messages"


def safe_name(value: str) -> str:
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value)


def legacy_uid(sender_name: str) -> str:
    if sender_name.isdigit():
        return f"u_legacy_{sender_name}"
    digest = hashlib.md5(sender_name.encode("utf-8")).hexdigest()[:16]
    return f"u_legacy_{digest}"


def sender_object(sender_name: str) -> dict[str, str]:
    name = sender_name or ""
    if name in SELF_NAME_ALIASES:
        return {
            "uid": SELF_UID,
            "uin": SELF_UIN,
            "name": name,
        }
    return {
        "uid": legacy_uid(name),
        "uin": name if name.isdigit() else "",
        "name": name,
    }


def parse_when(value: str) -> datetime:
    return datetime.fromisoformat(value)


def qq_time(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def iso_utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def conversation_type(kind: str) -> str:
    if kind == "GroupMsg":
        return "group"
    if kind == "C2CMsg":
        return "private"
    return kind


def chat_name(kind: str, peer_id: str) -> str:
    if kind == "GroupMsg":
        return f"GroupMsg_{peer_id}"
    if kind == "C2CMsg":
        return f"C2CMsg_{peer_id}"
    return f"{kind}_{peer_id}"


def make_content(text: str) -> dict[str, Any]:
    elements = []
    if text:
        elements.append({"type": "text", "data": {"text": text}})
    return {
        "text": text,
        "html": "",
        "elements": elements,
        "resources": [],
        "mentions": [],
    }


def message_id(kind: str, peer_id: str, index: str, timestamp_ms: int) -> str:
    seed = f"{kind}|{peer_id}|{index}|{timestamp_ms}"
    return "legacy_" + hashlib.md5(seed.encode("utf-8")).hexdigest()


def make_message(row: dict[str, str], include_chat: bool = False) -> dict[str, Any]:
    dt = parse_when(row["when"])
    timestamp_ms = int(dt.timestamp() * 1000)
    kind = row["kind"]
    peer_id = row["peer_id"]
    message = {
        "id": message_id(kind, peer_id, row["index"], timestamp_ms),
        "seq": row["index"],
        "timestamp": timestamp_ms,
        "time": qq_time(dt),
        "sender": sender_object(row["sender"]),
        "type": "text",
        "content": make_content(row["content"]),
        "recalled": False,
        "system": False,
    }
    if include_chat:
        message["chat"] = {
            "name": chat_name(kind, peer_id),
            "type": conversation_type(kind),
            "peerId": peer_id,
            "sourceKind": kind,
        }
    return message


def statistics(rows: list[dict[str, str]]) -> dict[str, Any]:
    if not rows:
        return {
            "totalMessages": 0,
            "timeRange": {"start": "", "end": "", "durationDays": 0},
            "messageTypes": {},
            "senders": [],
            "resources": {"total": 0, "byType": {}},
        }

    times = [parse_when(row["when"]) for row in rows]
    start = min(times)
    end = max(times)
    sender_counts = Counter(row["sender"] for row in rows)
    total = len(rows)

    senders = []
    for sender, count in sender_counts.most_common():
        sender_info = sender_object(sender)
        senders.append(
            {
                "uid": sender_info["uid"],
                "name": sender,
                "messageCount": count,
                "percentage": round(count * 100 / total, 2),
            }
        )

    return {
        "totalMessages": total,
        "timeRange": {
            "start": iso_utc(start),
            "end": iso_utc(end),
            "durationDays": (end.date() - start.date()).days,
        },
        "messageTypes": {"text": total},
        "senders": senders,
        "resources": {"total": 0, "byType": {}},
    }


def metadata() -> dict[str, str]:
    return {
        "name": "Legacy QQ recovery / QQChatExporter-style JSON",
        "version": "1.0",
        "source": "Recovered from old QQ MsgEx backup",
        "formatReference": "QQChatExporter V5 JSON structure",
    }


def export_options() -> dict[str, Any]:
    return {
        "includedFields": ["id", "timestamp", "sender", "content", "resources"],
        "filters": {},
        "options": {
            "includeResourceLinks": False,
            "includeSystemMessages": False,
            "timeFormat": "YYYY-MM-DD HH:mm:ss",
            "encoding": "utf-8",
        },
    }


def chat_info(kind: str, peer_id: str) -> dict[str, str]:
    return {
        "name": chat_name(kind, peer_id),
        "type": conversation_type(kind),
        "selfUid": SELF_UID,
        "selfUin": SELF_UIN,
        "selfName": SELF_NAME,
        "peerId": peer_id,
        "sourceKind": kind,
    }


def all_chat_info(total_conversations: int) -> dict[str, Any]:
    return {
        "name": COLLECTION_NAME,
        "type": "mixed",
        "selfUid": SELF_UID,
        "selfUin": SELF_UIN,
        "selfName": SELF_NAME,
        "conversationCount": total_conversations,
    }


def document(
    rows: list[dict[str, str]],
    info: dict[str, Any],
    include_chat: bool = False,
) -> dict[str, Any]:
    sorted_rows = sorted(
        rows,
        key=lambda row: (parse_when(row["when"]), row["kind"], row["peer_id"], int(row["index"])),
    )
    return {
        "metadata": metadata(),
        "chatInfo": info,
        "statistics": statistics(sorted_rows),
        "messages": [make_message(row, include_chat=include_chat) for row in sorted_rows],
        "exportOptions": export_options(),
    }


def write_json(path: Path, data: dict[str, Any]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, separators=(",", ":"))


def load_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        rows = [dict(row) for row in reader]
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Convert recovered QQ messages CSV to QQChatExporter-style JSON."
    )
    parser.add_argument(
        "--csv",
        type=Path,
        default=Path("recovered_messages/messages_clean.csv"),
        help="Clean recovered messages CSV",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("recovered_messages/qqchat_style_json"),
        help="Output directory",
    )
    parser.add_argument(
        "--self-uin",
        default="",
        help="owner QQ number for chatInfo/sender mapping; omit to keep blank",
    )
    parser.add_argument(
        "--self-name",
        default="",
        help="owner display name; omit to keep blank",
    )
    parser.add_argument(
        "--self-alias",
        action="append",
        default=[],
        help="additional owner display-name alias; repeat as needed",
    )
    parser.add_argument(
        "--collection-name",
        default="Recovered legacy QQ messages",
        help="display name for the combined JSON export",
    )
    args = parser.parse_args()
    configure_identity(
        args.self_uin,
        args.self_name,
        args.self_alias,
        args.collection_name,
    )

    rows = load_rows(args.csv)
    grouped: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        grouped[(row["kind"], row["peer_id"])].append(row)

    if args.out.exists():
        shutil.rmtree(args.out)
    by_conversation = args.out / "by_conversation"
    by_conversation.mkdir(parents=True, exist_ok=True)

    manifest_items = []
    for (kind, peer_id), conv_rows in sorted(grouped.items()):
        filename = f"{safe_name(kind)}_{safe_name(peer_id)}.json"
        path = by_conversation / filename
        write_json(path, document(conv_rows, chat_info(kind, peer_id)))
        manifest_items.append(
            {
                "file": f"by_conversation/{filename}",
                "kind": kind,
                "type": conversation_type(kind),
                "peerId": peer_id,
                "messages": len(conv_rows),
            }
        )

    all_path = args.out / "all_chats_qqchat_style.json"
    write_json(
        all_path,
        document(rows, all_chat_info(len(grouped)), include_chat=True),
    )

    manifest = {
        "metadata": metadata(),
        "sourceCsv": args.csv.name,
        "allChatsFile": "all_chats_qqchat_style.json",
        "conversationCount": len(grouped),
        "messageCount": len(rows),
        "conversations": manifest_items,
    }
    write_json(args.out / "manifest.json", manifest)

    print(f"messages={len(rows)}")
    print(f"conversations={len(grouped)}")
    print(f"out={args.out}")
    print(f"all={all_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

---
name: recover-legacy-qq-chat
description: Recover authorized legacy Tencent QQ MsgEx chat backups, especially .bak/OLE compound files containing Index.msj and Data.msj streams. Use when Codex needs to inspect, decrypt, restore, validate, clean, or convert old QQ chat archives; probe MD5-derived QQ TEA keys; parse C2C/group records; remove rich-text/font/image artifacts; or export CSV, TXT, and QQChatExporter-style JSON.
---

# Recover Legacy QQ Chat

Recover old QQ MsgEx-era backups with the bundled scripts. Treat the backup, account identifiers, derived keys, sender names, and recovered messages as sensitive data.

## Safety And Privacy

- Confirm that the user owns the backup or has explicit authorization to recover it.
- Work from a copy. Never modify the source `.bak`.
- Do not execute an unknown legacy reader `.exe`; parse the backup directly. Inspect executables statically only when format research is necessary.
- Keep `probe` output private because candidate labels may contain account identifiers.
- Do not publish `summary.json`, logs, screenshots, or message samples without reviewing them for personal information.
- Use placeholders such as `<BACKUP>`, `<SELF_UIN>`, `<OUTPUT>`, and `<SELF_NAME>` in documentation and shared commands.

## Dependencies

Use Python 3.10 or newer:

```powershell
python -m pip install -r scripts/requirements.txt
```

## Workflow

### 1. Identify The Container

Check the file signature before assuming the format. An OLE/Compound File normally starts with:

```text
D0 CF 11 E0 A1 B1 1A E1
```

Run:

```powershell
python scripts/recover_qq_bak.py --bak <BACKUP> inspect
```

Expect conversation pairs whose streams end in:

```text
<root>/<kind>/<peer>/Index.msj
<root>/<kind>/<peer>/Data.msj
```

Do not hardcode the root component or version number. Enumerate all streams and pair paths case-insensitively by their final names.

If the backup is not OLE or has no `Index.msj`/`Data.msj` pairs, stop and identify the actual format before adapting the parser.

### 2. Probe Authorized Key Candidates

For this MsgEx variant, the 16-byte QQ TEA key is often an MD5 digest of the backup owner's QQ number encoded as ASCII or UTF-16LE. This is a format heuristic, not a guarantee.

Supply only account numbers legitimately associated with the backup:

```powershell
python scripts/recover_qq_bak.py --bak <BACKUP> probe --id <SELF_UIN> --top 10
```

Repeat `--id` for additional authorized candidates. Do not brute-force broad numeric ranges.

Accept a candidate only when it decrypts multiple records across conversations with:

- plausible timestamps;
- valid length-prefixed sender/content fields;
- readable GB18030 text;
- consistent parse counts;
- no padding failures.

Do not accept a key based on one readable fragment. Use `--show-key` only in a private terminal.

### 3. Export Raw Recovered Messages

Use the highest-confidence label returned by `probe`:

```powershell
python scripts/recover_qq_bak.py --bak <BACKUP> export `
  --key-label md5-ascii:<SELF_UIN> `
  --out <OUTPUT>
```

Alternatively pass an already-known 16-byte key with `--key-hex`.

The exporter writes:

- `summary.json`;
- `messages.csv`;
- `conversations/*.txt`.

The default summary omits the key and stores only the backup filename, not its absolute path. Add `--include-key-metadata` only for a private, local recovery record.

### 4. Clean Legacy Formatting Artifacts

Run:

```powershell
python scripts/clean_recovered_messages.py --dir <OUTPUT>
```

This preserves the raw export and creates:

- `messages_clean.csv`;
- `conversations_clean/*.txt`.

The cleaner:

- removes C0 control characters except newline and tab;
- removes old rich-text runs such as `\x13\x15...`;
- removes `0086xx` font/style tails at line endings;
- replaces `.gif`, `.jpg`, `.jpeg`, `.png`, and `.bmp` references with `[图片表情]`;
- normalizes excessive whitespace.

Keep `messages.csv` as the evidentiary/raw decoded export.

### 5. Export QQChatExporter-Style JSON

Run:

```powershell
python scripts/export_qqchat_style_json.py `
  --csv <OUTPUT>/messages_clean.csv `
  --out <OUTPUT>/qqchat_style_json `
  --self-uin <SELF_UIN> `
  --self-name <SELF_NAME> `
  --self-alias <OLD_SELF_NAME>
```

Omit the identity arguments when producing a shareable or anonymized JSON structure.

The exporter writes:

- `all_chats_qqchat_style.json`;
- `by_conversation/*.json`;
- `manifest.json`.

Modern QQ fields unavailable in the old backup are represented conservatively: generated legacy UIDs, text-only message types, and empty resource arrays.

### 6. Validate The Recovery

Parse outputs with CSV/JSON libraries, not line counting or raw string splitting.

Check:

- `indexed_messages >= exported_messages`;
- `stats.decrypted >= stats.parsed`;
- a fully supported backup normally has equal indexed, decrypted, parsed, and exported counts;
- the sum of per-conversation JSON messages equals the CSV row count;
- every JSON file loads successfully;
- timestamps and conversation types are plausible;
- cleaned text contains no remaining `0086xx` font tails or image extensions intended for replacement.

Investigate mismatches before claiming a complete recovery. Unsupported message kinds, corrupt offsets, a wrong key, or a client-version layout difference may produce partial output.

## Format Notes

Use these invariants when adapting the scripts:

- Read `Index.msj` as little-endian unsigned 32-bit offsets.
- Append `len(Data.msj)` as the final sentinel offset.
- Slice only ranges satisfying `0 <= start < end <= data_size`.
- Decrypt each slice independently.
- Use 16-round TEA with big-endian 64-bit blocks and the QQ custom CBC/padding scheme.
- Reset the block position inside the QQ decrypt step and keep a loop guard so bad keys cannot hang probing.
- Decode legacy strings with GB18030 and replacement on undecodable bytes.
- Validate every signed length before slicing.

Known record-prefix adjustments after the little-endian timestamp:

| Kind | Prefix adjustment |
| --- | ---: |
| `C2CMsg` | `+1` byte |
| `GroupMsg` | `+8` bytes |
| `SysMsg` | `+4` bytes |
| `MobileMsg` | `+1` byte |
| `TempSessionMsg` | `+9` bytes, then a length-prefixed group field |

After the prefix, parse:

1. little-endian signed sender length;
2. sender bytes;
3. little-endian signed content length;
4. content bytes.

Treat this layout as version-specific. Add new message kinds only after validating several records.

## Failure Handling

- If `olefile` rejects the backup, verify the signature and test a copy for truncation.
- If no candidate parses, ask for authorized account identifiers associated with the backup and test both ASCII and UTF-16LE MD5 derivation.
- If a candidate parses only one record, reject it as a likely false positive.
- If timestamps are implausible, revisit record prefixes or endianness.
- If text is mojibake, preserve raw bytes and test GB18030 before other encodings.
- If image references become `[图片表情]`, do not claim that the original binary image was recovered.

## Sharing Checklist

Before sharing this skill or a recovery report:

1. Remove backups, screenshots, recovered outputs, and terminal logs.
2. Search for known account numbers, names, key hex values, and absolute user paths.
3. Confirm scripts contain no fixed personal identity defaults.
4. Confirm examples use placeholders only.
5. Run the skill validator.

Use the bundled scripts as the deterministic implementation. Patch them only when the inspected backup proves that its layout differs.

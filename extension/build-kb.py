#!/usr/bin/env python3
"""Собирает базу знаний (kb/*.md, kb/tickets/*.md) в общий макет расширения иик_БазаЗнаний.

Расширение ищет по макету само (kb_search в иик_Инструменты), поэтому статьи едут вместе с .cfe.
Запуск перед deploy.sh: python3 extension/build-kb.py
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
KB = ROOT / "kb"
OUT = ROOT / "extension" / "src" / "CommonTemplates" / "иик_БазаЗнаний" / "Ext" / "Template.txt"
MARK = "=== ФАЙЛ: "  # строка-разделитель статей; её же разбирает расширение

parts = []
for path in sorted(KB.rglob("*.md")):
    if path.name.lower() == "readme.md":
        continue
    text = path.read_text("utf-8").strip().replace("\r\n", "\n")
    parts.append(f"{MARK}{path.relative_to(KB).as_posix()}\n{text}\n")
OUT.parent.mkdir(parents=True, exist_ok=True)
OUT.write_text("﻿" + "\n".join(parts).replace("\n", "\r\n"), "utf-8", newline="")
print(f"{len(parts)} статей → {OUT.relative_to(ROOT)}")

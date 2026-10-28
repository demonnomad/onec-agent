"""Вложения: разбор Excel/CSV/текста, части, поиск, превью для агента, ограничения."""
import asyncio
import io

import pytest
from openpyxl import Workbook

from onec_agent import files

SESSION = "test-session-0123456789"


def upload(name: str, data: bytes) -> dict:
    return asyncio.run(files.save_upload(None, SESSION, name, data))  # http нужен только для скриншотов


def xlsx(rows: int) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "Реестр"
    ws.append(["Номер", "Контрагент", "ИНН", "Сумма"])
    for i in range(1, rows + 1):
        ws.append([f"ПА-{i:04d}", "Ромашка ООО" if i == 120 else f"Контрагент {i}", f"77{i:08d}", i * 100.5])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_excel_is_split_into_parts_with_header():
    meta = upload("реестр.xlsx", xlsx(200))
    assert meta["kind"] == "table" and meta["parts"] == 3  # по 80 строк
    part2 = files.read_part(SESSION, meta["id"], 2)
    assert part2["text"].splitlines()[0] == "Номер\tКонтрагент\tИНН\tСумма"  # заголовок в каждой части
    assert "строки 82–161" in part2["title"]


def test_search_finds_line_in_any_part():
    meta = upload("реестр.xlsx", xlsx(200))
    found = files.search(SESSION, "ромашка")
    assert found["total"] == 1 and found["hits"][0]["part"] == 2 and "ПА-0120" in found["hits"][0]["line"]
    assert files.search(SESSION, "ПА-0120 ромашка", file_id=meta["id"])["total"] == 1


def test_csv_and_text():
    meta = upload("выписка.csv", "Дата;Сумма;Плательщик\n01.09.2026;1 500,00;Ромашка\n".encode("cp1251"))
    assert meta["kind"] == "table"
    assert "Ромашка" in files.read_part(SESSION, meta["id"], 1)["text"]  # cp1251 распознан
    note = files.attachment_note(SESSION, [meta["id"]])
    assert "выписка.csv" in note and "Плательщик" in note


def test_limits_and_errors():
    with pytest.raises(files.FileError, match="не поддерживается"):
        upload("вирус.exe", b"MZ")
    meta = upload("заметка.txt", "одна строка".encode())
    with pytest.raises(files.FileError, match="части 1–1"):
        files.read_part(SESSION, meta["id"], 5)
    with pytest.raises(files.FileError, match="пустой"):
        files.search(SESSION, "   ")

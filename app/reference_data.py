from __future__ import annotations
from functools import lru_cache
from pathlib import Path
import xml.etree.ElementTree as ET

BASE = Path(__file__).resolve().parent / "reference_data" / "xml"
FILES = {
    "unusual_codes": "unusual_codes.xml",
    "participant_kinds": "partic_kinds.xml",
    "organization_kinds": "org_kinds.xml",
    "operation_codes": "oper_codes.xml",
    "organization_forms": "org_forms.xml",
    "currencies": "currency_codes.xml",
    "countries": "country_codes.xml",
    "document_codes": "doc_codes.xml",
    "towns": "town_codes.xml",
    "towns_view": "v_town_codes.xml",
}


def _read(path: Path) -> str:
    data = path.read_bytes()
    for encoding in ("utf-8-sig", "windows-1251"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            pass
    return data.decode("utf-8", errors="replace")


@lru_cache(maxsize=None)
def get_reference(name: str) -> list[dict[str, str]]:
    path = BASE / FILES[name]
    root = ET.fromstring(_read(path))
    rows: list[dict[str, str]] = []
    for row in root.findall(".//ROW"):
        item = {child.tag: (child.text or "").strip() for child in row}
        code = item.get("CODE", "").strip()
        name_value = item.get("NAME", "").strip()
        if code and name_value:
            item["CODE"] = code
            item["NAME"] = name_value
            rows.append(item)
    return rows


def ref_map(name: str) -> dict[str, dict[str, str]]:
    return {item["CODE"]: item for item in get_reference(name)}


def code_exists(name: str, code: str) -> bool:
    return str(code).strip() in ref_map(name)


def currency_numeric_by_alpha(alpha: str) -> str | None:
    alpha = alpha.strip().upper()
    for item in get_reference("currencies"):
        if item.get("CODELAT", "").strip().upper() == alpha:
            return item["CODE"].zfill(3)
    return None


def reference_context() -> dict[str, list[dict[str, str]]]:
    return {key: get_reference(key) for key in FILES}

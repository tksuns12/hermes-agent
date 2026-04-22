"""Generate minimal representative DOCX/XLSX fixtures for live preflight tests.

These fixtures are intentionally small, deterministic, and built only with
standard-library ZIP/XML primitives so they can be re-created on demand without
checking binary assets into the repository.
"""

from __future__ import annotations

from html import escape
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

REPRESENTATIVE_DOCX_FILENAME = "m004-s04-representative.docx"
REPRESENTATIVE_XLSX_FILENAME = "m004-s04-representative.xlsx"


def _zip_write(path: Path, members: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(path, mode="w", compression=ZIP_DEFLATED) as archive:
        for member_name, body in members.items():
            archive.writestr(member_name, body)


def build_minimal_docx(path: Path, *, title: str, body_lines: list[str]) -> Path:
    """Write a minimal valid DOCX package to ``path``."""
    safe_title = escape(title)
    safe_lines = [escape(line) for line in body_lines if line.strip()]
    paragraphs = "".join(
        f"<w:p><w:r><w:t xml:space=\"preserve\">{line}</w:t></w:r></w:p>"
        for line in safe_lines
    )

    members = {
        "[Content_Types].xml": """<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
</Types>
""".strip(),
        "_rels/.rels": """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>
""".strip(),
        "word/document.xml": f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:wpc="http://schemas.microsoft.com/office/word/2010/wordprocessingCanvas"
    xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006"
    xmlns:o="urn:schemas-microsoft-com:office:office"
    xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    xmlns:m="http://schemas.openxmlformats.org/officeDocument/2006/math"
    xmlns:v="urn:schemas-microsoft-com:vml"
    xmlns:wp14="http://schemas.microsoft.com/office/word/2010/wordprocessingDrawing"
    xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"
    xmlns:w10="urn:schemas-microsoft-com:office:word"
    xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml"
    xmlns:w15="http://schemas.microsoft.com/office/word/2012/wordml"
    xmlns:w16se="http://schemas.microsoft.com/office/word/2015/wordml/symex"
    xmlns:wpg="http://schemas.microsoft.com/office/word/2010/wordprocessingGroup"
    xmlns:wpi="http://schemas.microsoft.com/office/word/2010/wordprocessingInk"
    xmlns:wne="http://schemas.microsoft.com/office/word/2006/wordml"
    xmlns:wps="http://schemas.microsoft.com/office/word/2010/wordprocessingShape"
    mc:Ignorable="w14 w15 w16se wp14">
  <w:body>
    <w:p><w:r><w:t>{safe_title}</w:t></w:r></w:p>
    {paragraphs}
    <w:sectPr>
      <w:pgSz w:w="12240" w:h="15840"/>
      <w:pgMar w:top="1440" w:right="1440" w:bottom="1440" w:left="1440" w:header="708" w:footer="708" w:gutter="0"/>
    </w:sectPr>
  </w:body>
</w:document>
""".strip(),
        "word/_rels/document.xml.rels": """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"/>
""".strip(),
    }

    _zip_write(path, members)
    return path


def _column_name(index: int) -> str:
    if index < 1:
        raise ValueError("Column index must be positive")
    name = ""
    value = index
    while value:
        value, remainder = divmod(value - 1, 26)
        name = chr(ord("A") + remainder) + name
    return name


def _build_sheet_xml(rows: list[list[object]]) -> str:
    xml_rows: list[str] = []
    for row_index, row in enumerate(rows, start=1):
        cells: list[str] = []
        for col_index, value in enumerate(row, start=1):
            ref = f"{_column_name(col_index)}{row_index}"
            if isinstance(value, (int, float)):
                cells.append(f"<c r=\"{ref}\"><v>{value}</v></c>")
            else:
                text = escape(str(value))
                cells.append(
                    f"<c r=\"{ref}\" t=\"inlineStr\"><is><t>{text}</t></is></c>"
                )
        xml_rows.append(f"<row r=\"{row_index}\">{''.join(cells)}</row>")

    return (
        "<?xml version=\"1.0\" encoding=\"UTF-8\"?>"
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        f"<sheetData>{''.join(xml_rows)}</sheetData>"
        "</worksheet>"
    )


def build_minimal_xlsx(path: Path, *, sheet_name: str, rows: list[list[object]]) -> Path:
    """Write a minimal valid XLSX package to ``path``."""
    if not rows:
        raise ValueError("rows must contain at least one row")

    safe_sheet_name = escape(sheet_name or "Sheet1")
    members = {
        "[Content_Types].xml": """<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
  <Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
</Types>
""".strip(),
        "_rels/.rels": """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>
""".strip(),
        "xl/workbook.xml": f"""<?xml version="1.0" encoding="UTF-8"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
          xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <sheets>
    <sheet name="{safe_sheet_name}" sheetId="1" r:id="rId1"/>
  </sheets>
</workbook>
""".strip(),
        "xl/_rels/workbook.xml.rels": """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
</Relationships>
""".strip(),
        "xl/worksheets/sheet1.xml": _build_sheet_xml(rows),
    }

    _zip_write(path, members)
    return path


def build_representative_office_fixtures(target_dir: Path) -> dict[str, Path]:
    """Build representative DOCX/XLSX fixtures and return their paths."""
    target_dir.mkdir(parents=True, exist_ok=True)

    docx_path = build_minimal_docx(
        target_dir / REPRESENTATIVE_DOCX_FILENAME,
        title="Hermes DOCX Representative Sample",
        body_lines=[
            "Owner: Product Ops",
            "Risk: Quarterly onboarding backlog is increasing.",
            "Action: Publish a markdown executive summary artifact.",
        ],
    )

    xlsx_path = build_minimal_xlsx(
        target_dir / REPRESENTATIVE_XLSX_FILENAME,
        sheet_name="Anomalies",
        rows=[
            ["region", "week", "value", "delta_pct"],
            ["APAC", "2026-W15", 1200, 4.2],
            ["APAC", "2026-W16", 610, -49.2],
            ["EMEA", "2026-W15", 980, 1.3],
            ["EMEA", "2026-W16", 1475, 50.5],
        ],
    )

    return {"docx": docx_path, "xlsx": xlsx_path}

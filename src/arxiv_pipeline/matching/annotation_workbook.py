"""Text-safe XLSX forms and strict import for the TV3 annotation workflow.

Only Gan_nhan and Candidates are exported. No labels or reviews are invented.
The reader accepts Excel/Google Sheets inline/shared strings and rejects formulas
and numeric IDs rather than guessing how a locale changed an arXiv identifier.
"""
from __future__ import annotations

from pathlib import Path
import posixpath
import re
import zipfile
import xml.etree.ElementTree as ET

from arxiv_pipeline.matching.contracts import (
    ContractError, LabelRecord, PublicationBundle, canonical_id,
)
from arxiv_pipeline.matching.labeling import _format_authors

NS = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'
REL = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
ET.register_namespace('', NS)
ET.register_namespace('r', REL)

def element(tag, attrs=None, text=None):
    value = ET.Element(f'{{{NS}}}{tag}', attrs or {})
    if text is not None:
        value.text = text
    return value

def xml(node):
    return ET.tostring(node, encoding='utf-8', xml_declaration=True)

def col(index):
    result = ''
    while index:
        index, remainder = divmod(index - 1, 26)
        result = chr(65 + remainder) + result
    return result

def worksheet(rows, widths, annotation=False):
    root = element('worksheet')
    root.append(element('dimension', {'ref': f'A1:{col(len(rows[0]))}{len(rows)}'}))
    views = element('sheetViews')
    view = element('sheetView', {'workbookViewId': '0', 'zoomScale': '85'})
    if annotation:
        view.append(element('pane', {'xSplit': '2', 'ySplit': '1', 'topLeftCell': 'C2', 'activePane': 'bottomRight', 'state': 'frozen'}))
    else:
        view.append(element('pane', {'ySplit': '1', 'topLeftCell': 'A2', 'activePane': 'bottomLeft', 'state': 'frozen'}))
    views.append(view); root.append(views)
    columns = element('cols')
    for i, width in enumerate(widths, start=1):
        columns.append(element('col', {'min': str(i), 'max': str(i), 'width': str(width), 'customWidth': '1', 'style': '0'}))
    root.append(columns)
    data = element('sheetData')
    for n, values in enumerate(rows, start=1):
        row = element('row', {'r': str(n), 'ht': '36' if n == 1 else ('90' if annotation else '54'), 'customHeight': '1'})
        for i, value in enumerate(values, start=1):
            cell = element('c', {'r': f'{col(i)}{n}', 't': 'inlineStr', 's': '1' if n == 1 else ('2' if annotation and i >= 9 else '0')})
            inline = element('is')
            inline.append(element('t', {'{http://www.w3.org/XML/1998/namespace}space': 'preserve'}, str(value)))
            cell.append(inline); row.append(cell)
        data.append(row)
    root.append(data)
    if len(rows[0]) > 1:
        root.append(element('autoFilter', {'ref': f'A1:{col(len(rows[0]))}{len(rows)}'}))
    if annotation:
        rules = [('I', 'matched,unmatched,uncertain'), ('K', 'tv1,tv2,tv3,tv4,tv5'), ('L', 'tv1,tv2,tv3,tv4,tv5'), ('M', 'pending,accepted,rejected')]
        dvs = element('dataValidations', {'count': str(len(rules))})
        for letter, choices in rules:
            dv = element('dataValidation', {'type': 'list', 'allowBlank': '1', 'showErrorMessage': '1', 'errorTitle': 'Giá trị chưa hợp lệ', 'error': 'Chọn một giá trị từ danh sách.', 'sqref': f'{letter}2:{letter}{len(rows)}'})
            dv.append(element('formula1', text=f'"{choices}"'))
            dvs.append(dv)
        root.append(dvs)
    root.append(element('pageMargins', {'left': '0.25','right': '0.25','top': '0.5','bottom': '0.5','header': '0.2','footer': '0.2'}))
    return xml(root)

STYLES = f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="{NS}">
 <fonts count="2"><font><sz val="11"/><name val="Calibri"/></font><font><b/><color rgb="FFFFFFFF"/><sz val="11"/><name val="Calibri"/></font></fonts>
 <fills count="4"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill><fill><patternFill patternType="solid"><fgColor rgb="FF17365D"/><bgColor indexed="64"/></patternFill></fill><fill><patternFill patternType="solid"><fgColor rgb="FFFFF2CC"/><bgColor indexed="64"/></patternFill></fill></fills>
 <borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>
 <cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
 <cellXfs count="3">
  <xf numFmtId="49" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1" applyAlignment="1"><alignment vertical="top" wrapText="1"/></xf>
  <xf numFmtId="49" fontId="1" fillId="2" borderId="0" xfId="0" applyNumberFormat="1" applyAlignment="1" applyFill="1" applyFont="1"><alignment vertical="center" wrapText="1"/></xf>
  <xf numFmtId="49" fontId="0" fillId="3" borderId="0" xfId="0" applyNumberFormat="1" applyAlignment="1" applyFill="1"><alignment vertical="top" wrapText="1"/></xf>
 </cellXfs><cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>
</styleSheet>'''

def write_workbook(destination, sheets):
    if destination.exists():
        raise FileExistsError(f'Preserve/review existing workbook before overwriting: {destination}')
    content_types = ['<?xml version="1.0" encoding="UTF-8"?>', '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">',
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>',
        '<Default Extension="xml" ContentType="application/xml"/>',
        '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>',
        '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>']
    book = element('workbook'); book.append(element('bookViews'))
    book[0].append(element('workbookView', {'activeTab': '0'}))
    tabs = element('sheets'); book.append(tabs)
    rels = ET.Element('Relationships', {'xmlns': 'http://schemas.openxmlformats.org/package/2006/relationships'})
    for i, (name, _, _, _) in enumerate(sheets, start=1):
        tabs.append(element('sheet', {'name': name, 'sheetId': str(i), f'{{{REL}}}id': f'rId{i}'}))
        ET.SubElement(rels, 'Relationship', {'Id': f'rId{i}', 'Type': REL+'/worksheet', 'Target': f'worksheets/sheet{i}.xml'})
        content_types.append(f'<Override PartName="/xl/worksheets/sheet{i}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>')
    ET.SubElement(rels, 'Relationship', {'Id': f'rId{len(sheets)+1}', 'Type': REL+'/styles', 'Target': 'styles.xml'})
    content_types.append('</Types>')
    with zipfile.ZipFile(destination, 'x', compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('[Content_Types].xml', ''.join(content_types))
        archive.writestr('_rels/.rels', f'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="{REL}/officeDocument" Target="xl/workbook.xml"/></Relationships>')
        archive.writestr('xl/workbook.xml', xml(book))
        archive.writestr('xl/_rels/workbook.xml.rels', xml(rels))
        archive.writestr('xl/styles.xml', STYLES)
        for i, (_, rows, widths, annotation) in enumerate(sheets, start=1):
            archive.writestr(f'xl/worksheets/sheet{i}.xml', worksheet(rows, widths, annotation))


ANNOTATION_COLUMNS = (
    'arxiv_id', 'citation_key', 'bib_title', 'bib_authors', 'bib_year',
    'bib_doi', 'bib_url', 'candidates_preview', 'decision',
    'groundtruth_arxiv_id', 'annotator', 'reviewer', 'review_status', 'notes',
)
SOURCE_COLUMNS = ANNOTATION_COLUMNS[:8]


def annotation_rows(bundles: list[PublicationBundle]) -> list[dict[str, str]]:
    """Build the same source columns as labeling.export_annotation_sheet."""
    rows = []
    seen = set()
    for bundle in bundles:
        if bundle.readiness != 'ready':
            raise ContractError(f'Publication is not ready: {bundle.arxiv_id}')
        preview = '\n'.join(
            f'[{i}] {ref.ref_arxiv_id} | {ref.ref_title} | '
            f'{_format_authors(ref.ref_authors)} | {ref.ref_submission_date or ""}'
            for i, ref in enumerate(bundle.references)
        ) or '(no candidates)'
        for bib in bundle.bibtex:
            key = bundle.arxiv_id, bib.citation_key
            if key in seen:
                raise ContractError(f'Duplicate query: {key}')
            seen.add(key)
            row = dict.fromkeys(ANNOTATION_COLUMNS, '')
            row.update(arxiv_id=bundle.arxiv_id, citation_key=bib.citation_key,
                bib_title=bib.title, bib_authors=_format_authors(bib.authors),
                bib_year='' if bib.year is None else str(bib.year),
                bib_doi=bib.doi or '', bib_url=bib.url or '',
                candidates_preview=preview, review_status='pending')
            rows.append(row)
    return rows


def export_annotation_workbook(bundles: list[PublicationBundle], output: Path | str) -> int:
    """Write a new workbook with IDs stored as text. Refuse any overwrite."""
    path = Path(output)
    rows = annotation_rows(bundles)
    if not rows:
        raise ContractError('Cannot export an empty annotation workbook')
    candidates = [['arxiv_id', 'candidate_index', 'ref_arxiv_id', 'ref_title',
        'ref_authors', 'ref_submission_date']]
    for bundle in bundles:
        for i, ref in enumerate(bundle.references):
            candidates.append([bundle.arxiv_id, str(i), ref.ref_arxiv_id,
                ref.ref_title or '', '; '.join(ref.ref_authors), ref.ref_submission_date or ''])
    data = [list(ANNOTATION_COLUMNS)] + [[row[c] for c in ANNOTATION_COLUMNS] for row in rows]
    for row in data + candidates:
        if any(len(value.encode('utf-16-le')) // 2 > 32767 for value in row):
            raise ContractError('A cell exceeds the XLSX text limit; split the input publication')
    path.parent.mkdir(parents=True, exist_ok=True)
    write_workbook(path, [
        ('Gan_nhan', data, [17,25,62,38,10,28,30,95,16,22,14,14,18,64], True),
        ('Candidates', candidates, [17,17,20,90,75,23], False),
    ])
    return len(rows)


def _xml(archive: zipfile.ZipFile, name: str):
    raw = archive.read(name)
    if b'<!DOCTYPE' in raw.upper() or b'<!ENTITY' in raw.upper():
        raise ContractError('DTD/entity declarations are not supported')
    return ET.fromstring(raw)


def _cell_value(cell, strings: list[str]) -> str:
    if cell.find(f'{{{NS}}}f') is not None:
        raise ContractError(f'Formula in {cell.get("r")}; use literal values')
    kind = cell.get('t', 'n')
    raw = cell.findtext(f'{{{NS}}}v', '')
    if kind == 'inlineStr':
        return ''.join(t.text or '' for t in cell.findall(f'{{{NS}}}is//{{{NS}}}t'))
    if kind == 's':
        if not raw.isdigit() or int(raw) >= len(strings):
            raise ContractError('Invalid shared string index')
        return strings[int(raw)]
    if kind in {'str', 'n'}:
        return raw
    if raw:
        raise ContractError(f'Unsupported cell type {kind} in {cell.get("r")}')
    return ''


def _read_annotation_rows(path: Path) -> list[dict[str, str]]:
    with zipfile.ZipFile(path) as archive:
        if sum(i.file_size for i in archive.infolist()) > 64 * 1024 * 1024:
            raise ContractError('Workbook exceeds the 64 MiB uncompressed input limit')
        book = _xml(archive, 'xl/workbook.xml')
        tabs = [s for s in book.findall(f'{{{NS}}}sheets/{{{NS}}}sheet') if s.get('name') == 'Gan_nhan']
        if len(tabs) != 1:
            raise ContractError('Expected one Gan_nhan sheet')
        relations = _xml(archive, 'xl/_rels/workbook.xml.rels')
        relation = next((r for r in relations if r.get('Id') == tabs[0].get(f'{{{REL}}}id')), None)
        if relation is None or relation.get('TargetMode') == 'External':
            raise ContractError('Missing/local worksheet relationship required')
        target = relation.get('Target', '')
        name = target.lstrip('/') if target.startswith('/') else posixpath.normpath('xl/' + target)
        if not name.startswith('xl/') or '..' in name.split('/'):
            raise ContractError('Worksheet path escapes workbook')
        strings = []
        if 'xl/sharedStrings.xml' in archive.namelist():
            shared = _xml(archive, 'xl/sharedStrings.xml')
            strings = [''.join(t.text or '' for t in item.iter(f'{{{NS}}}t')) for item in shared]
        sheet = _xml(archive, name)
        header = None
        result = []
        for row in sheet.findall(f'{{{NS}}}sheetData/{{{NS}}}row'):
            cells = {}
            for cell in row:
                address = cell.get('r', '')
                if not re.fullmatch(r'[A-Z]+[1-9][0-9]*', address):
                    raise ContractError('Invalid worksheet cell reference')
                column = re.sub(r'[0-9]+$', '', address)
                if column in cells:
                    raise ContractError(f'Duplicate cell {address}')
                cells[column] = (cell, _cell_value(cell, strings))
            if not any(value for _, value in cells.values()):
                continue
            if header is None:
                header = {column: value for column, (_, value) in cells.items()}
                if len(header) != len(ANNOTATION_COLUMNS) or set(header.values()) != set(ANNOTATION_COLUMNS):
                    raise ContractError('Unexpected annotation headers')
                continue
            if any(column not in header and value for column, (_, value) in cells.items()):
                raise ContractError('Unexpected data outside annotation columns')
            record = {}
            for column, field in header.items():
                cell, value = cells.get(column, (None, ''))
                if field in {'arxiv_id', 'citation_key', 'groundtruth_arxiv_id'} and value:
                    if cell.get('t') not in {'s', 'inlineStr', 'str'}:
                        raise ContractError(f'{field} must be text; restore ID from the original XLSX')
                record[field] = value.replace('\r\n', '\n')
            result.append(record)
        if header is None:
            raise ContractError('Empty annotation worksheet')
        return result


def load_annotation_workbook(path: Path | str, bundles: list[PublicationBundle],
        *, expected_annotator: str | None = None) -> list[LabelRecord]:
    """Import edits against an immutable query registry; never auto-accept."""
    try:
        rows = _read_annotation_rows(Path(path))
        expected = {(r['arxiv_id'], r['citation_key']): r for r in annotation_rows(bundles)}
        targets = {b.arxiv_id: {r.ref_arxiv_id for r in b.references} for b in bundles}
        seen, labels = set(), []
        for row in rows:
            pid = canonical_id(row['arxiv_id'])
            key = pid, row['citation_key']
            if key in seen or key not in expected:
                raise ContractError(f'Duplicate/unknown query: {key}')
            seen.add(key)
            if any(row[c] != expected[key][c] for c in SOURCE_COLUMNS):
                raise ContractError(f'Source columns changed for {key}; restore from original')
            decision = row['decision'].strip().lower()
            if not decision:
                if row['groundtruth_arxiv_id'].strip() or row['review_status'].strip().lower() not in {'', 'pending'}:
                    raise ContractError(f'Groundtruth/review without decision: {key}')
                continue
            annotator = row['annotator'].strip().lower() or None
            if not annotator or (expected_annotator and annotator != expected_annotator):
                raise ContractError(f'Wrong/missing annotator for {key}')
            gt = canonical_id(row['groundtruth_arxiv_id']) if row['groundtruth_arxiv_id'].strip() else None
            label = LabelRecord(arxiv_id=pid, citation_key=key[1],
                groundtruth_arxiv_id=gt, decision=decision, label_source='manual',
                annotator=annotator, reviewer=row['reviewer'].strip().lower() or None,
                review_status=row['review_status'].strip().lower() or 'pending',
                notes=row['notes'].strip() or None)
            if gt is not None and gt not in targets[pid]:
                raise ContractError(f'Groundtruth outside candidates: {key}')
            if decision == 'uncertain' and not label.notes:
                raise ContractError(f'Uncertain decision requires notes: {key}')
            labels.append(label)
        if seen != set(expected):
            raise ContractError('Worksheet is missing queries; do not delete filtered rows')
        return labels
    except ContractError:
        raise
    except (OSError, ValueError, KeyError, IndexError, ET.ParseError, zipfile.BadZipFile) as exc:
        raise ContractError(f'Invalid annotation workbook: {exc}') from exc

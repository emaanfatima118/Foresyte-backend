from fastapi import APIRouter, Depends, HTTPException, status, Query, BackgroundTasks
from fastapi.responses import FileResponse, Response
from sqlalchemy.orm import Session
from sqlalchemy import false as sql_false
from uuid import UUID
from datetime import datetime, date
from typing import List, Optional, Dict, Any
from pydantic import BaseModel, computed_field
import os
import json
import csv
import logging
from pathlib import Path
from xml.sax.saxutils import escape as _xml_escape

logger = logging.getLogger(__name__)

try:
    from reportlab.lib.pagesizes import letter, A4, landscape
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import inch
    from reportlab.lib import colors
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak, Image
    from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
    REPORTLAB_AVAILABLE = True
except ImportError as e:
    REPORTLAB_AVAILABLE = False
    logger.warning("reportlab not installed: %s. PDF generation will create text files. Install with: pip install reportlab", e)


def _insert_soft_line_breaks(text: str, max_chars: int = 40) -> str:
    """
    Split long strings at word boundaries and join with <br/> so ReportLab Paragraph
    cannot paint text past the cell (overflow into adjacent columns).
    Each line is XML-escaped; <br/> is inserted between lines only.
    """
    raw = str(text) if text is not None else ""
    raw = raw.replace("\r\n", " ").replace("\r", " ").replace("\n", " ")
    words = raw.split()
    if not words:
        return _xml_escape(raw.strip())
    lines: list[list[str]] = []
    cur: list[str] = []
    line_len = 0
    for w in words:
        add = len(w) + (1 if cur else 0)
        if cur and line_len + add > max_chars:
            lines.append(cur)
            cur = [w]
            line_len = len(w)
        else:
            cur.append(w)
            line_len += add
    if cur:
        lines.append(cur)
    escaped = [_xml_escape(" ".join(line)) for line in lines]
    return "<br/>".join(escaped)


def _pdf_paragraph_cell(
    text: Any,
    style,
    *,
    soft_wrap_chars: Optional[int] = None,
) -> Any:
    """Single table cell: wrap long text; escape XML for ReportLab Paragraph."""
    if not REPORTLAB_AVAILABLE:
        return str(text) if text is not None else ""
    from reportlab.platypus import Paragraph

    raw = str(text) if text is not None else ""
    if soft_wrap_chars is not None:
        safe = _insert_soft_line_breaks(raw, max_chars=max(18, soft_wrap_chars))
    else:
        raw = raw.replace("\r\n", "\n").replace("\r", "\n")
        safe = _xml_escape(raw).replace("\n", "<br/>")
    return Paragraph(safe, style)


def _pdf_link_cell(
    url: Any,
    style,
    *,
    display_text: Optional[str] = None,
    soft_wrap_chars: Optional[int] = None,
) -> Any:
    """Paragraph cell with wrapped visible text but one full clickable hyperlink."""
    if not REPORTLAB_AVAILABLE:
        return str(display_text or url or "")
    from reportlab.platypus import Paragraph

    raw_url = str(url).strip() if url is not None else ""
    raw_text = str(display_text if display_text is not None else raw_url)
    if not raw_url:
        return Paragraph(_xml_escape(raw_text), style)
    if soft_wrap_chars is not None:
        safe_text = _insert_soft_line_breaks(raw_text, max_chars=max(18, soft_wrap_chars))
    else:
        safe_text = _xml_escape(raw_text).replace("\n", "<br/>")
    safe_href = _xml_escape(raw_url).replace("\n", "").replace("\r", "")
    return Paragraph(f'<link href="{safe_href}">{safe_text}</link>', style)


from database.db import get_db, SessionLocal
from database.models import (
    Report,
    Violation,
    Investigator,
    StudentActivity,
    Exam,
    Invigilator,
    InvigilatorActivity,
    Room,
    ExamRoomAssignment,
)
from database.auth import get_current_user
from database.severity_logic import severity_from_int, SEVERITY_TO_INT
from database.cheating_labels import is_supported_cheating_activity_type
from app.storage.blob_storage import (
    prepare_evidence_files_for_report,
    upload_report_file,
    get_report_blob_url,
    download_report_bytes,
)

router = APIRouter(prefix="/reports", tags=["Reports"])


def _report_row_severity_rank(row: Dict[str, Any]) -> int:
    """
    Sort rank for a report activity row: critical=4 … low=1; unknown/N/A=0.
    Uses activity severity and, when present, nested violation severity (max of both).
    """
    def _one(raw: Any) -> int:
        if raw is None:
            return 0
        if isinstance(raw, bool):
            return 0
        if isinstance(raw, int):
            if raw < 1:
                return 0
            if raw > 4:
                return 4
            return raw
        s = str(raw).strip().lower()
        if not s or s in ("n/a", "unknown"):
            return 0
        if s.isdigit():
            v = int(s)
            return v if 1 <= v <= 4 else 0
        return int(SEVERITY_TO_INT.get(s, 0))

    r = _one(row.get("severity"))
    v = row.get("violation")
    if isinstance(v, dict) and v.get("severity") is not None:
        r = max(r, _one(v.get("severity")))
    return r


# -------------------------
# Report Generation Utilities
# -------------------------
REPORTS_DIR = Path("uploads/reports")
REPORTS_DIR.mkdir(parents=True, exist_ok=True)


def _report_blob_url_for_path(file_path: Optional[str]) -> Optional[str]:
    filename = Path(str(file_path or "")).name
    if not filename:
        return None
    return get_report_blob_url(filename)


def _build_report_evidence_items(
    report_id: str,
    rows: List[Dict[str, Any]],
    actor_label: str,
    *,
    url_field: str = "report_evidence_url",
    caption_prefix: str | None = None,
) -> List[Dict[str, str]]:
    urls: List[str] = []
    seen_urls: set[str] = set()
    captions: Dict[str, str] = {}
    for row in rows or []:
        url = str(row.get(url_field) or "").strip()
        if not url or url in ("N/A", "") or url in seen_urls:
            continue
        seen_urls.add(url)
        urls.append(url)
        who = row.get("student_name") or row.get("invigilator_name") or actor_label
        when = row.get("timestamp") or ""
        behavior = row.get("activity_type") or row.get("type") or ""
        prefix = caption_prefix or actor_label
        captions[url] = f"{prefix}: {who} | {behavior} | {when}"

    local_map = prepare_evidence_files_for_report(str(report_id), urls)
    items: List[Dict[str, str]] = []
    for url in urls:
        local_path = local_map.get(url)
        if local_path:
            items.append({"path": local_path, "caption": captions.get(url, actor_label)})
    return items


def _build_seat_mapping_embed_items(
    report_id: str,
    activities: List[Dict[str, Any]],
) -> List[Dict[str, str]]:
    """
    PDF embeds only the shared student seat-mapping frame (identification_evidence_url / *_rolls),
    not per-violation detection frames (report_evidence_url).
    """
    rows: List[Dict[str, Any]] = []
    for act in activities or []:
        url = str(act.get("identification_evidence_url") or "").strip()
        if not url or url in ("N/A", ""):
            continue
        rows.append(
            {
                "identification_evidence_url": url,
                "student_name": act.get("student_name"),
                "activity_type": act.get("activity_type"),
                "timestamp": act.get("timestamp"),
            }
        )
    return _build_report_evidence_items(
        str(report_id),
        rows,
        "Seat mapping",
        url_field="identification_evidence_url",
        caption_prefix="Student seat mapping",
    )


def _append_report_evidence_section(
    story: List[Any],
    title: str,
    evidence_items: List[Dict[str, str]],
    styles,
    max_width: float,
    *,
    leading_page_break: bool = True,
) -> None:
    if not REPORTLAB_AVAILABLE or not evidence_items:
        return

    if leading_page_break:
        story.append(PageBreak())
    else:
        story.append(Spacer(1, 0.22 * inch))
    story.append(Paragraph(title, styles["Heading2"]))
    story.append(Spacer(1, 0.12 * inch))
    for item in evidence_items:
        img_path = item.get("path")
        if not img_path or not Path(img_path).exists():
            continue
        try:
            img = Image(img_path)
            iw, ih = img.imageWidth, img.imageHeight
            if iw and ih:
                scale = min(max_width / float(iw), 4.4 * inch / float(ih), 1.0)
                img.drawWidth = float(iw) * scale
                img.drawHeight = float(ih) * scale
            story.append(Paragraph(_xml_escape(item.get("caption", "")), styles["Normal"]))
            story.append(Spacer(1, 0.06 * inch))
            story.append(img)
            story.append(Spacer(1, 0.18 * inch))
        except Exception as exc:
            logger.warning("Failed to embed report evidence image %s: %s", img_path, exc)


def generate_json_report(data: Dict[str, Any], file_path: str) -> bool:
    """Generate a comprehensive JSON report file with all details."""
    full_path = REPORTS_DIR / Path(file_path).name
    logger.info("generate_json_report: starting, file_path=%s, full_path=%s", file_path, full_path)
    try:
        # Ensure all data is JSON serializable
        json_data = {
            'report_metadata': {
                'title': data.get('title', 'Report'),
                'generated_at': data.get('generated_at', datetime.utcnow().isoformat()),
                'report_type': data.get('report_type', 'N/A')
            },
            'summary': data.get('summary', {}),
            'exam_information': data.get('exam', {}),
            'activities_and_violations': data.get('activities', []),
            'primary_violation': data.get('primary_violation', None),
            'invigilator_activities': (
                data.get('invigilator_activities', [])
                if str(data.get('report_type') or '').lower() == 'invigilator'
                else []
            ),
        }
        
        with open(full_path, 'w', encoding='utf-8') as f:
            json.dump(json_data, f, indent=2, default=str)
        
        logger.info("generate_json_report: success, path=%s", full_path)
        return True
    except Exception as e:
        logger.exception("generate_json_report failed: file_path=%s, error=%s", file_path, e)
        return False

def generate_csv_report(data: Dict[str, Any], file_path: str) -> bool:
    """Generate a comprehensive CSV report file with detailed violation information."""
    full_path = REPORTS_DIR / Path(file_path).name
    logger.info("generate_csv_report: starting, file_path=%s, full_path=%s", file_path, full_path)
    try:
        # Prepare rows for CSV with all details
        rows = []
        if 'incidents' in data:
            for incident in data['incidents']:
                row = {
                    'incident_id': incident.get('id', ''),
                    'type': incident.get('type', ''),
                    'timestamp': incident.get('timestamp', ''),
                    'student_name': incident.get('student_name', ''),
                    'severity': incident.get('severity', ''),
                    'status': incident.get('status', '')
                }
                rows.append(row)
        elif 'invigilator_activities' in data:
            for row in (data['invigilator_activities'] or []):
                rows.append({
                    'Activity_ID': row.get('activity_id', ''),
                    'Invigilator': row.get('invigilator_name', ''),
                    'Activity_Type': row.get('activity_type', ''),
                    'Timestamp': row.get('timestamp', ''),
                    'Severity': row.get('severity', ''),
                    'Confidence': row.get('confidence', ''),
                    'Exam': row.get('exam_name', ''),
                    'Room': row.get('room_label', ''),
                    'Evidence_URL': row.get('evidence_url', ''),
                    'Report_Evidence_URL': row.get('report_evidence_url', ''),
                    'Notes': row.get('notes', ''),
                })
        elif 'activities' in data:
            for activity in data['activities']:
                violation_info = activity.get('violation', {})
                row = {
                    'Activity_ID': activity.get('activity_id', ''),
                    'Student_Name': activity.get('student_name', ''),
                    'Roll_Number': activity.get('student_roll_number', ''),
                    'Violation_Type': activity.get('activity_type', ''),
                    'Timestamp': activity.get('timestamp', ''),
                    'Severity': activity.get('severity', ''),
                    'Confidence': activity.get('confidence', ''),
                    'Cheating_Frame_URL': activity.get('evidence_url', ''),
                    'Annotated_Report_Frame_URL': activity.get('report_evidence_url', ''),
                    'Seat_Map_Frame_URL': activity.get('identification_evidence_url', ''),
                    'Violation_ID': violation_info.get('violation_id', 'N/A') if violation_info else 'N/A',
                    'Violation_Severity': violation_info.get('severity', 'N/A') if violation_info else 'N/A',
                    'Violation_Status': violation_info.get('status', 'N/A') if violation_info else 'N/A',
                    'Description': activity.get('description', '')
                }
                
                # Add exam info if available
                if 'exam' in data:
                    row['Exam_Name'] = data['exam'].get('name', '')
                    row['Exam_Date'] = data['exam'].get('date', '')
                
                rows.append(row)
        else:
            # Generic CSV from dict
            rows = [data] if isinstance(data, dict) else data
        
        # If no activities, create a summary row
        if not rows and 'summary' in data:
            rows = [{
                'Report_Type': data.get('report_type', ''),
                'Generated_At': data.get('generated_at', ''),
                'Total_Activities': data['summary'].get('total_activities', 0),
                'Total_Violations': data['summary'].get('total_violations', 0),
                'Unique_Students': data['summary'].get('unique_students_flagged', 0)
            }]
        
        if rows:
            with open(full_path, 'w', newline='', encoding='utf-8') as f:
                if rows:
                    writer = csv.DictWriter(f, fieldnames=rows[0].keys())
                    writer.writeheader()
                    writer.writerows(rows)
        
        logger.info("generate_csv_report: success, path=%s, rows=%s", full_path, len(rows))
        return True
    except Exception as e:
        logger.exception("generate_csv_report failed: file_path=%s, error=%s", file_path, e)
        return False

def generate_pdf_report(data: Dict[str, Any], file_path: str) -> bool:
    """Generate a professional PDF report file using reportlab."""
    full_path = REPORTS_DIR / Path(file_path).name
    logger.info("generate_pdf_report: starting, file_path=%s, full_path=%s", file_path, full_path)
    try:
        if not full_path.suffix.lower() == '.pdf':
            full_path = full_path.with_suffix('.pdf')
        
        if not REPORTLAB_AVAILABLE:
            logger.error("generate_pdf_report: reportlab not available. Install with: pip install reportlab==4.2.5")
            return False
        
        # Landscape: wide frame so full-width Activity/Violation paragraphs wrap correctly.
        _pg_w, _pg_h = landscape(A4)
        _margin_pt = 56
        _usable_w_pt = _pg_w - 2 * _margin_pt

        doc = SimpleDocTemplate(
            str(full_path),
            pagesize=landscape(A4),
            rightMargin=_margin_pt,
            leftMargin=_margin_pt,
            topMargin=_margin_pt,
            bottomMargin=44,
        )
        
        # Container for the 'Flowable' objects
        story = []
        
        # Define styles
        styles = getSampleStyleSheet()
        
        # Custom styles
        title_style = ParagraphStyle(
            'CustomTitle',
            parent=styles['Heading1'],
            fontSize=24,
            textColor=colors.HexColor('#6e5ae6'),
            spaceAfter=30,
            alignment=TA_CENTER,
            fontName='Helvetica-Bold'
        )
        
        heading_style = ParagraphStyle(
            'CustomHeading',
            parent=styles['Heading2'],
            fontSize=16,
            textColor=colors.HexColor('#4a5568'),
            spaceAfter=12,
            spaceBefore=12,
            fontName='Helvetica-Bold'
        )
        
        # Title
        title = data.get('title', 'Report')
        story.append(Paragraph(title, title_style))
        story.append(Spacer(1, 0.2*inch))
        
        # Report metadata
        metadata_data = [
            ['Generated:', datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')],
            ['Report Type:', data.get('report_type', 'N/A')],
        ]
        
        if 'exam' in data:
            exam_info = data['exam']
            metadata_data.append(['Exam:', exam_info.get('name', 'N/A')])
            metadata_data.append(['Exam Date:', exam_info.get('date', 'N/A')])
        
        metadata_table = Table(metadata_data, colWidths=[2*inch, 4*inch])
        metadata_table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (0, -1), colors.HexColor('#f7fafc')),
            ('TEXTCOLOR', (0, 0), (-1, -1), colors.black),
            ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
            ('FONTNAME', (0, 0), (0, -1), 'Helvetica-Bold'),
            ('FONTSIZE', (0, 0), (-1, -1), 10),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 8),
            ('TOPPADDING', (0, 0), (-1, -1), 8),
            ('GRID', (0, 0), (-1, -1), 1, colors.grey),
        ]))
        story.append(metadata_table)
        story.append(Spacer(1, 0.3*inch))
        
        # Summary section
        if 'summary' in data and data['summary']:
            story.append(Paragraph("Executive Summary", heading_style))
            summary_data = [['Metric', 'Value']]
            
            # Format summary data with better presentation
            summary = data['summary']
            if 'total_activities' in summary:
                summary_data.append(['Total Activities Detected', str(summary['total_activities'])])
            if 'total_violations' in summary:
                summary_data.append(['Total Violations', str(summary['total_violations'])])
            if 'unique_students_flagged' in summary:
                summary_data.append(['Unique Students Flagged', str(summary['unique_students_flagged'])])
            if 'exam_name' in summary:
                summary_data.append(['Exam Name', summary['exam_name']])
            if 'exam_date' in summary:
                summary_data.append(['Exam Date', summary['exam_date']])
            
            # Add severity breakdown if available
            if 'severity_breakdown' in summary:
                severity = summary['severity_breakdown']
                story.append(Spacer(1, 0.1*inch))
                summary_data.append(['--- Severity Breakdown ---', ''])
                for level, count in severity.items():
                    if count > 0:
                        summary_data.append([f'{level.title()} Severity', str(count)])
            
            summary_table = Table(summary_data, colWidths=[3.5*inch, 2.5*inch])
            summary_table.setStyle(TableStyle([
                ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#6e5ae6')),
                ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
                ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
                ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
                ('FONTSIZE', (0, 0), (-1, -1), 10),
                ('BOTTOMPADDING', (0, 0), (-1, -1), 8),
                ('TOPPADDING', (0, 0), (-1, -1), 8),
                ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor('#f7fafc')]),
                ('GRID', (0, 0), (-1, -1), 1, colors.grey),
            ]))
            story.append(summary_table)
            story.append(Spacer(1, 0.4*inch))
        
        # Embed seat-mapping frame (*_rolls / identification_evidence_url) only — not detection frames.
        # Evidence column links below stay as-is (evidence / report_evidence URLs).
        _rt = str(data.get("report_type") or "").lower()
        if _rt in ("exam", "incident") and data.get("activities"):
            _seat_items = _build_seat_mapping_embed_items(
                str(data.get("report_id") or "report"),
                data["activities"],
            )
            if _seat_items:
                _append_report_evidence_section(
                    story,
                    "Student seat mapping",
                    _seat_items,
                    styles,
                    _usable_w_pt * 0.92,
                    leading_page_break=False,
                )

        # Detailed Activities table.
        # Keep Activity in the table itself, but give it a wide column and let the row
        # grow vertically when the text wraps. Do not repeat Violation text per row.
        if 'activities' in data and data['activities']:
            story.append(Paragraph("Detailed Violation Report", heading_style))
            story.append(Spacer(1, 0.1*inch))

            pdf_cell_style = ParagraphStyle(
                "PdfActCell",
                parent=styles["Normal"],
                fontName="Helvetica",
                fontSize=7,
                leading=11,
                alignment=TA_LEFT,
                spaceBefore=0,
                spaceAfter=0,
                wordWrap="LTR",
            )
            pdf_header_style = ParagraphStyle(
                "PdfActHeader",
                parent=styles["Normal"],
                fontName="Helvetica-Bold",
                fontSize=7,
                leading=11,
                alignment=TA_LEFT,
                textColor=colors.whitesmoke,
                wordWrap="LTR",
            )
            # 7 columns: Student, Roll, Violation, Time, Sev., Status, R2 URL.
            _cw7 = [
                _usable_w_pt * 0.14,
                _usable_w_pt * 0.10,
                _usable_w_pt * 0.22,
                _usable_w_pt * 0.10,
                _usable_w_pt * 0.07,
                _usable_w_pt * 0.10,
                _usable_w_pt * 0.27,
            ]
            _row_pad = TableStyle(
                [
                    ("ALIGN", (0, 0), (-1, -1), "LEFT"),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("LEFTPADDING", (0, 0), (-1, -1), 4),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 4),
                    ("TOPPADDING", (0, 0), (-1, -1), 5),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
                    ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
                ]
            )

            header_row = [
                [
                    _pdf_paragraph_cell("Student", pdf_header_style),
                    _pdf_paragraph_cell("Roll", pdf_header_style),
                    _pdf_paragraph_cell("Violation", pdf_header_style),
                    _pdf_paragraph_cell("Time", pdf_header_style),
                    _pdf_paragraph_cell("Sev.", pdf_header_style),
                    _pdf_paragraph_cell("Status", pdf_header_style),
                    _pdf_paragraph_cell("R2 URL", pdf_header_style),
                ]
            ]
            hdr_tbl = Table(header_row, colWidths=_cw7)
            hdr_style = TableStyle(list(_row_pad.getCommands()))
            hdr_style.add("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#6e5ae6"))
            hdr_tbl.setStyle(hdr_style)
            story.append(hdr_tbl)

            # Include every activity (no cap). Activity wraps in-cell and increases row height.
            for idx, activity in enumerate(data["activities"]):
                violation_info = activity.get("violation", {}) or {}

                student_name = activity.get("student_name", "Unknown") or "Unknown"
                if len(student_name) > 22:
                    student_name = student_name[:19] + "..."

                ts = activity.get("timestamp", "") or ""
                time_only = ts[-8:] if len(ts) >= 8 else (ts or "N/A")
                evidence_url = (
                    activity.get("evidence_url")
                    or activity.get("report_evidence_url")
                    or "N/A"
                )

                bg = colors.white if idx % 2 == 0 else colors.HexColor("#f7fafc")

                row = [
                    [
                        _pdf_paragraph_cell(student_name, pdf_cell_style),
                        _pdf_paragraph_cell(activity.get("student_roll_number", "N/A"), pdf_cell_style),
                        _pdf_paragraph_cell(
                            activity.get("activity_type", "N/A"),
                            pdf_cell_style,
                            soft_wrap_chars=55,
                        ),
                        _pdf_paragraph_cell(time_only, pdf_cell_style),
                        _pdf_paragraph_cell(str(activity.get("severity", "N/A")), pdf_cell_style),
                        _pdf_paragraph_cell(
                            violation_info.get("status", "N/A") if violation_info else "N/A",
                            pdf_cell_style,
                        ),
                        _pdf_link_cell(
                            evidence_url,
                            pdf_cell_style,
                            display_text="evidence",
                            soft_wrap_chars=44,
                        ),
                    ]
                ]
                row_tbl = Table(row, colWidths=_cw7)
                rs = TableStyle(list(_row_pad.getCommands()))
                rs.add("BACKGROUND", (0, 0), (-1, 0), bg)
                row_tbl.setStyle(rs)
                story.append(row_tbl)

            story.append(Spacer(1, 0.2 * inch))
        
        # Violation section
        if 'violation' in data and data['violation']:
            story.append(Paragraph("Violation Details", heading_style))
            violation = data['violation']
            violation_data = [
                ['Violation ID:', violation.get('id', 'N/A')],
                ['Type:', violation.get('type', 'N/A')],
                ['Severity:', str(violation.get('severity', 'N/A'))],
                ['Status:', violation.get('status', 'N/A')],
            ]
            
            violation_table = Table(violation_data, colWidths=[2*inch, 4*inch])
            violation_table.setStyle(TableStyle([
                ('BACKGROUND', (0, 0), (0, -1), colors.HexColor('#f7fafc')),
                ('TEXTCOLOR', (0, 0), (-1, -1), colors.black),
                ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
                ('FONTNAME', (0, 0), (0, -1), 'Helvetica-Bold'),
                ('FONTSIZE', (0, 0), (-1, -1), 10),
                ('BOTTOMPADDING', (0, 0), (-1, -1), 8),
                ('TOPPADDING', (0, 0), (-1, -1), 8),
                ('GRID', (0, 0), (-1, -1), 1, colors.grey),
            ]))
            story.append(violation_table)
            story.append(Spacer(1, 0.3*inch))

        # Footer
        story.append(Spacer(1, 0.5*inch))
        footer_text = f"Generated by ForeSyte System | {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}"
        story.append(Paragraph(footer_text, styles['Normal']))
        
        # Build PDF
        doc.build(story)
        logger.info("generate_pdf_report: success, path=%s", full_path)
        return True
        
    except Exception as e:
        logger.exception("generate_pdf_report failed: file_path=%s, error=%s", file_path, e)
        return False


def _invigilator_activity_is_violation_row(act: InvigilatorActivity) -> bool:
    """Used for 'violations only' invigilator report modes (exclude routine/normal-only rows)."""
    t = (act.activity_type or "").strip().lower()
    sev = (act.severity or "").strip().lower()
    if sev in ("medium", "high", "critical"):
        return True
    if any(k in t for k in ("phone", "wave", "paper", "alert", "suspicious")):
        return True
    if t in ("normal", "idle", "sitting") or t.startswith("normal"):
        return False
    return bool(t)


def generate_invigilator_pdf_report(data: Dict[str, Any], file_path: str) -> bool:
    """PDF for invigilator activity list; each row links to the stored detection frame (R2/blob)."""
    full_path = REPORTS_DIR / Path(file_path).name
    logger.info("generate_invigilator_pdf_report: starting, full_path=%s", full_path)
    try:
        if not str(full_path).lower().endswith(".pdf"):
            full_path = full_path.with_suffix(".pdf")
        if not REPORTLAB_AVAILABLE:
            return False
        _pg_w, _pg_h = landscape(A4)
        _margin_pt = 48
        _usable_w_pt = _pg_w - 2 * _margin_pt

        doc = SimpleDocTemplate(
            str(full_path),
            pagesize=landscape(A4),
            rightMargin=_margin_pt,
            leftMargin=_margin_pt,
            topMargin=_margin_pt,
            bottomMargin=44,
        )
        story = []
        styles = getSampleStyleSheet()
        title_style = ParagraphStyle(
            "InvigTitle",
            parent=styles["Heading1"],
            fontSize=20,
            textColor=colors.HexColor("#6e5ae6"),
            spaceAfter=16,
            alignment=TA_CENTER,
            fontName="Helvetica-Bold",
        )
        heading_style = ParagraphStyle(
            "InvigHeading",
            parent=styles["Heading2"],
            fontSize=14,
            textColor=colors.HexColor("#4a5568"),
            spaceAfter=8,
            spaceBefore=4,
            fontName="Helvetica-Bold",
        )
        story.append(Paragraph(data.get("title", "Invigilator Activity Report"), title_style))
        story.append(Spacer(1, 0.15 * inch))
        summary = data.get("summary") or {}
        meta_rows = [
            ["Generated:", datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")],
            ["Rows:", str(summary.get("total_activities", 0))],
            ["Report mode:", str(data.get("report_mode", "N/A"))],
        ]
        meta_table = Table(meta_rows, colWidths=[1.8 * inch, 8.2 * inch])
        meta_table.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#f7fafc")),
                    ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
                    ("FONTSIZE", (0, 0), (-1, -1), 9),
                ]
            )
        )
        story.append(meta_table)
        story.append(Spacer(1, 0.2 * inch))
        rows_data = data.get("invigilator_activities") or []
        if not rows_data:
            story.append(Paragraph("No invigilator activities matched the selected filters.", styles["Normal"]))
        else:
            story.append(Paragraph("Activities (object-storage frame per row)", heading_style))
            story.append(Spacer(1, 0.06 * inch))
            pdf_cell_style = ParagraphStyle(
                "InvigPdfCell",
                parent=styles["Normal"],
                fontName="Helvetica",
                fontSize=6,
                leading=9,
                alignment=TA_LEFT,
                wordWrap="LTR",
            )
            pdf_header_style = ParagraphStyle(
                "InvigPdfHdr",
                parent=styles["Normal"],
                fontName="Helvetica-Bold",
                fontSize=6,
                leading=9,
                alignment=TA_LEFT,
                textColor=colors.whitesmoke,
                wordWrap="LTR",
            )
            _inv_cw = [
                _usable_w_pt * 0.09,
                _usable_w_pt * 0.11,
                _usable_w_pt * 0.17,
                _usable_w_pt * 0.07,
                _usable_w_pt * 0.11,
                _usable_w_pt * 0.07,
                _usable_w_pt * 0.38,
            ]
            _row_pad_inv = TableStyle(
                [
                    ("ALIGN", (0, 0), (-1, -1), "LEFT"),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("LEFTPADDING", (0, 0), (-1, -1), 3),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 3),
                    ("TOPPADDING", (0, 0), (-1, -1), 4),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                    ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
                ]
            )
            header_row = [
                [
                    _pdf_paragraph_cell("Time", pdf_header_style),
                    _pdf_paragraph_cell("Invigilator", pdf_header_style),
                    _pdf_paragraph_cell("Activity", pdf_header_style),
                    _pdf_paragraph_cell("Severity", pdf_header_style),
                    _pdf_paragraph_cell("Exam", pdf_header_style),
                    _pdf_paragraph_cell("Room", pdf_header_style),
                    _pdf_paragraph_cell("Evidence (R2)", pdf_header_style),
                ]
            ]
            hdr_tbl = Table(header_row, colWidths=_inv_cw)
            hs = TableStyle(list(_row_pad_inv.getCommands()))
            hs.add("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#6e5ae6"))
            hdr_tbl.setStyle(hs)
            story.append(hdr_tbl)

            for idx, r in enumerate(rows_data[:200]):
                ev_url = str(r.get("report_evidence_url") or r.get("evidence_url") or "").strip()
                if ev_url and not ev_url.startswith(("http://", "https://")):
                    ev_cell = _pdf_paragraph_cell(ev_url[:120], pdf_cell_style, soft_wrap_chars=80)
                elif ev_url:
                    ev_cell = _pdf_link_cell(
                        ev_url,
                        pdf_cell_style,
                        display_text="open",
                        soft_wrap_chars=52,
                    )
                else:
                    ev_cell = _pdf_paragraph_cell("N/A", pdf_cell_style)

                bg = colors.white if idx % 2 == 0 else colors.HexColor("#f7fafc")
                row_tbl = Table(
                    [
                        [
                            _pdf_paragraph_cell(str(r.get("timestamp", ""))[:19], pdf_cell_style),
                            _pdf_paragraph_cell(
                                str(r.get("invigilator_name", ""))[:42],
                                pdf_cell_style,
                                soft_wrap_chars=28,
                            ),
                            _pdf_paragraph_cell(
                                str(r.get("activity_type", ""))[:56],
                                pdf_cell_style,
                                soft_wrap_chars=48,
                            ),
                            _pdf_paragraph_cell(str(r.get("severity", ""))[:14], pdf_cell_style),
                            _pdf_paragraph_cell(
                                str(r.get("exam_name", ""))[:40],
                                pdf_cell_style,
                                soft_wrap_chars=28,
                            ),
                            _pdf_paragraph_cell(str(r.get("room_label", ""))[:22], pdf_cell_style),
                            ev_cell,
                        ]
                    ],
                    colWidths=_inv_cw,
                )
                rs = TableStyle(list(_row_pad_inv.getCommands()))
                rs.add("BACKGROUND", (0, 0), (-1, 0), bg)
                row_tbl.setStyle(rs)
                story.append(row_tbl)

            if len(rows_data) > 200:
                story.append(
                    Spacer(1, 0.08 * inch),
                )
                story.append(
                    Paragraph(
                        _xml_escape(f"… {len(rows_data) - 200} more rows not shown"),
                        styles["Normal"],
                    ),
                )
        story.append(Spacer(1, 0.3 * inch))
        story.append(
            Paragraph(
                f"Generated by ForeSyte | {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}",
                styles["Normal"],
            )
        )
        doc.build(story)
        logger.info("generate_invigilator_pdf_report: success, path=%s", full_path)
        return True
    except Exception as e:
        logger.exception("generate_invigilator_pdf_report failed: %s", e)
        return False


async def generate_invigilator_report_file_async(
    report_id: UUID,
    file_path: str,
    format_type: str,
    report_mode: str,
    invigilator_id: Optional[UUID],
    exam_id: Optional[UUID],
):
    """Background task: invigilator activity export (JSON/CSV/PDF)."""
    db = SessionLocal()
    try:
        q = db.query(InvigilatorActivity).join(Room, InvigilatorActivity.room_id == Room.room_id)

        if report_mode == "all_invigilators_violations":
            pass
        elif report_mode == "single_exam_detailed":
            if not invigilator_id or not exam_id:
                q = q.filter(sql_false())
            else:
                room_ids = [rid for (rid,) in db.query(Room.room_id).filter(Room.exam_id == exam_id).all()]
                if not room_ids:
                    q = q.filter(sql_false())
                else:
                    q = q.filter(
                        InvigilatorActivity.invigilator_id == invigilator_id,
                        InvigilatorActivity.room_id.in_(room_ids),
                    )
        elif report_mode == "single_all_exams_violations":
            if not invigilator_id:
                q = q.filter(sql_false())
            else:
                room_ids = [
                    rid
                    for (rid,) in db.query(ExamRoomAssignment.room_id)
                    .filter(ExamRoomAssignment.invigilator_id == invigilator_id)
                    .distinct()
                    .all()
                ]
                if not room_ids:
                    q = q.filter(sql_false())
                else:
                    q = q.filter(
                        InvigilatorActivity.invigilator_id == invigilator_id,
                        InvigilatorActivity.room_id.in_(room_ids),
                    )
        else:
            q = q.filter(sql_false())

        activities = q.order_by(InvigilatorActivity.timestamp.asc()).limit(5000).all()

        violations_only = report_mode in ("all_invigilators_violations", "single_all_exams_violations")
        if violations_only:
            activities = [a for a in activities if _invigilator_activity_is_violation_row(a)]

        invigilator_rows: List[Dict[str, Any]] = []
        for act in activities:
            inv = None
            if act.invigilator_id:
                inv = db.query(Invigilator).filter(Invigilator.invigilator_id == act.invigilator_id).first()
            room = db.query(Room).filter(Room.room_id == act.room_id).first()
            exam = None
            if room and room.exam_id:
                exam = db.query(Exam).filter(Exam.exam_id == room.exam_id).first()
            room_label = ""
            if room:
                room_label = f"{room.block}-{room.room_number}" if room.block else str(room.room_number)
            invigilator_rows.append(
                {
                    "activity_id": str(act.activity_id),
                    "invigilator_name": inv.name if inv else "",
                    "activity_type": act.activity_type or "",
                    "timestamp": act.timestamp.strftime("%Y-%m-%d %H:%M:%S") if act.timestamp else "",
                    "severity": act.severity or "",
                    "confidence": f"{act.confidence * 100:.1f}%" if act.confidence is not None else "",
                    "exam_name": exam.course if exam else "",
                    "room_label": room_label,
                    "evidence_url": act.evidence_url or "",
                    "report_evidence_url": getattr(act, "report_evidence_url", "") or "",
                    "notes": act.notes or "",
                }
            )

        report_data: Dict[str, Any] = {
            "report_id": str(report_id),
            "title": "Invigilator Activity Report",
            "generated_at": datetime.utcnow().isoformat(),
            "report_type": "invigilator",
            "report_mode": report_mode,
            "summary": {
                "total_activities": len(invigilator_rows),
            },
            "invigilator_activities": invigilator_rows,
        }

        fmt = format_type.lower()
        success = False
        if fmt == "json":
            success = generate_json_report(report_data, file_path)
        elif fmt == "csv":
            success = generate_csv_report(report_data, file_path)
        elif fmt == "pdf":
            success = generate_invigilator_pdf_report(report_data, file_path)
        else:
            success = generate_json_report(report_data, file_path)

        report = db.query(Report).filter(Report.report_id == report_id).first()
        if report:
            if success:
                report.status = "completed"
                base_name = Path(file_path).stem
                possible_extensions = [".pdf", ".csv", ".json"] if fmt == "pdf" else [f".{fmt}"] if fmt in ("csv", "json") else [".json"]
                actual_file = None
                for ext in possible_extensions:
                    test_file = REPORTS_DIR / f"{base_name}{ext}"
                    if test_file.exists():
                        actual_file = test_file
                        break
                if actual_file:
                    try:
                        upload_report_file(actual_file)
                    except Exception as upload_exc:
                        logger.warning("generate_invigilator_report_file_async: blob upload failed for %s: %s", actual_file, upload_exc)
                    report.file_path = f"/reports/{actual_file.name}"
                else:
                    report.status = "failed"
            else:
                report.status = "failed"
            db.commit()
        else:
            logger.error("generate_invigilator_report_file_async: report not found report_id=%s", report_id)
    except Exception as e:
        logger.exception("generate_invigilator_report_file_async failed: %s", e)
        try:
            report = db.query(Report).filter(Report.report_id == report_id).first()
            if report:
                report.status = "failed"
                db.commit()
        except Exception:
            pass
    finally:
        db.close()


async def generate_report_file_async(
    report_id: UUID,
    report_type: str,
    file_path: str,
    format_type: str,
    activities: List[StudentActivity] = None,
    exam: Exam = None,
    violation: Violation = None
):
    """Background task to generate the actual report file with detailed information."""
    logger.info(
        "generate_report_file_async: start report_id=%s report_type=%s file_path=%s format=%s activities_count=%s exam=%s has_violation=%s REPORTS_DIR=%s exists=%s",
        report_id, report_type, file_path, format_type,
        len(activities) if activities else 0,
        exam.exam_id if exam else None,
        violation is not None,
        REPORTS_DIR.resolve(),
        REPORTS_DIR.exists(),
    )
    db = SessionLocal()
    try:
        from database.models import Student, Violation as ViolationModel
        
        # Prepare comprehensive report data
        report_data = {
            'report_id': str(report_id),
            'title': f"{report_type.title()} Report",
            'generated_at': datetime.utcnow().isoformat(),
            'report_type': report_type,
            'summary': {}
        }
        
        # Collect detailed violation information
        violations_list = []
        unique_students = set()
        severity_counts = {'low': 0, 'medium': 0, 'high': 0, 'critical': 0}
        
        if activities:
            # Build detailed activities with student info and violations
            detailed_activities = []
            
            for act in activities:
                # Get student information
                student = db.query(Student).filter(Student.student_id == act.student_id).first()
                student_name = f"{student.name}" if student else "Unknown Student"
                student_roll = student.roll_number if student else "N/A"
                unique_students.add(str(act.student_id))
                
                # Get associated violation
                act_violation = db.query(ViolationModel).filter(
                    ViolationModel.activity_id == act.activity_id
                ).first()
                
                # Determine severity category
                if act.severity:
                    if act.severity in ['low', 'medium', 'high', 'critical']:
                        severity_counts[act.severity] += 1
                    elif isinstance(act.severity, int):
                        if act.severity <= 1:
                            severity_counts['low'] += 1
                        elif act.severity == 2:
                            severity_counts['medium'] += 1
                        elif act.severity == 3:
                            severity_counts['high'] += 1
                        else:
                            severity_counts['critical'] += 1
                
                activity_detail = {
                    'activity_id': str(act.activity_id),
                    'activity_type': act.activity_type or 'Unknown',
                    'timestamp': act.timestamp.strftime('%Y-%m-%d %H:%M:%S') if act.timestamp else '',
                    'student_id': str(act.student_id) if act.student_id else '',
                    'student_name': student_name,
                    'student_roll_number': student_roll,
                    'severity': str(act.severity) if act.severity else 'N/A',
                    'confidence': f"{act.confidence * 100:.1f}%" if act.confidence else 'N/A',
                    'evidence_url': act.evidence_url or 'N/A',
                    'report_evidence_url': getattr(act, "report_evidence_url", None) or 'N/A',
                    'identification_evidence_url': getattr(act, "identification_evidence_url", None) or 'N/A',
                    'description': (
                        f"{act.activity_type} at "
                        f"{act.timestamp.strftime('%H:%M:%S') if act.timestamp else 'unknown time'}"
                    ),
                }
                
                # Add violation information if exists
                if act_violation:
                    activity_detail['violation'] = {
                        'violation_id': str(act_violation.violation_id),
                        'type': act_violation.violation_type or 'N/A',
                        'severity': act_violation.severity or 0,
                        'status': act_violation.status or 'pending',
                        'timestamp': act_violation.timestamp.strftime('%Y-%m-%d %H:%M:%S') if act_violation.timestamp else '',
                    }
                    violations_list.append(activity_detail['violation'])
                else:
                    activity_detail['violation'] = None
                
                detailed_activities.append(activity_detail)
            # Highest severity first, then roll number; timestamp breaks ties
            detailed_activities.sort(
                key=lambda row: (
                    -_report_row_severity_rank(row),
                    str(row.get("student_roll_number") or "").lower(),
                    str(row.get("timestamp") or ""),
                )
            )
            
            report_data['activities'] = detailed_activities
            report_data['summary']['total_activities'] = len(activities)
            report_data['summary']['total_violations'] = len(violations_list)
            report_data['summary']['unique_students_flagged'] = len(unique_students)
            report_data['summary']['severity_breakdown'] = severity_counts
            logger.info("generate_report_file_async: built %s activities for report_id=%s", len(detailed_activities), report_id)
        else:
            logger.info("generate_report_file_async: no activities, report_id=%s", report_id)
        
        if exam:
            report_data['exam'] = {
                'id': str(exam.exam_id),
                'name': exam.course or 'Unknown',
                'course_code': exam.course or 'N/A',
                'date': exam.exam_date.strftime('%Y-%m-%d') if exam.exam_date else '',
                'start_time': exam.start_time.strftime('%H:%M:%S') if exam.start_time else 'N/A',
                'end_time': exam.end_time.strftime('%H:%M:%S') if exam.end_time else 'N/A',
            }
            report_data['summary']['exam_name'] = exam.course or 'Unknown'
            report_data['summary']['exam_date'] = exam.exam_date.strftime('%Y-%m-%d') if exam.exam_date else 'N/A'
        
        if violation:
            report_data['primary_violation'] = {
                'id': str(violation.violation_id),
                'type': violation.violation_type or 'Unknown',
                'severity': violation.severity or 0,
                'status': violation.status or 'pending',
                'timestamp': violation.timestamp.strftime('%Y-%m-%d %H:%M:%S') if violation.timestamp else '',
                'evidence_url': violation.evidence_url or 'N/A'
            }
        
        logger.info(
            "generate_report_file_async: report_data ready report_id=%s summary_keys=%s activities_len=%s",
            report_id, list(report_data.get('summary', {}).keys()), len(report_data.get('activities', [])),
        )
        
        # Generate the file based on format
        success = False
        if format_type.lower() == 'json':
            success = generate_json_report(report_data, file_path)
        elif format_type.lower() == 'csv':
            success = generate_csv_report(report_data, file_path)
        elif format_type.lower() == 'pdf':
            success = generate_pdf_report(report_data, file_path)
        else:
            # Default to JSON
            logger.info("generate_report_file_async: unknown format %s, defaulting to JSON", format_type)
            success = generate_json_report(report_data, file_path)
        
        logger.info("generate_report_file_async: format generation finished report_id=%s success=%s", report_id, success)
        
        # Update report status
        report = db.query(Report).filter(Report.report_id == report_id).first()
        if report:
            if success:
                report.status = "completed"
                # Update file_path to actual generated file
                # Check what file was actually created
                base_name = Path(file_path).stem
                
                # Try to find the actual file
                possible_extensions = []
                if format_type.lower() == 'pdf':
                    possible_extensions = ['.pdf', '.txt']  # Fallback for PDF
                elif format_type.lower() == 'csv':
                    possible_extensions = ['.csv']
                elif format_type.lower() == 'json':
                    possible_extensions = ['.json']
                
                actual_file = None
                for ext in possible_extensions:
                    test_file = REPORTS_DIR / f"{base_name}{ext}"
                    if test_file.exists():
                        actual_file = test_file
                        break
                
                if actual_file:
                    try:
                        upload_report_file(actual_file)
                    except Exception as upload_exc:
                        logger.warning("generate_report_file_async: blob upload failed for %s: %s", actual_file, upload_exc)
                    report.file_path = f"/reports/{actual_file.name}"
                    logger.info("generate_report_file_async: report completed report_id=%s file=%s", report_id, actual_file.name)
                else:
                    logger.warning("generate_report_file_async: generated file not found report_id=%s base_name=%s checked=%s", report_id, base_name, possible_extensions)
                    report.status = "failed"
            else:
                logger.warning("generate_report_file_async: generation returned False report_id=%s", report_id)
                report.status = "failed"
            db.commit()
        else:
            logger.error("generate_report_file_async: report record not found report_id=%s", report_id)
        
    except Exception as e:
        logger.exception("generate_report_file_async failed: report_id=%s error=%s", report_id, e)
        # Update status to failed
        try:
            report = db.query(Report).filter(Report.report_id == report_id).first()
            if report:
                report.status = "failed"
                db.commit()
                logger.info("generate_report_file_async: marked report_id=%s as failed", report_id)
        except Exception as commit_err:
            logger.exception("generate_report_file_async: failed to update report status to failed: %s", commit_err)
    finally:
        db.close()
        logger.info("generate_report_file_async: done report_id=%s", report_id)

# -------------------------
# Helper Functions
# -------------------------
def get_investigator_id_for_report(current_user: dict, db: Session) -> UUID:
    """
    Get the appropriate investigator_id for report generation.
    If user is an investigator, use their ID. If admin, use a default investigator.
    Always returns a valid investigator_id - creates a system investigator if needed.
    """
    user_type = current_user.get("user_type")
    user_id = current_user.get("id")
    
    logger.info("get_investigator_id_for_report: user_type=%s user_id=%s", user_type, user_id)
    
    if user_type == "investigator":
        try:
            investigator_id = UUID(user_id)
            # Verify the investigator exists
            investigator = db.query(Investigator).filter(Investigator.investigator_id == investigator_id).first()
            if not investigator:
                logger.warning("get_investigator_id_for_report: investigator %s not found, using default", investigator_id)
                # Fall through to create system investigator
            else:
                logger.info("get_investigator_id_for_report: using investigator_id=%s", investigator_id)
                return investigator_id
        except (ValueError, TypeError) as e:
            logger.warning("get_investigator_id_for_report: invalid investigator ID %s: %s", user_id, e)
            # Fall through to create system investigator
    
    # For admins OR if investigator not found, use/create a system investigator
    default_investigator = db.query(Investigator).first()
    
    if default_investigator:
        logger.info("get_investigator_id_for_report: using default investigator_id=%s", default_investigator.investigator_id)
        return default_investigator.investigator_id
    else:
        # Create a system investigator if none exists
        logger.info("get_investigator_id_for_report: no investigators found, creating system investigator")
        from database.auth import hash_password
        
        # Check again to avoid race condition (in case another request created one)
        default_investigator = db.query(Investigator).filter(Investigator.email == "system@foresyte.edu").first()
        if default_investigator:
            logger.info("get_investigator_id_for_report: system investigator exists id=%s", default_investigator.investigator_id)
            return default_investigator.investigator_id
        
        system_investigator = Investigator(
            name="System Investigator",
            email="system@foresyte.edu",
            designation="System",
            password_hash=hash_password("System123!")
        )
        db.add(system_investigator)
        db.commit()
        db.refresh(system_investigator)
        logger.info("get_investigator_id_for_report: created system investigator id=%s", system_investigator.investigator_id)
        return system_investigator.investigator_id

# -------------------------
# Pydantic Schemas
# -------------------------
class ReportCreate(BaseModel):
    report_type: str
    file_path: str
    violation_id: UUID
    generated_by: UUID
    status: Optional[str] = "generating"  # Default to generating - reports need async processing


class ReportRead(BaseModel):
    report_id: UUID
    name: Optional[str] = None  # User-defined display name
    report_type: str
    generated_date: date
    file_path: str
    violation_id: Optional[UUID] = None  # Made optional since reports can exist without violations
    generated_by: UUID
    status: str = "generating"  # generating, completed, failed

    model_config = {
        "from_attributes": True
    }

    @computed_field
    @property
    def blob_url(self) -> Optional[str]:
        return _report_blob_url_for_path(self.file_path)


class ReportUpdate(BaseModel):
    name: Optional[str] = None  # Rename report
    report_type: Optional[str] = None
    file_path: Optional[str] = None
    violation_id: Optional[UUID] = None
    generated_by: Optional[UUID] = None
    status: Optional[str] = None  # completed, generating, failed


class ReportRenameRequest(BaseModel):
    name: str  # New display name for the report


class IncidentReportRequest(BaseModel):
    incident_ids: List[str]
    format: str  # pdf, csv, json
    include_video_links: bool
    violation_view: Optional[str] = "all"  # "all" = every violation row; "summary" = per-student rows (student, type, severity, frequency + evidence links)


class ExamReportRequest(BaseModel):
    format: str  # pdf, csv, json
    include_statistics: bool
    violation_view: Optional[str] = "all"  # "all" | "summary"


class StudentReportRequest(BaseModel):
    format: str  # pdf, csv, json
    include_statistics: Optional[bool] = True
    violation_view: Optional[str] = "all"  # "all" | "summary"


class InvigilatorReportRequest(BaseModel):
    format: str  # pdf, csv, json
    include_statistics: Optional[bool] = True
    invigilator_id: Optional[UUID] = None
    exam_id: Optional[UUID] = None
    report_mode: Optional[str] = "all_invigilators_violations"


class ReportListItem(BaseModel):
    """Report row for list endpoint: includes generated_by_email for display."""
    report_id: UUID
    name: Optional[str] = None
    report_type: str
    generated_date: date
    file_path: str
    violation_id: Optional[UUID] = None
    generated_by: UUID
    generated_by_email: Optional[str] = None
    status: str = "generating"

    model_config = {"from_attributes": True}

    @computed_field
    @property
    def blob_url(self) -> Optional[str]:
        return _report_blob_url_for_path(self.file_path)


class ReportListResponse(BaseModel):
    reports: List[ReportListItem]
    total: int


# -------------------------
# CRUD Routes
# -------------------------

# CREATE (Admin Only)
@router.post("/", response_model=ReportRead, status_code=status.HTTP_201_CREATED)
def create_report(
    report: ReportCreate,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    """
    Only admins can create reports.
    """
    if current_user.get("user_type") != "admin":
        raise HTTPException(status_code=403, detail="Only admins can create reports")

    # Validate violation
    violation = db.query(Violation).filter(Violation.violation_id == report.violation_id).first()
    if not violation:
        raise HTTPException(status_code=404, detail="Violation not found")

    # Validate investigator
    investigator = db.query(Investigator).filter(Investigator.investigator_id == report.generated_by).first()
    if not investigator:
        raise HTTPException(status_code=404, detail="Investigator not found")

    report_dict = report.dict()
    # Ensure status is set if not provided
    if 'status' not in report_dict:
        report_dict['status'] = 'generating'
    new_report = Report(**report_dict)
    db.add(new_report)
    db.commit()
    db.refresh(new_report)
    return new_report


# -------------------------
# Generate Incident Report
# -------------------------
@router.post("/incidents")
def generate_incident_report(
    request: IncidentReportRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    """
    Generate an incident report for specified incidents.
    """
    logger.info("generate_incident_report: request incident_ids=%s format=%s", request.incident_ids, request.format)
    if current_user.get("user_type") not in ["admin", "investigator"]:
        raise HTTPException(status_code=403, detail="Access denied")

    # Validate incident IDs
    activities = []
    for incident_id in request.incident_ids:
        try:
            activity = db.query(StudentActivity).filter(
                StudentActivity.activity_id == UUID(incident_id)
            ).first()
            if activity:
                activities.append(activity)
        except ValueError as e:
            logger.warning("generate_incident_report: invalid incident_id=%s %s", incident_id, e)
            continue

    if not activities:
        logger.warning("generate_incident_report: no valid incidents found for ids=%s", request.incident_ids)
        raise HTTPException(status_code=404, detail="No valid incidents found")

    # Generate report file (simplified - in production, use a proper report generator)
    report_id = UUID(current_user.get("id"))
    timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    filename = f"incident_report_{timestamp}.{request.format}"
    file_path = os.path.join("reports", filename)

    # Create report record
    # For now, we'll create a report linked to the first violation if available
    violation = None
    if activities:
        violation = db.query(Violation).filter(
            Violation.activity_id == activities[0].activity_id
        ).first()

    if not violation:
        # Create a placeholder violation if needed
        violation = Violation(
            activity_id=activities[0].activity_id,
            violation_type=activities[0].activity_type or "Unknown",
            severity=1,
            status="pending"
        )
        db.add(violation)
        db.commit()
        db.refresh(violation)

    # Get investigator_id for report (handles both admin and investigator users)
    investigator_id = get_investigator_id_for_report(current_user, db)
    
    initial_name = f"Incident Report - {datetime.utcnow().strftime('%Y-%m-%d %H:%M')}"
    new_report = Report(
        name=initial_name,
        report_type="incident",
        file_path=file_path,
        violation_id=violation.violation_id,
        generated_by=investigator_id,
        status="generating"  # Will be updated to "completed" by background task
    )
    db.add(new_report)
    db.commit()
    db.refresh(new_report)

    # Start background task to generate the actual report file
    logger.info(
        "generate_incident_report: adding background task report_id=%s file_path=%s format=%s",
        new_report.report_id, file_path, request.format,
    )
    background_tasks.add_task(
        generate_report_file_async,
        report_id=new_report.report_id,
        report_type="incident",
        file_path=file_path,
        format_type=request.format,
        activities=activities,
        violation=violation
    )

    # Return report URL or file path
    return {
        "id": str(new_report.report_id),
        "file_path": file_path,
        "format": request.format,
        "status": "generating"
    }


# -------------------------
# Generate Exam Report
# -------------------------
@router.post("/exams/{exam_id}")
def generate_exam_report(
    exam_id: UUID,
    request: ExamReportRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    """
    Generate a report for a specific exam.
    """
    logger.info("generate_exam_report: exam_id=%s format=%s", exam_id, request.format)
    if current_user.get("user_type") not in ["admin", "investigator"]:
        raise HTTPException(status_code=403, detail="Access denied")

    exam = db.query(Exam).filter(Exam.exam_id == exam_id).first()
    if not exam:
        logger.warning("generate_exam_report: exam not found exam_id=%s", exam_id)
        raise HTTPException(status_code=404, detail="Exam not found")

    # Generate report file
    timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    filename = f"exam_report_{exam_id}_{timestamp}.{request.format}"
    file_path = os.path.join("reports", filename)

    # Get violations/incidents for this exam
    activities = db.query(StudentActivity).filter(
        StudentActivity.exam_id == exam_id
    ).all()
    activities = [
        a for a in activities
        if is_supported_cheating_activity_type(a.activity_type or "")
    ]
    logger.info("generate_exam_report: exam_id=%s activities_count=%s", exam_id, len(activities))

    violation = None
    if activities:
        violation = db.query(Violation).filter(
            Violation.activity_id == activities[0].activity_id
        ).first()

    if not violation and activities:
        violation = Violation(
            activity_id=activities[0].activity_id,
            violation_type="Exam Report",
            severity=1,
            status="pending"
        )
        db.add(violation)
        db.commit()
        db.refresh(violation)

    # Get investigator_id for report (handles both admin and investigator users)
    investigator_id = get_investigator_id_for_report(current_user, db)

    initial_name = f"Exam Report - {exam.course or 'Exam'} - {exam.exam_date.strftime('%Y-%m-%d') if exam.exam_date else 'N/A'}"
    new_report = Report(
        name=initial_name,
        report_type="exam",
        file_path=file_path,
        violation_id=violation.violation_id if violation else None,
        generated_by=investigator_id,
        status="generating"  # Will be updated to "completed" by background task
    )
    db.add(new_report)
    db.commit()
    db.refresh(new_report)

    # Start background task to generate the actual report file
    logger.info(
        "generate_exam_report: adding background task report_id=%s file_path=%s format=%s violation_id=%s",
        new_report.report_id, file_path, request.format, new_report.violation_id,
    )
    background_tasks.add_task(
        generate_report_file_async,
        report_id=new_report.report_id,
        report_type="exam",
        file_path=file_path,
        format_type=request.format,
        activities=activities,
        exam=exam,
        violation=violation
    )

    return {
        "id": str(new_report.report_id),
        "file_path": file_path,
        "format": request.format,
        "status": "generating"
    }


# -------------------------
# Invigilator report: dropdown options + generate (must be before GET /{report_id})
# -------------------------
@router.get("/invigilators/options")
def get_invigilator_report_options(
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    """List invigilators for the invigilator report dropdown."""
    user_type = current_user.get("user_type")
    user_id = current_user.get("id")
    if user_type not in ["admin", "investigator", "invigilator"]:
        raise HTTPException(status_code=403, detail="Access denied")

    # Invigilator users can only select themselves.
    if user_type == "invigilator":
        try:
            iid = UUID(str(user_id))
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid invigilator token id")
        me = db.query(Invigilator).filter(Invigilator.invigilator_id == iid).first()
        return {
            "invigilators": (
                [{"invigilator_id": str(me.invigilator_id), "name": me.name, "email": me.email}]
                if me else []
            )
        }

    rows = db.query(Invigilator).order_by(Invigilator.name).all()
    return {
        "invigilators": [
            {"invigilator_id": str(r.invigilator_id), "name": r.name, "email": r.email}
            for r in rows
        ]
    }


@router.get("/invigilators/{invigilator_id}/exam-options")
def get_invigilator_exam_report_options(
    invigilator_id: UUID,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    """Exams this invigilator is assigned to (for single-exam detailed report)."""
    user_type = current_user.get("user_type")
    user_id = current_user.get("id")
    if user_type not in ["admin", "investigator", "invigilator"]:
        raise HTTPException(status_code=403, detail="Access denied")
    # Invigilator users can only request their own exam options.
    if user_type == "invigilator":
        try:
            invigilator_id = UUID(str(user_id))
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid invigilator token id")

    exam_ids = (
        db.query(ExamRoomAssignment.exam_id)
        .filter(ExamRoomAssignment.invigilator_id == invigilator_id)
        .distinct()
        .all()
    )
    eids = [e[0] for e in exam_ids]
    if not eids:
        return []
    exams = db.query(Exam).filter(Exam.exam_id.in_(eids)).all()
    exams.sort(key=lambda e: (e.exam_date or date.min, e.course or ""), reverse=True)
    return [
        {
            "exam_id": str(e.exam_id),
            "id": str(e.exam_id),
            "name": e.course or "Exam",
            "exam_date": e.exam_date.isoformat() if e.exam_date else None,
        }
        for e in exams
    ]


@router.post("/invigilators")
def generate_invigilator_report(
    request: InvigilatorReportRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    """Generate invigilator activity report (CSV/JSON/PDF)."""
    user_type = current_user.get("user_type")
    user_id = current_user.get("id")
    if user_type not in ["admin", "investigator", "invigilator"]:
        raise HTTPException(status_code=403, detail="Access denied")

    mode = request.report_mode or "all_invigilators_violations"
    # Invigilators can only generate reports about themselves.
    if user_type == "invigilator":
        try:
            my_id = UUID(str(user_id))
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid invigilator token id")
        if request.invigilator_id and request.invigilator_id != my_id:
            raise HTTPException(status_code=403, detail="Cannot generate report for another invigilator")
        request.invigilator_id = my_id
        # For invigilator role, default to their all-exams violations unless user chose single exam.
        if mode == "all_invigilators_violations":
            mode = "single_all_exams_violations"

    if mode in ("single_exam_detailed", "single_all_exams_violations") and not request.invigilator_id:
        raise HTTPException(status_code=400, detail="invigilator_id is required for this report mode")
    if mode == "single_exam_detailed" and not request.exam_id:
        raise HTTPException(status_code=400, detail="exam_id is required for single exam detailed report")

    investigator_id = get_investigator_id_for_report(current_user, db)
    timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    filename = f"invigilator_report_{timestamp}.{request.format}"
    file_path = os.path.join("reports", filename)
    initial_name = f"Invigilator Report - {datetime.utcnow().strftime('%Y-%m-%d %H:%M')}"

    new_report = Report(
        name=initial_name,
        report_type="invigilator",
        file_path=file_path,
        violation_id=None,
        generated_by=investigator_id,
        status="generating",
    )
    db.add(new_report)
    db.commit()
    db.refresh(new_report)

    background_tasks.add_task(
        generate_invigilator_report_file_async,
        report_id=new_report.report_id,
        file_path=file_path,
        format_type=request.format,
        report_mode=mode,
        invigilator_id=request.invigilator_id,
        exam_id=request.exam_id,
    )

    return {
        "id": str(new_report.report_id),
        "file_path": file_path,
        "format": request.format,
        "status": "generating",
    }


# READ All (Admin + Investigator)
@router.get("/", response_model=ReportListResponse)
def get_all_reports(
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    """
    Admins and Investigators can view all reports with pagination.
    Returns generated_by_email for display (By: email).
    """
    if current_user.get("user_type") not in ["admin", "investigator"]:
        raise HTTPException(status_code=403, detail="Access denied")

    query = db.query(Report)
    total = query.count()

    offset = (page - 1) * limit
    reports = query.order_by(Report.generated_date.desc()).offset(offset).limit(limit).all()

    items = []
    for report in reports:
        investigator = db.query(Investigator).filter(Investigator.investigator_id == report.generated_by).first()
        generated_by_email = investigator.email if investigator else None
        items.append(ReportListItem(
            report_id=report.report_id,
            name=report.name,
            report_type=report.report_type,
            generated_date=report.generated_date,
            file_path=report.file_path or "",
            violation_id=report.violation_id,
            generated_by=report.generated_by,
            generated_by_email=generated_by_email,
            status=report.status or "generating",
        ))

    return ReportListResponse(reports=items, total=total)


# READ by ID (Admin + Investigator)
@router.get("/{report_id}", response_model=ReportRead)
def get_report(
    report_id: UUID,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    """
    Admins and Investigators can view a specific report.
    """
    if current_user.get("user_type") not in ["admin", "investigator"]:
        raise HTTPException(status_code=403, detail="Access denied")

    report = db.query(Report).filter(Report.report_id == report_id).first()
    if not report:
        raise HTTPException(status_code=404, detail="Report not found")

    return report


# UPDATE (Admin Only)
@router.put("/{report_id}", response_model=ReportRead)
def update_report(
    report_id: UUID,
    updated: ReportUpdate,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    """
    Only admins can update reports.
    """
    if current_user.get("user_type") != "admin":
        raise HTTPException(status_code=403, detail="Only admins can update reports")

    report = db.query(Report).filter(Report.report_id == report_id).first()
    if not report:
        raise HTTPException(status_code=404, detail="Report not found")

    # Validate updated relations if provided
    if updated.violation_id:
        violation = db.query(Violation).filter(Violation.violation_id == updated.violation_id).first()
        if not violation:
            raise HTTPException(status_code=404, detail="Violation not found")

    if updated.generated_by:
        investigator = db.query(Investigator).filter(Investigator.investigator_id == updated.generated_by).first()
        if not investigator:
            raise HTTPException(status_code=404, detail="Investigator not found")

    for key, value in updated.dict(exclude_unset=True).items():
        setattr(report, key, value)

    db.commit()
    db.refresh(report)
    return report


# RENAME Report (Admin + Investigator)
@router.patch("/{report_id}/name", response_model=ReportRead)
def rename_report(
    report_id: UUID,
    body: ReportRenameRequest,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    """
    Rename a report. The new name is stored in the database.
    Admins and Investigators can rename reports.
    """
    if current_user.get("user_type") not in ["admin", "investigator"]:
        raise HTTPException(status_code=403, detail="Access denied")

    report = db.query(Report).filter(Report.report_id == report_id).first()
    if not report:
        raise HTTPException(status_code=404, detail="Report not found")

    new_name = (body.name or "").strip()
    if not new_name:
        raise HTTPException(status_code=400, detail="Report name cannot be empty")

    report.name = new_name
    db.commit()
    db.refresh(report)
    logger.info("rename_report: report_id=%s new_name=%s", report_id, new_name)
    return report


# DELETE (Admin Only)
@router.delete("/{report_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_report(
    report_id: UUID,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    """
    Only admins can delete reports.
    """
    if current_user.get("user_type") != "admin":
        raise HTTPException(status_code=403, detail="Only admins can delete reports")

    report = db.query(Report).filter(Report.report_id == report_id).first()
    if not report:
        raise HTTPException(status_code=404, detail="Report not found")

    db.delete(report)
    db.commit()
    return None


# UPDATE Report Status (for async report generation)
@router.patch("/{report_id}/status", response_model=ReportRead)
def update_report_status(
    report_id: UUID,
    new_status: str = Query(..., regex="^(generating|completed|failed)$"),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    """
    Update report status (used by async report generation tasks).
    Allows updating status from 'generating' to 'completed' or 'failed'.
    """
    if current_user.get("user_type") not in ["admin", "investigator"]:
        raise HTTPException(status_code=403, detail="Access denied")

    report = db.query(Report).filter(Report.report_id == report_id).first()
    if not report:
        raise HTTPException(status_code=404, detail="Report not found")

    report.status = new_status
    db.commit()
    db.refresh(report)
    return report


# DOWNLOAD Report File
@router.get("/{report_id}/download")
def download_report(
    report_id: UUID,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    """Download a report file."""
    report = db.query(Report).filter(Report.report_id == report_id).first()
    if not report:
        raise HTTPException(status_code=404, detail="Report not found")
    
    # Check if report is completed
    if report.status != "completed":
        raise HTTPException(
            status_code=400, 
            detail=f"Report is not ready for download. Current status: {report.status}"
        )
    
    # Get the file path - it's stored as /reports/filename.ext
    if not report.file_path:
        raise HTTPException(status_code=404, detail="Report file path not found")
    
    # Extract filename from path
    filename = Path(report.file_path).name
    remote_download = download_report_bytes(filename)
    if remote_download is not None:
        data, media_type = remote_download
        return Response(
            content=data,
            media_type=media_type,
            headers={
                "Content-Disposition": f"attachment; filename={filename}"
            },
        )

    file_full_path = REPORTS_DIR / filename
    
    # If file doesn't exist, try alternative extensions (for backwards compatibility)
    if not file_full_path.exists():
        # Try alternative extensions
        base_name = file_full_path.stem
        possible_extensions = ['.pdf', '.txt', '.csv', '.json']
        
        found = False
        for ext in possible_extensions:
            alternative_path = REPORTS_DIR / f"{base_name}{ext}"
            if alternative_path.exists():
                file_full_path = alternative_path
                filename = alternative_path.name
                found = True
                break
        
        if not found:
            raise HTTPException(
                status_code=404, 
                detail=f"Report file not found on server: {filename} (checked all extensions)"
            )
    
    # Determine media type based on file extension
    extension = file_full_path.suffix.lower()
    media_type_map = {
        '.pdf': 'application/pdf',
        '.csv': 'text/csv',
        '.json': 'application/json',
        '.txt': 'text/plain'
    }
    media_type = media_type_map.get(extension, 'application/octet-stream')
    
    # Return file as download
    return FileResponse(
        path=str(file_full_path),
        media_type=media_type,
        filename=filename,
        headers={
            "Content-Disposition": f"attachment; filename={filename}"
        }
    )

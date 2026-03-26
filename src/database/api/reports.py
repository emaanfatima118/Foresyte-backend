from fastapi import APIRouter, Depends, HTTPException, status, Query, BackgroundTasks
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session
from sqlalchemy.exc import ProgrammingError
from uuid import UUID
from datetime import datetime, date
from typing import List, Optional, Dict, Any
from pydantic import BaseModel
import os
import json
import csv
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

try:
    from reportlab.lib.pagesizes import letter, A4
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import inch
    from reportlab.lib import colors
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak
    from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
    REPORTLAB_AVAILABLE = True
except ImportError as e:
    REPORTLAB_AVAILABLE = False
    logger.warning("reportlab not installed: %s. PDF generation will create text files. Install with: pip install reportlab", e)

from database.db import get_db, SessionLocal
<<<<<<< HEAD
from database.models import Report, Violation, Investigator, StudentActivity, Exam
=======
from database.models import Report, Violation, Investigator, StudentActivity, Exam, Student, InvigilatorActivity, Invigilator, Room, ExamInvigilatorAssignment
>>>>>>> b003b4f5d912c3823aed770b8cd4e0da7e87090c
from database.auth import get_current_user
from database.severity_logic import severity_from_int

router = APIRouter(prefix="/reports", tags=["Reports"])

# -------------------------
# Report Generation Utilities
# -------------------------
REPORTS_DIR = Path("uploads/reports")
REPORTS_DIR.mkdir(parents=True, exist_ok=True)

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
            'primary_violation': data.get('primary_violation', None)
        }
        if data.get('report_type') == 'invigilator':
            json_data['invigilator_header_lines'] = data.get('invigilator_header_lines', [])
            json_data['invigilator_violations'] = data.get('invigilator_violations', [])
            json_data['invigilation_activity'] = data.get('invigilation_activity', [])
        
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
        elif 'activities' in data:
<<<<<<< HEAD
            for activity in data['activities']:
                violation_info = activity.get('violation', {})
                row = {
                    'Activity_ID': activity.get('activity_id', ''),
                    'Student_Name': activity.get('student_name', ''),
=======
            if data.get('report_type') == 'invigilator':
                viol = data.get('invigilator_violations') or []
                routine = data.get('invigilation_activity') or []
                for activity in viol + routine:
                    row = {
                        'Section': 'Violations' if activity.get('activity_category') == 'violation' else 'Invigilation activity',
                        'Invigilator_Name': activity.get('invigilator_name', ''),
                        'Exam': activity.get('exam_course', ''),
                        'Exam_Date': activity.get('exam_date', ''),
                        'Exam_Scheduled_Time': activity.get('exam_time_range', ''),
                        'Room': activity.get('room_name', ''),
                        'Timestamp': activity.get('timestamp', ''),
                        'Activity_Type': activity.get('activity_type', ''),
                        'Duration_Seconds': activity.get('duration_seconds', '') if activity.get('duration_seconds') is not None else '',
                        'Severity': activity.get('severity', '') or '',
                        'Notes': activity.get('notes', '') or '',
                    }
                    rows.append(row)
            else:
                for activity in data['activities']:
                    violation_info = activity.get('violation', {})
                    row = {
                        'Activity_ID': activity.get('activity_id', ''),
                        'Student_Name': activity.get('student_name', ''),
>>>>>>> b003b4f5d912c3823aed770b8cd4e0da7e87090c
                    'Roll_Number': activity.get('student_roll_number', ''),
                    'Activity_Type': activity.get('activity_type', ''),
                    'Timestamp': activity.get('timestamp', ''),
                    'Severity': activity.get('severity', ''),
                    'Confidence': activity.get('confidence', ''),
                    'Evidence_URL': activity.get('evidence_url', ''),
                    'Violation_ID': violation_info.get('violation_id', 'N/A') if violation_info else 'N/A',
                    'Violation_Type': violation_info.get('type', 'N/A') if violation_info else 'N/A',
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
        
        # Create PDF document
        doc = SimpleDocTemplate(str(full_path), pagesize=A4,
                               rightMargin=72, leftMargin=72,
                               topMargin=72, bottomMargin=18)
        
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
<<<<<<< HEAD
            metadata_data.append(['Exam:', exam_info.get('name', 'N/A')])
            metadata_data.append(['Exam Date:', exam_info.get('date', 'N/A')])
        
=======
            metadata_data.append(['Exam:', _cell_para(exam_info.get('name', 'N/A'))])
            metadata_data.append(['Exam Date:', _cell_para(exam_info.get('date', 'N/A'))])
        if data.get('report_type') == 'invigilator' and data.get('invigilator_header_lines'):
            for i, line in enumerate(data['invigilator_header_lines']):
                metadata_data.append([
                    'Invigilation context:' if i == 0 else '',
                    _cell_para(line),
                ])

>>>>>>> b003b4f5d912c3823aed770b8cd4e0da7e87090c
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
<<<<<<< HEAD
                summary_data.append(['Exam Date', summary['exam_date']])
            
            # Add severity breakdown if available
=======
                summary_data.append([_cell_para('Exam Date'), _cell_para(summary['exam_date'])])
            if 'invigilator_violations_count' in summary:
                summary_data.append([
                    _cell_para('Violations (policy)'),
                    _cell_para(str(summary['invigilator_violations_count'])),
                ])
            if 'invigilation_activity_count' in summary:
                summary_data.append([
                    _cell_para('Invigilation activity (non-violations)'),
                    _cell_para(str(summary['invigilation_activity_count'])),
                ])

            severity_breakdown_start_row = None
>>>>>>> b003b4f5d912c3823aed770b8cd4e0da7e87090c
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
        
        # Detailed Activities and Violations section
<<<<<<< HEAD
        if 'activities' in data and data['activities']:
            story.append(Paragraph("Detailed Violation Report", heading_style))
            story.append(Spacer(1, 0.1*inch))
=======
        if data.get('report_type') == 'invigilator':
                viol = data.get('invigilator_violations') or []
                routine = data.get('invigilation_activity') or []
                mode = data.get('invigilator_report_mode') or ''

                def _compact_row(act: Dict[str, Any], show_severity: bool) -> list:
                    ts = act.get('timestamp', 'N/A')
                    ts_disp = ts if not (isinstance(ts, str) and len(ts) > 22) else ts[:10] + '…' + ts[-8:]
                    dur = act.get('duration_seconds')
                    dur_s = f"{float(dur):.0f}" if dur is not None else '—'
                    row = [
                        _cell_para(ts_disp),
                        _cell_para(act.get('activity_type') or 'N/A'),
                        _cell_para(dur_s),
                    ]
                    if show_severity:
                        row.append(_cell_para(act.get('severity') or '—'))
                    row.append(_cell_para((act.get('notes') or '').strip() or '—'))
                    return row

                def _render_compact_table(title: str, items: List[Dict[str, Any]], *, show_severity: bool, header_bg: str):
                    story.append(Paragraph(title, heading_style))
                    story.append(Spacer(1, 0.08*inch))
                    hdr = [_cell_para('Observed'), _cell_para('Activity'), _cell_para('Dur(s)')]
                    widths = [1.05*inch, 1.65*inch, 0.6*inch]
                    if show_severity:
                        hdr.append(_cell_para('Severity'))
                        widths.append(0.8*inch)
                    hdr.append(_cell_para('Notes'))
                    widths.append(2.4*inch if show_severity else 3.2*inch)
                    rows = [hdr] + [_compact_row(a, show_severity) for a in items[:120]]
                    if len(items) > 120:
                        rows.append(['…', f'{len(items) - 120} more', '', '', ''][:len(hdr)])
                    t = Table(rows, colWidths=widths)
                    t.setStyle(TableStyle([
                        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor(header_bg)),
                        ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
                        ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
                        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
                        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
                        ('FONTSIZE', (0, 0), (-1, -1), 7),
                        ('BOTTOMPADDING', (0, 0), (-1, -1), 3),
                        ('TOPPADDING', (0, 0), (-1, -1), 3),
                        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor('#f7fafc')]),
                        ('GRID', (0, 0), (-1, -1), 0.5, colors.grey),
                    ]))
                    story.append(t)
                    story.append(Spacer(1, 0.2*inch))

                # Single invigilator + single exam: exam/invigilator details are already in metadata, so keep compact table only.
                if mode == 'single_exam_detailed':
                    # Explicit exam details table (shown once)
                    exam_name = (viol[0].get('exam_course') if viol else None) or (routine[0].get('exam_course') if routine else 'N/A')
                    exam_date = (viol[0].get('exam_date') if viol else None) or (routine[0].get('exam_date') if routine else 'N/A')
                    exam_time = (viol[0].get('exam_time_range') if viol else None) or (routine[0].get('exam_time_range') if routine else 'N/A')
                    room_name = (viol[0].get('room_name') if viol else None) or (routine[0].get('room_name') if routine else 'N/A')
                    inv_name = (viol[0].get('invigilator_name') if viol else None) or (routine[0].get('invigilator_name') if routine else 'N/A')
                    exam_info_data = [
                        [_cell_para('Invigilator'), _cell_para(inv_name)],
                        [_cell_para('Exam'), _cell_para(exam_name)],
                        [_cell_para('Exam Date'), _cell_para(exam_date)],
                        [_cell_para('Scheduled Time'), _cell_para(exam_time)],
                        [_cell_para('Room'), _cell_para(room_name)],
                    ]
                    exam_info_table = Table(exam_info_data, colWidths=[1.5*inch, 4.5*inch])
                    exam_info_table.setStyle(TableStyle([
                        ('BACKGROUND', (0, 0), (0, -1), colors.HexColor('#f7fafc')),
                        ('FONTNAME', (0, 0), (0, -1), 'Helvetica-Bold'),
                        ('GRID', (0, 0), (-1, -1), 0.5, colors.grey),
                        ('FONTSIZE', (0, 0), (-1, -1), 8),
                        ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
                        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
                    ]))
                    story.append(Paragraph("Exam Details", heading_style))
                    story.append(exam_info_table)
                    story.append(Spacer(1, 0.08*inch))
                    _render_compact_table("Violations", viol, show_severity=True, header_bg='#b91c1c')
                    if data.get('invigilator_include_routine', True):
                        _render_compact_table("Invigilation activity", routine, show_severity=False, header_bg='#6e5ae6')

                # Single invigilator + all exams: separate exam sections, avoid redundant columns.
                elif mode == 'single_all_exams_violations':
                    grouped: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
                    for a in viol:
                        ex = a.get('exam_course') or 'Unknown Exam'
                        dt = a.get('exam_date') or 'N/A'
                        tm = a.get('exam_time_range') or 'N/A'
                        rm = a.get('room_name') or 'N/A'
                        key = f"{ex} | {dt} | {tm} | Room {rm}"
                        grouped[key].append(a)
                    story.append(Paragraph("Violations by Exam", heading_style))
                    story.append(Spacer(1, 0.08*inch))
                    for section, items in grouped.items():
                        story.append(Paragraph(f"<b>{html.escape(section)}</b>", styles['Normal']))
                        story.append(Spacer(1, 0.05*inch))
                        # Exam details table for each exam section
                        first = items[0] if items else {}
                        sec_info = [
                            [_cell_para('Invigilator'), _cell_para(first.get('invigilator_name') or 'N/A')],
                            [_cell_para('Exam'), _cell_para(first.get('exam_course') or 'N/A')],
                            [_cell_para('Exam Date'), _cell_para(first.get('exam_date') or 'N/A')],
                            [_cell_para('Scheduled Time'), _cell_para(first.get('exam_time_range') or 'N/A')],
                            [_cell_para('Room'), _cell_para(first.get('room_name') or 'N/A')],
                        ]
                        sec_info_table = Table(sec_info, colWidths=[1.5*inch, 4.5*inch])
                        sec_info_table.setStyle(TableStyle([
                            ('BACKGROUND', (0, 0), (0, -1), colors.HexColor('#f7fafc')),
                            ('FONTNAME', (0, 0), (0, -1), 'Helvetica-Bold'),
                            ('GRID', (0, 0), (-1, -1), 0.5, colors.grey),
                            ('FONTSIZE', (0, 0), (-1, -1), 8),
                            ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
                            ('VALIGN', (0, 0), (-1, -1), 'TOP'),
                        ]))
                        story.append(sec_info_table)
                        story.append(Spacer(1, 0.05*inch))
                        _render_compact_table(" ", items, show_severity=True, header_bg='#b91c1c')

                # Default (all invigilators mode): split by invigilator + exam session with per-section headings/tables.
                else:
                    grouped: Dict[tuple, List[Dict[str, Any]]] = defaultdict(list)
                    for a in viol:
                        key = (
                            a.get('invigilator_name') or 'N/A',
                            a.get('exam_course') or 'Unknown Exam',
                            a.get('exam_date') or 'N/A',
                            a.get('exam_time_range') or 'N/A',
                            a.get('room_name') or 'N/A',
                        )
                        grouped[key].append(a)

                    story.append(Paragraph("Violations by Invigilator and Exam", heading_style))
                    story.append(Spacer(1, 0.08*inch))

                    for (inv_name, ex, dt, tm, rm), items in grouped.items():
                        story.append(Paragraph(f"<b>{html.escape(inv_name)} — {html.escape(ex)}</b>", styles['Normal']))
                        info_data = [
                            [_cell_para('Exam Date'), _cell_para(dt)],
                            [_cell_para('Scheduled Time'), _cell_para(tm)],
                            [_cell_para('Room'), _cell_para(rm)],
                        ]
                        info_table = Table(info_data, colWidths=[1.5*inch, 4.5*inch])
                        info_table.setStyle(TableStyle([
                            ('BACKGROUND', (0, 0), (0, -1), colors.HexColor('#f7fafc')),
                            ('FONTNAME', (0, 0), (0, -1), 'Helvetica-Bold'),
                            ('GRID', (0, 0), (-1, -1), 0.5, colors.grey),
                            ('FONTSIZE', (0, 0), (-1, -1), 8),
                            ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
                            ('VALIGN', (0, 0), (-1, -1), 'TOP'),
                        ]))
                        story.append(info_table)
                        story.append(Spacer(1, 0.06*inch))
                        _render_compact_table("Violations", items, show_severity=True, header_bg='#b91c1c')
        elif 'activities' in data and data['activities']:
            if data.get('violation_view') == 'summary' and data.get('aggregated_violations'):
                story.append(Paragraph("Violations by student (type, severity, frequency)", heading_style))
                story.append(Spacer(1, 0.1*inch))
                agg_headers = [[_cell_para('Student'), _cell_para('Roll No'), _cell_para('Violation Type'), _cell_para('Severity'), _cell_para('Frequency'), _cell_para('Evidence (video links)')]]
                agg_col_widths = [1.3*inch, 0.75*inch, 1.4*inch, 0.65*inch, 0.55*inch, 1.8*inch]
                for row in data['aggregated_violations']:
                    urls = row.get('evidence_urls') or []
                    links_str = ", ".join(str(u) for u in urls[:3]) if urls else "—"
                    agg_headers.append([
                        _cell_para(row.get('student_name') or 'N/A'),
                        _cell_para(row.get('student_roll_number') or 'N/A'),
                        _cell_para(row.get('activity_type') or 'N/A'),
                        (row.get('severity') or 'N/A'),
                        str(row.get('frequency', 0)),
                        _cell_para(links_str),
                    ])
                agg_style = [
                    ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#6e5ae6')),
                    ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
                    ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
                    ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
                    ('VALIGN', (0, 0), (-1, -1), 'TOP'),
                    ('FONTSIZE', (0, 0), (-1, -1), 8),
                    ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
                    ('TOPPADDING', (0, 0), (-1, -1), 4),
                    ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor('#f7fafc')]),
                    ('GRID', (0, 0), (-1, -1), 0.5, colors.grey),
                ]
                for r in range(1, len(agg_headers)):
                    if len(agg_headers[r]) > 3:
                        sev = (agg_headers[r][3] or "N/A").strip()
                        bg, tx = _severity_pdf_colors(sev)
                        agg_style.append(('BACKGROUND', (3, r), (3, r), colors.HexColor(bg)))
                        agg_style.append(('TEXTCOLOR', (3, r), (3, r), colors.HexColor(tx)))
                agg_table = Table(agg_headers, colWidths=agg_col_widths)
                agg_table.setStyle(TableStyle(agg_style))
                story.append(agg_table)
                story.append(Spacer(1, 0.3*inch))
            else:
                story.append(Paragraph("Detailed Violation Report", heading_style))
                story.append(Spacer(1, 0.1*inch))
>>>>>>> b003b4f5d912c3823aed770b8cd4e0da7e87090c
            
            # Create detailed table with violations
            activities_data = [[
                'Student',
                'Roll No',
                'Activity Type',
                'Time',
                'Severity',
                'Violation Type',
                'Status'
            ]]
            
<<<<<<< HEAD
            for activity in data['activities'][:100]:  # Show up to 100 activities
                violation_info = activity.get('violation', {})
                
                # Determine row color based on severity
                student_name = activity.get('student_name', 'Unknown')
                if len(student_name) > 20:
                    student_name = student_name[:17] + '...'
                
                activities_data.append([
                    student_name,
                    activity.get('student_roll_number', 'N/A'),
                    activity.get('activity_type', 'N/A'),
                    activity.get('timestamp', 'N/A')[-8:] if activity.get('timestamp') else 'N/A',  # Time only
                    str(activity.get('severity', 'N/A')),
                    violation_info.get('type', 'N/A') if violation_info else 'N/A',
                    violation_info.get('status', 'N/A') if violation_info else 'N/A'
                ])
=======
                for activity in data['activities'][:100]:  # Show up to 100 activities
                    violation_info = activity.get('violation', {})
                    student_name = activity.get('student_name', 'Unknown')
                    if len(student_name) > 20:
                        student_name = student_name[:17] + '...'
                    exam_display = (activity.get('exam_name') or '') + (' ' + (activity.get('exam_date') or '') if activity.get('exam_date') else '')
                    if len(exam_display) > 14:
                        exam_display = exam_display[:11] + '...'
                    ev_url = activity.get('evidence_url') or 'N/A'
                    if ev_url and ev_url != 'N/A' and (ev_url.startswith('http://') or ev_url.startswith('https://')):
                        evidence_cell = Paragraph(f'<a href="{ev_url}" color="#0066cc">View</a>', styles['Normal'])
                    else:
                        evidence_cell = ev_url[:8] + '...' if ev_url and len(str(ev_url)) > 8 else (ev_url or '-')
                    ts_raw = activity.get('timestamp', 'N/A')
                    if isinstance(ts_raw, str) and len(ts_raw) > 12:
                        time_str = ts_raw[-8:]
                    else:
                        time_str = str(ts_raw)
                    row = [
                        _cell_para(student_name),
                        _cell_para(activity.get('student_roll_number') or 'N/A'),
                        _cell_para(activity.get('activity_type') or 'N/A'),
                        time_str,
                        str(activity.get('severity', 'N/A')),
                        violation_info.get('type', 'N/A') if violation_info else 'N/A',
                        violation_info.get('status', 'N/A') if violation_info else 'N/A',
                        evidence_cell
                    ]
                    if has_per_activity_exam:
                        row.insert(2, _cell_para(exam_display))
                    activities_data.append(row)
>>>>>>> b003b4f5d912c3823aed770b8cd4e0da7e87090c
            
            if len(data['activities']) > 100:
                activities_data.append([
                    '...', 
                    f'{len(data["activities"]) - 100} more',
                    '', '', '', '', ''
                ])
            
            activities_table = Table(
                activities_data,
                colWidths=[1.2*inch, 0.8*inch, 1.3*inch, 0.7*inch, 0.6*inch, 1.1*inch, 0.8*inch]
            )
            activities_table.setStyle(TableStyle([
                ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#6e5ae6')),
                ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
                ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
                ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
                ('FONTSIZE', (0, 0), (-1, 0), 7),
                ('FONTSIZE', (0, 1), (-1, -1), 6),
                ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
                ('TOPPADDING', (0, 0), (-1, -1), 4),
                ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor('#f7fafc')]),
                ('GRID', (0, 0), (-1, -1), 0.5, colors.grey),
                ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
            ]))
            story.append(activities_table)
            story.append(Spacer(1, 0.3*inch))
        
        # Violation section
        if 'violation' in data and data['violation']:
            story.append(Paragraph("Violation Details", heading_style))
<<<<<<< HEAD
            violation = data['violation']
=======
            violation = viol_data
            sev_val = str(violation.get('severity', 'N/A'))
            ev_url = violation.get('evidence_url') or 'N/A'
            evidence_row = ['Evidence:', ev_url]
            if ev_url and ev_url != 'N/A' and (str(ev_url).startswith('http://') or str(ev_url).startswith('https://')):
                evidence_row[1] = Paragraph(f'<a href="{ev_url}" color="#0066cc">View evidence image</a>', styles['Normal'])
>>>>>>> b003b4f5d912c3823aed770b8cd4e0da7e87090c
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
                    'description': f"{act.activity_type} detected at {act.timestamp.strftime('%H:%M:%S') if act.timestamp else 'unknown time'}"
                }
                
                # Add violation information if exists
                if act_violation:
                    activity_detail['violation'] = {
                        'violation_id': str(act_violation.violation_id),
                        'type': act_violation.violation_type or 'N/A',
                        'severity': act_violation.severity or 0,
                        'status': act_violation.status or 'pending',
                        'timestamp': act_violation.timestamp.strftime('%Y-%m-%d %H:%M:%S') if act_violation.timestamp else ''
                    }
                    violations_list.append(activity_detail['violation'])
                else:
                    activity_detail['violation'] = None
                
                detailed_activities.append(activity_detail)
            
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

<<<<<<< HEAD
=======

async def generate_invigilator_report_file_async(
    report_id: UUID,
    file_path: str,
    format_type: str,
    invigilator_id: Optional[UUID] = None,
    exam_id: Optional[UUID] = None,
    report_mode: str = "all_invigilators_violations",
):
    """Background task: invigilator duty report (violations vs invigilation activity, with exam / room / time)."""
    logger.info(
        "generate_invigilator_report_file_async: start report_id=%s file_path=%s format=%s invigilator_id=%s exam_id=%s mode=%s",
        report_id, file_path, format_type, invigilator_id, exam_id, report_mode
    )
    db = SessionLocal()
    try:
        def _build_query(fallback_legacy: bool = False):
            if fallback_legacy:
                q = db.query(
                    InvigilatorActivity.activity_id.label("activity_id"),
                    InvigilatorActivity.invigilator_id.label("invigilator_id"),
                    InvigilatorActivity.room_id.label("room_id"),
                    InvigilatorActivity.timestamp.label("timestamp"),
                    InvigilatorActivity.activity_type.label("activity_type"),
                    InvigilatorActivity.notes.label("notes"),
                )
            else:
                q = db.query(InvigilatorActivity)
            if report_mode in ("single_exam_detailed", "single_all_exams_violations") and invigilator_id:
                q = q.filter(InvigilatorActivity.invigilator_id == invigilator_id)
            if report_mode == "single_exam_detailed" and exam_id:
                q = q.join(Room, Room.room_id == InvigilatorActivity.room_id).filter(Room.exam_id == exam_id)
            return q.order_by(InvigilatorActivity.timestamp.desc())

        legacy_fallback = False
        try:
            activities = _build_query(False).all()
        except ProgrammingError as e:
            db.rollback()
            if "invigilator_activities.severity" in str(e):
                logger.warning("Falling back to legacy invigilator_activities schema (no severity/activity_category/duration)")
                legacy_fallback = True
                activities = _build_query(True).all()
            else:
                raise
        detailed = []
        exam_keys: Dict[str, Any] = {}
        for act in activities:
            inv = db.query(Invigilator).filter(Invigilator.invigilator_id == act.invigilator_id).first()
            room = db.query(Room).filter(Room.room_id == act.room_id).first()
            exam = None
            if room and room.exam_id:
                exam = db.query(Exam).filter(Exam.exam_id == room.exam_id).first()
                exam_keys[str(room.exam_id)] = (exam, room)
            if room:
                room_display = f"{room.block}-{room.room_number}" if room.block else str(room.room_number)
            else:
                room_display = "N/A"
            exam_course = exam.course if exam else "N/A"
            exam_date = str(exam.exam_date) if exam and exam.exam_date else ""
            if exam and exam.start_time and exam.end_time:
                exam_time_range = (
                    f"{exam.start_time.strftime('%H:%M')}–{exam.end_time.strftime('%H:%M')}"
                )
            elif exam and exam.start_time:
                exam_time_range = exam.start_time.strftime('%H:%M')
            else:
                exam_time_range = ""
            cat = (None if legacy_fallback else getattr(act, "activity_category", None)) or "invigilation_activity"
            if cat not in ("violation", "invigilation_activity"):
                cat = "invigilation_activity"
            dur = None if legacy_fallback else getattr(act, "duration_seconds", None)
            detailed.append({
                'activity_id': str(act.activity_id),
                'invigilator_name': inv.name if inv else 'N/A',
                'room_name': room_display,
                'timestamp': act.timestamp.strftime('%Y-%m-%d %H:%M:%S') if act.timestamp else '',
                'activity_type': act.activity_type or 'N/A',
                'severity': ('' if legacy_fallback else (getattr(act, 'severity', None) or '')),
                'notes': act.notes or '',
                'activity_category': cat,
                'duration_seconds': dur,
                'exam_course': exam_course,
                'exam_date': exam_date,
                'exam_time_range': exam_time_range,
            })

        violations = [r for r in detailed if r.get('activity_category') == 'violation']
        include_routine = report_mode == "single_exam_detailed"
        routine = [r for r in detailed if r.get('activity_category') != 'violation'] if include_routine else []

        header_lines: List[str] = []
        if len(exam_keys) == 1:
            ex, rm = next(iter(exam_keys.values()))
            if ex and rm:
                st = ex.start_time.strftime('%H:%M') if ex.start_time else ''
                en = ex.end_time.strftime('%H:%M') if ex.end_time else ''
                tr = f"{st} – {en}" if (st and en) else (st or en or "")
                rlabel = f"{rm.block}-{rm.room_number}" if rm.block else str(rm.room_number)
                header_lines = [
                    f"Exam / course: {ex.course}",
                    f"Exam date: {ex.exam_date}",
                    f"Scheduled exam time: {tr}",
                    f"Room: {rlabel}",
                ]
        elif len(exam_keys) > 1:
            header_lines = [
                "Multiple exam sessions are included; each row lists the exam, scheduled time, and room.",
            ]
        else:
            header_lines = [
                "Link activities to exams by assigning rooms to exams and invigilators via Invigilator Plans.",
            ]

        report_data = {
            'title': 'Invigilator duty report',
            'generated_at': datetime.utcnow().isoformat(),
            'report_type': 'invigilator',
            'invigilator_report_mode': report_mode,
            'invigilator_include_routine': include_routine,
            'activities': detailed,
            'invigilator_violations': violations,
            'invigilation_activity': routine,
            'invigilator_header_lines': header_lines,
            'summary': {
                'total_activities': len(detailed) if include_routine else len(violations),
                'invigilator_violations_count': len(violations),
                'invigilation_activity_count': len(routine),
            },
        }
        success = False
        if format_type.lower() == 'json':
            success = generate_json_report(report_data, file_path)
        elif format_type.lower() == 'csv':
            success = generate_csv_report(report_data, file_path)
        elif format_type.lower() == 'pdf':
            success = generate_pdf_report(report_data, file_path)
        else:
            success = generate_json_report(report_data, file_path)
        report = db.query(Report).filter(Report.report_id == report_id).first()
        if report:
            if success:
                report.status = "completed"
                base_name = Path(file_path).stem
                for ext in ['.pdf', '.txt', '.csv', '.json']:
                    test_file = REPORTS_DIR / f"{base_name}{ext}"
                    if test_file.exists():
                        report.file_path = f"/reports/{test_file.name}"
                        break
            else:
                report.status = "failed"
            db.commit()
    except Exception as e:
        logger.exception("generate_invigilator_report_file_async failed: report_id=%s error=%s", report_id, e)
        try:
            r = db.query(Report).filter(Report.report_id == report_id).first()
            if r:
                r.status = "failed"
                db.commit()
        except Exception:
            pass
    finally:
        db.close()
        logger.info("generate_invigilator_report_file_async: done report_id=%s", report_id)


>>>>>>> b003b4f5d912c3823aed770b8cd4e0da7e87090c
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


class ReportListResponse(BaseModel):
    reports: List[ReportListItem]
    total: int


class InvigilatorOption(BaseModel):
    invigilator_id: UUID
    name: str
    email: Optional[str] = None


class InvigilatorExamOption(BaseModel):
    exam_id: UUID
    name: str
    exam_date: Optional[str] = None


# -------------------------
# CRUD Routes
# -------------------------

@router.get("/invigilators/options", response_model=List[InvigilatorOption])
def get_invigilator_options(
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    """Lightweight invigilator list for report dropdowns."""
    if current_user.get("user_type") not in ["admin", "investigator"]:
        raise HTTPException(status_code=403, detail="Access denied")
    rows = db.query(Invigilator).order_by(Invigilator.name.asc()).all()
    return [
        InvigilatorOption(
            invigilator_id=r.invigilator_id,
            name=r.name or "Unknown",
            email=r.email,
        )
        for r in rows
    ]


@router.get("/invigilators/{invigilator_id}/exam-options", response_model=List[InvigilatorExamOption])
def get_exam_options_for_invigilator(
    invigilator_id: UUID,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    """Return only exams that this invigilator has monitored (based on invigilator_activities)."""
    if current_user.get("user_type") not in ["admin", "investigator"]:
        raise HTTPException(status_code=403, detail="Access denied")

    inv = db.query(Invigilator).filter(Invigilator.invigilator_id == invigilator_id).first()
    if not inv:
        raise HTTPException(status_code=404, detail="Invigilator not found")

    # Include exams from BOTH:
    # 1) invigilator plans (assignments), and
    # 2) actual recorded invigilator activities.
    exam_by_id: Dict[UUID, Exam] = {}

    assigned_exams = (
        db.query(Exam)
        .join(ExamInvigilatorAssignment, ExamInvigilatorAssignment.exam_id == Exam.exam_id)
        .filter(ExamInvigilatorAssignment.invigilator_id == invigilator_id)
        .all()
    )
    for e in assigned_exams:
        exam_by_id[e.exam_id] = e

    activity_exams = (
        db.query(Exam)
        .join(Room, Room.exam_id == Exam.exam_id)
        .join(InvigilatorActivity, InvigilatorActivity.room_id == Room.room_id)
        .filter(InvigilatorActivity.invigilator_id == invigilator_id)
        .distinct()
        .all()
    )
    for e in activity_exams:
        exam_by_id[e.exam_id] = e

    rows = sorted(
        exam_by_id.values(),
        key=lambda e: (e.exam_date or date.min),
        reverse=True,
    )
    return [
        InvigilatorExamOption(
            exam_id=e.exam_id,
            name=e.course or "Unnamed Exam",
            exam_date=e.exam_date.strftime("%Y-%m-%d") if e.exam_date else None,
        )
        for e in rows
    ]

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
<<<<<<< HEAD
        violation=violation
=======
        violation=violation,
        violation_view=getattr(request, "violation_view", None) or "all",
    )

    return {
        "id": str(new_report.report_id),
        "file_path": file_path,
        "format": request.format,
        "status": "generating"
    }


# -------------------------
# Generate Invigilator Report
# -------------------------
@router.post("/invigilators", status_code=status.HTTP_201_CREATED)
def generate_invigilator_report(
    request: InvigilatorReportRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    """Generate a report of all invigilator activities."""
    logger.info(
        "generate_invigilator_report: format=%s invigilator_id=%s exam_id=%s mode=%s",
        request.format, request.invigilator_id, request.exam_id, request.report_mode
    )
    if current_user.get("user_type") not in ["admin", "investigator"]:
        raise HTTPException(status_code=403, detail="Access denied")

    timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    filename = f"invigilator_report_{timestamp}.{request.format}"
    file_path = os.path.join("reports", filename)
    investigator_id = get_investigator_id_for_report(current_user, db)
    mode = (request.report_mode or "all_invigilators_violations").strip()
    valid_modes = {"all_invigilators_violations", "single_exam_detailed", "single_all_exams_violations"}
    if mode not in valid_modes:
        raise HTTPException(status_code=400, detail="Invalid report_mode")

    selected_inv_name = None
    if mode in {"single_exam_detailed", "single_all_exams_violations"}:
        if not request.invigilator_id:
            raise HTTPException(status_code=400, detail="invigilator_id is required for selected report mode")
        selected_inv = db.query(Invigilator).filter(
            Invigilator.invigilator_id == request.invigilator_id
        ).first()
        if not selected_inv:
            raise HTTPException(status_code=404, detail="Selected invigilator not found")
        selected_inv_name = selected_inv.name
    if mode == "single_exam_detailed":
        if not request.exam_id:
            raise HTTPException(status_code=400, detail="exam_id is required for single_exam_detailed mode")
        exam_check = db.query(Exam).filter(Exam.exam_id == request.exam_id).first()
        if not exam_check:
            raise HTTPException(status_code=404, detail="Selected exam not found")
    initial_name = (
        f"Invigilator Report - {selected_inv_name} - {datetime.utcnow().strftime('%Y-%m-%d')}"
        if selected_inv_name
        else f"Invigilator Report - {datetime.utcnow().strftime('%Y-%m-%d')}"
    )
    new_report = Report(
        name=initial_name,
        report_type="invigilator",
        file_path=file_path,
        violation_id=None,
        generated_by=investigator_id,
        status="generating"
    )
    db.add(new_report)
    db.commit()
    db.refresh(new_report)
    background_tasks.add_task(
        generate_invigilator_report_file_async,
        report_id=new_report.report_id,
        file_path=file_path,
        format_type=request.format,
        invigilator_id=request.invigilator_id,
        exam_id=request.exam_id,
        report_mode=mode,
    )
    return {
        "id": str(new_report.report_id),
        "file_path": file_path,
        "format": request.format,
        "status": "generating"
    }


# -------------------------
# Generate Student Violations Report (one student, all exams)
# -------------------------
@router.post("/students/{student_id}")
def generate_student_report(
    student_id: UUID,
    request: StudentReportRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    """
    Generate a report for a single student showing their violations across all exams.
    """
    logger.info("generate_student_report: student_id=%s format=%s", student_id, request.format)
    if current_user.get("user_type") not in ["admin", "investigator"]:
        raise HTTPException(status_code=403, detail="Access denied")

    student = db.query(Student).filter(Student.student_id == student_id).first()
    if not student:
        logger.warning("generate_student_report: student not found student_id=%s", student_id)
        raise HTTPException(status_code=404, detail="Student not found")

    # All activities (violations/incidents) for this student, any exam, newest first
    activities = (
        db.query(StudentActivity)
        .filter(StudentActivity.student_id == student_id)
        .order_by(StudentActivity.timestamp.desc())
        .all()
    )
    logger.info("generate_student_report: student_id=%s activities_count=%s", student_id, len(activities))

    timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    filename = f"student_report_{student_id}_{timestamp}.{request.format}"
    file_path = os.path.join("reports", filename)

    violation = None
    if activities:
        violation = db.query(Violation).filter(
            Violation.activity_id == activities[0].activity_id
        ).first()
    if not violation and activities:
        violation = Violation(
            activity_id=activities[0].activity_id,
            violation_type="Student Report",
            severity=1,
            status="pending"
        )
        db.add(violation)
        db.commit()
        db.refresh(violation)

    investigator_id = get_investigator_id_for_report(current_user, db)
    initial_name = f"Student Report - {student.name or 'Student'} ({student.roll_number or student_id}) - Violations across exams"
    new_report = Report(
        name=initial_name,
        report_type="student",
        file_path=file_path,
        violation_id=violation.violation_id if violation else None,
        generated_by=investigator_id,
        status="generating"
    )
    db.add(new_report)
    db.commit()
    db.refresh(new_report)

    logger.info(
        "generate_student_report: adding background task report_id=%s student_id=%s format=%s",
        new_report.report_id, student_id, request.format,
    )
    background_tasks.add_task(
        generate_report_file_async,
        report_id=new_report.report_id,
        report_type="student",
        file_path=file_path,
        format_type=request.format,
        activities=activities,
        exam=None,
        violation=violation,
        violation_view=getattr(request, "violation_view", None) or "all",
>>>>>>> b003b4f5d912c3823aed770b8cd4e0da7e87090c
    )

    return {
        "id": str(new_report.report_id),
        "file_path": file_path,
        "format": request.format,
        "status": "generating"
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

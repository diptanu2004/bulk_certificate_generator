"""The single predefined certificate template.

A pure function: data in, PDF bytes out. It touches no database and no
filesystem, which makes it trivial to unit test and trivial to swap.

The template is drawn programmatically rather than overlaid on a supplied
image, so output needs no external font or asset and is reproducible on any
machine.
"""

from datetime import date
from io import BytesIO

from reportlab.lib.colors import HexColor
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas

PAGE_SIZE = landscape(A4)
WIDTH, HEIGHT = PAGE_SIZE

INK = HexColor("#1F2933")
MUTED = HexColor("#6B7A86")
ACCENT = HexColor("#9C6B1F")


def render_certificate(*, recipient_name: str, course_name: str, issued_on: date) -> bytes:
    if not recipient_name.strip():
        raise ValueError("recipient name is required to render a certificate")

    buffer = BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=PAGE_SIZE)
    pdf.setTitle(f"Certificate - {recipient_name}")

    # Double border.
    pdf.setStrokeColor(ACCENT)
    pdf.setLineWidth(3)
    pdf.rect(14 * mm, 14 * mm, WIDTH - 28 * mm, HEIGHT - 28 * mm)
    pdf.setLineWidth(0.8)
    pdf.rect(18 * mm, 18 * mm, WIDTH - 36 * mm, HEIGHT - 36 * mm)

    centre = WIDTH / 2

    pdf.setFillColor(MUTED)
    pdf.setFont("Helvetica", 12)
    pdf.drawCentredString(centre, HEIGHT - 48 * mm, "CERTIFICATE OF COMPLETION")

    pdf.setFillColor(INK)
    pdf.setFont("Helvetica", 13)
    pdf.drawCentredString(centre, HEIGHT - 72 * mm, "This is to certify that")

    pdf.setFillColor(ACCENT)
    pdf.setFont("Helvetica-Bold", 30)
    pdf.drawCentredString(centre, HEIGHT - 90 * mm, recipient_name)

    pdf.setStrokeColor(MUTED)
    pdf.setLineWidth(0.5)
    pdf.line(centre - 70 * mm, HEIGHT - 95 * mm, centre + 70 * mm, HEIGHT - 95 * mm)

    pdf.setFillColor(INK)
    pdf.setFont("Helvetica", 13)
    pdf.drawCentredString(centre, HEIGHT - 110 * mm, "has successfully completed")

    pdf.setFont("Helvetica-Bold", 18)
    pdf.drawCentredString(centre, HEIGHT - 124 * mm, course_name)

    pdf.setFillColor(MUTED)
    pdf.setFont("Helvetica", 11)
    pdf.drawCentredString(centre, 30 * mm, f"Issued on {issued_on.isoformat()}")

    pdf.showPage()
    pdf.save()
    return buffer.getvalue()

"""Render and independently inspect each finished PDF before it can be saved."""

from dataclasses import dataclass
from io import BytesIO
from threading import Lock

from .document import PDFError
from .layout import MARGIN, MAX_PAGES, PAGE_HEIGHT, PAGE_WIDTH, pages

# PDFium calls must be serialized. ReportLab also shares the registered fonts across threads.
RENDER_LOCK = Lock()
MAX_BYTES = 2_000_000
RASTER_SCALE = 1.5


@dataclass(frozen=True)
class ValidatedPDF:
    title: str
    pages: int
    data: bytes

    def receipt(self):
        return {"title": self.title, "pages": self.pages}


def render(document):
    try:
        with RENDER_LOCK:
            return build(document)
    except ImportError as exc:
        raise PDFError("renderer_unavailable", "The server PDF dependencies are unavailable.") from exc
    except PDFError:
        raise
    except Exception as exc:
        raise PDFError("validation_failed", "The PDF could not be rendered or verified.") from exc


def build(document):
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.pdfgen.canvas import Canvas

    for name, filename in (("NexSans", "Vera.ttf"), ("NexBold", "VeraBd.ttf")):
        if name not in pdfmetrics.getRegisteredFontNames():
            pdfmetrics.registerFont(TTFont(name, filename))
    glyphs = pdfmetrics.getFont("NexSans").face.charToGlyph
    if any(not glyphs.get(ord(character)) for character in document.text() if not character.isspace()):
        raise PDFError("unsupported_text", "Some characters are unavailable in the embedded PDF fonts.")

    def measure(text, style):
        return pdfmetrics.stringWidth(text, style.font, style.size)

    buffer = BytesIO()
    canvas = Canvas(buffer, pagesize=(PAGE_WIDTH, PAGE_HEIGHT), pageCompression=1)
    canvas.setTitle(document.title)
    planned = []
    for placements, rule, divider in pages(document, measure):
        if len(planned) == MAX_PAGES:
            raise PDFError("page_limit", "The document exceeds the supported page count.")
        canvas.setStrokeColor("#46645c")
        canvas.setLineWidth(2 if document.layout == "newspaper" and not planned else 0.7)
        canvas.line(MARGIN, rule, PAGE_WIDTH - MARGIN, rule)
        if divider is not None:
            canvas.setStrokeColor("#b6c1bf")
            canvas.setLineWidth(0.5)
            canvas.line(PAGE_WIDTH / 2, rule - 12, PAGE_WIDTH / 2, divider)
        for placement in placements:
            style = placement.line.style
            canvas.setFillColor(style.color)
            canvas.setFont(style.font, style.size)
            canvas.drawString(placement.x, placement.baseline, placement.line.text)
        canvas.showPage()
        planned.append(placements)
    canvas.save()
    data = buffer.getvalue()
    validate(data, planned)
    return ValidatedPDF(document.title, len(planned), data)


def validate(data, planned):
    from pypdfium2 import PdfDocument

    if not planned or len(data) > MAX_BYTES:
        raise PDFError("validation_failed", "The PDF has an invalid size or page count.")
    with PdfDocument(data) as pdf:
        if len(pdf) != len(planned):
            raise PDFError("validation_failed", "The rendered page count does not match the layout.")
        for index, lines in enumerate(planned):
            page = pdf[index]
            try:
                check_page(page, lines)
            finally:
                page.close()


def check_page(page, lines):
    from pypdfium2.raw import FPDF_PAGEOBJ_TEXT

    if page.get_size() != (PAGE_WIDTH, PAGE_HEIGHT):
        raise PDFError("validation_failed", "The rendered page has an unexpected size.")
    text_page = page.get_textpage()
    bitmap = page.render(scale=RASTER_SCALE, grayscale=True)
    try:
        image = bitmap.to_pil()
        regions = [line.bounds for line in lines]
        for text in page.get_objects(filter=(FPDF_PAGEOBJ_TEXT,)):
            bounds = text.get_bounds()
            if not any(inside(bounds, region) for region in regions):
                raise PDFError("validation_failed", "Text spills outside its line or column.")
        for line in lines:
            left, bottom, right, top = line.bounds
            if not (0 <= left < right <= PAGE_WIDTH and 0 <= bottom < top <= PAGE_HEIGHT):
                raise PDFError("validation_failed", "A text line is outside the page.")
            actual = " ".join(text_page.get_text_bounded(left, bottom, right, top).split())
            if actual != line.line.text:
                raise PDFError("validation_failed", "The rendered text does not match its planned line.")
            ink = image.crop((int(left * RASTER_SCALE), int((PAGE_HEIGHT - top) * RASTER_SCALE),
                              int(right * RASTER_SCALE), int((PAGE_HEIGHT - bottom) * RASTER_SCALE))).getextrema()
            if ink[0] > 200:
                raise PDFError("validation_failed", "A planned text line is missing from the rasterized page.")
    finally:
        bitmap.close()
        text_page.close()


def inside(bounds, region):
    left, bottom, right, top = bounds
    return region[0] <= left <= right <= region[2] and region[1] <= bottom <= top <= region[3]

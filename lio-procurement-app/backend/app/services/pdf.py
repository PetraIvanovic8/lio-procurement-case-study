import io

from pypdf import PdfReader

MAX_PDF_PAGES = 100
MAX_EXTRACTED_TEXT_CHARS = 120_000


class PdfExtractionError(ValueError):
    """Raised when a PDF exceeds safe parsing limits or cannot be read."""


def extract_text(pdf_bytes: bytes) -> str:
    """Extract the text from a vendor-offer PDF.

    Covers the offer formats we handle; the vendors we work with send
    text-based PDFs.
    """
    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
    except Exception as exc:
        raise PdfExtractionError("The PDF could not be read.") from exc
    if len(reader.pages) > MAX_PDF_PAGES:
        raise PdfExtractionError(
            f"PDF has {len(reader.pages)} pages; the maximum supported is {MAX_PDF_PAGES}."
        )
    pages = [page.extract_text() or "" for page in reader.pages]
    text = "\n".join(pages)
    if len(text) > MAX_EXTRACTED_TEXT_CHARS:
        raise PdfExtractionError(
            "PDF text is too long to process safely; please upload a shorter quote."
        )
    return text

from io import BytesIO

from pypdf import PdfReader
from pypdf.errors import PdfReadError


class InvalidPDFError(ValueError):
    pass


class NoExtractablePDFTextError(ValueError):
    pass


def extract_pdf_text(raw_content: bytes) -> str:
    if not raw_content.startswith(b'%PDF-'):
        raise InvalidPDFError('File does not have a valid PDF signature.')
    try:
        reader = PdfReader(BytesIO(raw_content), strict=True)
        if reader.is_encrypted:
            raise InvalidPDFError('Encrypted PDF files are not supported.')
        text = '\n'.join(
            page_text.strip()
            for page in reader.pages
            if (page_text := page.extract_text())
        )
    except PdfReadError as error:
        raise InvalidPDFError('Unable to parse the uploaded PDF.') from error
    if not text.strip():
        raise NoExtractablePDFTextError(
            'PDF contains no extractable text. Scanned/image-only PDFs are not supported.'
        )
    return text

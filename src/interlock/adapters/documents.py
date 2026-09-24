"""Getting text out of an uploaded recall document, safely.

Recall requests often arrive as PDFs - on ACH the reason for an R06 request
travels in a letter of indemnity that Nacha distributes as a PDF. This module
turns an upload into plain text for the intake agent, and refuses what it
cannot read honestly:

- **Scanned PDFs with no text layer** are refused with a message saying so.
  Reading them needs OCR, which is not built; returning empty text would let
  the intake agent "extract" nothing and look like a quiet success.
- **Size and page limits** stop a hostile file from tying up the server.
- **Encrypted PDFs** are refused rather than guessed at.

Only PDF and plain text are accepted. The file is never stored; its text is
used for one extraction and the draft keeps that text for the operator to see.
"""

from __future__ import annotations

import io

MAX_UPLOAD_BYTES = 5 * 1024 * 1024
MAX_PAGES = 20
MAX_TEXT_CHARS = 20_000


class DocumentError(ValueError):
    """An upload that cannot be turned into text we can stand behind."""


def text_from_upload(filename: str, data: bytes) -> str:
    """Plain text from a PDF or .txt upload, or :class:`DocumentError`."""
    if len(data) > MAX_UPLOAD_BYTES:
        raise DocumentError("File is larger than 5 MB; upload the relevant pages only.")
    name = (filename or "").lower()

    if name.endswith(".txt"):
        try:
            return data.decode("utf-8")[:MAX_TEXT_CHARS]
        except UnicodeDecodeError as bad:
            raise DocumentError("Text file is not UTF-8.") from bad

    if not (name.endswith(".pdf") or data.startswith(b"%PDF")):
        raise DocumentError("Only PDF and .txt files are accepted.")

    from pypdf import PdfReader
    from pypdf.errors import PdfReadError

    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            raise DocumentError("The PDF is password-protected; it was not opened.")
        if len(reader.pages) > MAX_PAGES:
            raise DocumentError(f"The PDF has more than {MAX_PAGES} pages.")
        text = "\n".join((page.extract_text() or "") for page in reader.pages)
    except DocumentError:
        raise
    except (PdfReadError, ValueError, KeyError, TypeError, OSError) as broken:
        raise DocumentError(f"The PDF could not be read ({type(broken).__name__}).") from broken

    if not text.strip():
        raise DocumentError(
            "The PDF has no text layer - it is probably a scan. Reading scans needs OCR, "
            "which this build does not have; type or paste the key details instead."
        )
    return text[:MAX_TEXT_CHARS]

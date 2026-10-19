import sys
import tempfile
import types
import unittest
import asyncio
import importlib.util
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

try:
    import pypdf  # noqa: F401
    PYPDF_AVAILABLE = True
except ImportError:
    PYPDF_AVAILABLE = False


class FakePage(dict):
    def __init__(self, text="", annotations=None, error=None):
        super().__init__({"/Annots": annotations or []})
        self.text = text
        self.error = error

    def extract_text(self):
        if self.error:
            raise self.error
        return self.text


class FakeReader:
    def __init__(self, pages=(), encrypted=False):
        self.pages = list(pages)
        self.is_encrypted = encrypted


def build_minimal_pdf(page_texts):
    """Build a small valid PDF fixture without depending on a PDF writer."""
    page_ids = [3 + index * 2 for index in range(len(page_texts))]
    content_ids = [page_id + 1 for page_id in page_ids]
    font_id = 3 + len(page_texts) * 2
    kids = " ".join(f"{page_id} 0 R" for page_id in page_ids)
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        f"<< /Type /Pages /Kids [{kids}] /Count {len(page_ids)} >>".encode(),
    ]
    for page_id, content_id, text in zip(page_ids, content_ids, page_texts):
        escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        stream = f"BT /F1 12 Tf 72 720 Td ({escaped}) Tj ET".encode("ascii")
        objects.extend([
            (f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
             f"/Resources << /Font << /F1 {font_id} 0 R >> >> "
             f"/Contents {content_id} 0 R >>").encode(),
            f"<< /Length {len(stream)} >>\nstream\n".encode() + stream + b"\nendstream",
        ])
    if page_texts:
        objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    output = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for index, body in enumerate(objects, 1):
        offsets.append(len(output))
        output.extend(f"{index} 0 obj\n".encode() + body + b"\nendobj\n")
    xref_offset = len(output)
    output.extend(f"xref\n0 {len(objects) + 1}\n".encode())
    output.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        output.extend(f"{offset:010d} 00000 n \n".encode())
    trailer = (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref_offset}\n%%EOF\n"
    )
    output.extend(trailer.encode())
    return bytes(output)


class PdfExtractionTests(unittest.TestCase):
    def _reader_module(self, reader_or_error):
        def build_reader(_source, strict=False):
            if isinstance(reader_or_error, Exception):
                raise reader_or_error
            return reader_or_error
        return types.SimpleNamespace(PdfReader=build_reader)

    def test_extracts_text_across_multiple_pdf_pages(self):
        import main
        reader = FakeReader([FakePage("First page"), FakePage("Second page")])
        with patch.dict(sys.modules, {"pypdf": self._reader_module(reader)}):
            self.assertEqual(main.extract_pdf_text(b"pdf bytes"), "First page\nSecond page")

    def test_empty_pdf_has_no_extracted_text(self):
        import main
        with patch.dict(sys.modules, {"pypdf": self._reader_module(FakeReader([]))}):
            self.assertEqual(main.extract_pdf_text(b"empty pdf"), "")

    def test_empty_pdf_upload_is_rejected_before_profile_storage(self):
        import main
        from fastapi import HTTPException, UploadFile
        with patch.object(main, "extract_pdf_text", return_value=""):
            with self.assertRaises(HTTPException) as raised:
                asyncio.run(main.upload_resume(UploadFile(filename="empty.pdf", file=BytesIO(b"empty"))))
        self.assertEqual(raised.exception.status_code, 400)
        self.assertEqual(raised.exception.detail, "No selectable text found.")

    def test_extracts_resume_text_and_official_profile_links(self):
        source = Path(__file__).resolve().parent.parent / "resume_reader.py"
        spec = importlib.util.spec_from_file_location("resume_reader_for_test", source)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        annotation = {"/A": {"/URI": "https://github.com/example-candidate"}}
        reader = FakeReader([FakePage("Example Candidate\nPython developer", [annotation])])
        with tempfile.TemporaryDirectory() as directory:
            resume = Path(directory) / "resume.pdf"
            resume.write_bytes(b"pdf bytes")
            with patch.dict(sys.modules, {"pypdf": self._reader_module(reader)}):
                result = module.read_resume(resume)
        self.assertEqual(result["full_name"], "Example Candidate")
        self.assertIn("Python developer", result["resume_text"])
        self.assertEqual(result["github"], "https://github.com/example-candidate")

    def test_reader_failure_is_reported_as_unreadable_pdf(self):
        import main
        with patch.dict(sys.modules, {"pypdf": self._reader_module(RuntimeError("bad PDF"))}):
            with self.assertRaisesRegex(RuntimeError, "bad PDF"):
                main.extract_pdf_text(b"corrupt PDF")

    def test_encrypted_pdf_is_rejected(self):
        import main
        with patch.dict(sys.modules, {"pypdf": self._reader_module(FakeReader(encrypted=True))}):
            with self.assertRaisesRegex(ValueError, "Password-protected"):
                main.extract_pdf_text(b"encrypted PDF")

    def test_page_limit_is_preserved(self):
        import main
        reader = FakeReader([FakePage("") for _ in range(31)])
        with patch.dict(sys.modules, {"pypdf": self._reader_module(reader)}):
            with self.assertRaisesRegex(OverflowError, "Maximum 30 pages"):
                main.extract_pdf_text(b"too many pages")


@unittest.skipUnless(PYPDF_AVAILABLE, "pypdf is not installed in this environment")
class PypdfIntegrationTests(unittest.TestCase):
    def test_real_multi_page_pdf_text_extraction(self):
        import main
        self.assertEqual(
            main.extract_pdf_text(build_minimal_pdf(["First page", "Second page"])),
            "First page\nSecond page",
        )

    def test_real_empty_pdf_has_no_text(self):
        import main
        self.assertEqual(main.extract_pdf_text(build_minimal_pdf([])), "")

    def test_real_malformed_pdf_fails_cleanly(self):
        import main
        with self.assertRaises(Exception):
            main.extract_pdf_text(b"not a PDF")


if __name__ == "__main__":
    unittest.main()

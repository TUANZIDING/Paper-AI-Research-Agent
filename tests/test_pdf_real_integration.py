from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

try:
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject
except ImportError:  # exercised by the dependency-free unit-test phase
    PdfWriter = None

from ai_research_agent.pdf_audit import extract_pdf_references


@unittest.skipIf(PdfWriter is None, "pypdf is not installed")
class RealPdfIntegrationTests(unittest.TestCase):
    def test_real_pypdf_extracts_reference_and_body_citation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "real-fixture.pdf"
            writer = PdfWriter()
            page = writer.add_blank_page(width=612, height=792)
            font = DictionaryObject({
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Helvetica"),
            })
            font_ref = writer._add_object(font)
            page[NameObject("/Resources")] = DictionaryObject({
                NameObject("/Font"): DictionaryObject({NameObject("/F1"): font_ref})
            })
            stream = DecodedStreamObject()
            stream.set_data(
                b"BT /F1 12 Tf 72 720 Td (Body cites [1].) Tj ET\n"
                b"BT /F1 12 Tf 72 680 Td (References) Tj ET\n"
                b"BT /F1 10 Tf 72 660 Td "
                b"([1] Smith J. Test title. Bone. 2020;1:1-2.) Tj ET"
            )
            page[NameObject("/Contents")] = writer._add_object(stream)
            with path.open("wb") as output:
                writer.write(output)

            result = extract_pdf_references(path, max_bytes=1_000_000, max_pages=2)

        self.assertEqual(len(result.references), 1)
        self.assertEqual(result.references[0].title, "Test title. Bone")
        self.assertEqual(result.references[0].year, 2020)
        self.assertEqual({item.reference_number for item in result.citations}, {1})
        self.assertEqual(result.uncited_reference_numbers, ())


if __name__ == "__main__":
    unittest.main()

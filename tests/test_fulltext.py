import hashlib
from pathlib import Path
import tempfile
import time
import unittest

from ai_research_agent.fulltext import (
    FulltextPolicyError,
    FulltextResponseError,
    SafeFulltextDownloader,
    TransportResponse,
    UnsafeNetworkTarget,
)


PDF = (
    b"%PDF-1.7\n1 0 obj\n<< /Type /Catalog >>\nendobj\n"
    b"2 0 obj\n<< /Type /Page >>\nendobj\n"
    b"xref\n0 3\n0000000000 65535 f \n"
    b"trailer\n<< /Root 1 0 R >>\nstartxref\n80\n%%EOF\n"
)


class FakeTransport:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def request(self, **kwargs):
        self.calls.append(kwargs)
        response = self.responses.get(kwargs["url"])
        if response is None and kwargs["url"].endswith("/robots.txt"):
            return TransportResponse(404, {"content-type": "text/plain"}, b"")
        if response is None:
            raise AssertionError(f"Unexpected request {kwargs['url']}")
        return response


class SlowTransport:
    def request(self, **kwargs):
        time.sleep(0.08)
        return TransportResponse(404, {"content-type": "text/plain"}, b"")


def public_resolver(hostname, port):
    return ["93.184.216.34"]


class SafeFulltextTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.destination = Path(self.temporary_directory.name) / "article.pdf"

    def tearDown(self):
        self.temporary_directory.cleanup()

    def downloader(self, responses, **kwargs):
        transport = FakeTransport(responses)
        downloader = SafeFulltextDownloader(
            resolver=kwargs.pop("resolver", public_resolver),
            transport=transport,
            pdf_validator=kwargs.pop("pdf_validator", lambda body: None),
            **kwargs,
        )
        return downloader, transport

    def test_valid_pdf_is_hashed_and_atomically_written(self):
        url = "https://repository.example/article.pdf"
        downloader, transport = self.downloader(
            {url: TransportResponse(200, {"Content-Type": "application/pdf"}, PDF)}
        )
        result = downloader.download(
            url=url,
            license="CC BY 4.0",
            version="publishedVersion",
            destination=self.destination,
        )
        self.assertEqual(self.destination.read_bytes(), PDF)
        self.assertEqual(result.sha256, hashlib.sha256(PDF).hexdigest())
        self.assertEqual(result.byte_length, len(PDF))
        self.assertEqual(transport.calls[-1]["pinned_ip"], "93.184.216.34")
        self.assertNotIn("Authorization", transport.calls[-1]["headers"])

    def test_missing_or_restricted_license_fails_before_network(self):
        url = "https://repository.example/article.pdf"
        downloader, transport = self.downloader({})
        for license_value in (None, "unknown", "CC-BY-ND-4.0"):
            with self.subTest(license=license_value):
                with self.assertRaises(FulltextPolicyError):
                    downloader.download(
                        url=url,
                        license=license_value,
                        version="publishedVersion",
                        destination=self.destination,
                    )
        self.assertEqual(transport.calls, [])

    def test_ssrf_private_and_mixed_dns_answers_are_blocked(self):
        for answers in (["127.0.0.1"], ["93.184.216.34", "10.0.0.5"]):
            with self.subTest(answers=answers):
                downloader, transport = self.downloader(
                    {}, resolver=lambda hostname, port, values=answers: values
                )
                with self.assertRaises(UnsafeNetworkTarget):
                    downloader.download(
                        url="https://repository.example/article.pdf",
                        license="CC BY 4.0",
                        version="publishedVersion",
                        destination=self.destination,
                    )
                self.assertEqual(transport.calls, [])

    def test_each_redirect_target_is_revalidated(self):
        first = "https://public.example/article"
        second = "https://internal.example/article.pdf"
        calls = []

        def resolver(hostname, port):
            calls.append(hostname)
            return ["10.0.0.8"] if hostname == "internal.example" else ["93.184.216.34"]

        downloader, transport = self.downloader(
            {first: TransportResponse(302, {"Location": second}, b"")},
            resolver=resolver,
        )
        with self.assertRaises(UnsafeNetworkTarget):
            downloader.download(
                url=first,
                license="CC BY 4.0",
                version="publishedVersion",
                destination=self.destination,
            )
        self.assertEqual(
            calls, ["public.example", "public.example", "internal.example"]
        )
        self.assertEqual(len(transport.calls), 2)

    def test_https_downgrade_redirect_is_blocked(self):
        first = "https://public.example/article"
        downloader, _ = self.downloader(
            {first: TransportResponse(302, {"location": "http://public.example/file.pdf"}, b"")}
        )
        with self.assertRaises(UnsafeNetworkTarget):
            downloader.download(
                url=first,
                license="CC BY 4.0",
                version="publishedVersion",
                destination=self.destination,
            )

    def test_cross_origin_redirect_is_not_covered_by_initial_license(self):
        first = "https://licensed.example/article"
        second = "https://unrelated.example/article.pdf"
        downloader, _ = self.downloader(
            {first: TransportResponse(302, {"location": second}, b"")}
        )
        with self.assertRaisesRegex(FulltextPolicyError, "Cross-origin"):
            downloader.download(
                url=first, license="CC BY 4.0", version="publishedVersion",
                destination=self.destination,
            )

    def test_minimal_fake_pdf_and_html_xml_are_rejected(self):
        for content_type, body in (
            ("application/pdf", b"%PDF-1.7\nnot a PDF\n%%EOF"),
            ("application/xml", b"<html><body>paywall</body></html>"),
        ):
            with self.subTest(content_type=content_type):
                url = "https://repository.example/article"
                downloader, _ = self.downloader(
                    {url: TransportResponse(200, {"content-type": content_type}, body)}
                )
                with self.assertRaises(FulltextResponseError):
                    downloader.download(
                        url=url, license="CC BY 4.0", version="publishedVersion",
                        destination=self.destination,
                    )

    def test_structured_fake_pdf_reaches_required_parser_and_empty_xml_is_rejected(self):
        url = "https://repository.example/article"
        fake_pdf = b"%PDF-1.7\n/Type /Page\nxref\ntrailer\nstartxref\n0\n%%EOF"
        downloader, _ = self.downloader(
            {url: TransportResponse(200, {"content-type": "application/pdf"}, fake_pdf)},
            pdf_validator=lambda body: (_ for _ in ()).throw(
                FulltextResponseError("PDF parser rejected the document.")
            ),
        )
        with self.assertRaisesRegex(FulltextResponseError, "parser"):
            downloader.download(
                url=url, license="CC BY 4.0", version="publishedVersion",
                destination=self.destination,
            )
        downloader, _ = self.downloader({
            url: TransportResponse(200, {"content-type": "application/xml"},
                                   b'<article xmlns="urn:attacker"/>')
        })
        with self.assertRaises(FulltextResponseError):
            downloader.download(
                url=url, license="CC BY 4.0", version="publishedVersion",
                destination=self.destination,
            )

    def test_jats_namespace_prefix_spoof_and_structureless_xml_are_rejected(self):
        url = "https://repository.example/article.xml"
        bodies = (
            b'<article xmlns="http://jats.nlm.nih.gov.attacker.example/evil"><body><p>'
            + b"X" * 220 + b"</p></body></article>",
            b"<article><body><p>" + b"X" * 220 + b"</p></body></article>",
        )
        for body in bodies:
            with self.subTest(body=body[:60]):
                downloader, _ = self.downloader({
                    url: TransportResponse(200, {"content-type": "application/xml"}, body)
                })
                with self.assertRaises(FulltextResponseError):
                    downloader.download(
                        url=url, license="CC BY 4.0", version="publishedVersion",
                        destination=self.destination,
                    )

    def test_fake_pdf_is_rejected_and_not_written(self):
        url = "https://repository.example/article.pdf"
        downloader, _ = self.downloader(
            {url: TransportResponse(200, {"content-type": "application/pdf"}, b"<html>login</html>")}
        )
        with self.assertRaisesRegex(FulltextResponseError, "signature"):
            downloader.download(
                url=url,
                license="CC BY 4.0",
                version="publishedVersion",
                destination=self.destination,
            )
        self.assertFalse(self.destination.exists())

    def test_access_control_response_is_not_bypassed(self):
        url = "https://publisher.example/article.pdf"
        downloader, _ = self.downloader(
            {url: TransportResponse(401, {"content-type": "text/html"}, b"login")}
        )
        with self.assertRaisesRegex(FulltextPolicyError, "no bypass"):
            downloader.download(
                url=url,
                license="CC BY 4.0",
                version="publishedVersion",
                destination=self.destination,
            )
        self.assertFalse(self.destination.exists())

    def test_oversized_body_is_rejected(self):
        url = "https://repository.example/article.pdf"
        downloader, _ = self.downloader(
            {url: TransportResponse(200, {"content-type": "application/pdf"}, PDF + b"x" * 100)},
            max_bytes=len(PDF),
        )
        with self.assertRaisesRegex(FulltextResponseError, "exceeds"):
            downloader.download(
                url=url,
                license="CC BY 4.0",
                version="publishedVersion",
                destination=self.destination,
            )

    def test_unknown_version_and_robots_denial_fail_closed(self):
        url = "https://repository.example/article.pdf"
        downloader, transport = self.downloader(
            {
                "https://repository.example/robots.txt": TransportResponse(
                    200,
                    {"content-type": "text/plain"},
                    b"User-agent: *\nDisallow: /article.pdf\n",
                ),
                url: TransportResponse(200, {"content-type": "application/pdf"}, PDF),
            },
        )
        with self.assertRaises(FulltextPolicyError):
            downloader.download(
                url=url,
                license="CC BY 4.0",
                version="unknown",
                destination=self.destination,
            )
        with self.assertRaisesRegex(FulltextPolicyError, "robots"):
            downloader.download(
                url=url,
                license="CC BY 4.0",
                version="acceptedVersion",
                destination=self.destination,
            )
        self.assertEqual(
            [call["url"] for call in transport.calls],
            ["https://repository.example/robots.txt"],
        )

    def test_wall_clock_timeout_covers_transport_before_headers(self):
        downloader = SafeFulltextDownloader(
            resolver=public_resolver, transport=SlowTransport(), timeout=0.02,
            pdf_validator=lambda body: None,
        )
        with self.assertRaisesRegex(FulltextResponseError, "wall-clock"):
            downloader.download(
                url="https://repository.example/article.pdf",
                license="CC BY 4.0", version="publishedVersion",
                destination=self.destination,
            )


if __name__ == "__main__":
    unittest.main()

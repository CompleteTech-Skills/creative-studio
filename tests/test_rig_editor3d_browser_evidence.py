"""Credential boundary tests for public browser acceptance evidence."""
import unittest
from editor3d_browser_evidence import redact_browser_log


class BrowserEvidenceRedactionTests(unittest.TestCase):
    def test_headers_launch_fragments_and_query_credentials_are_redacted(self):
        secrets = ['header_Aa1', 'json_Bb2', 'fragment_Cc3', 'query_Dd4', 'encoded_Ee5', 'bare_Ff6', 'ipv6_Gg7']
        source = '''authorization: Bearer header_Aa1
{"Authorization":"Bearer json_Bb2"}
Page.goto: http://127.0.0.1:8765/#fragment_Cc3
Retry http://localhost:8765/editor?token=query_Dd4&sha256=abc123&version=141.0
Encoded http://127.0.0.1:8765/?access%5Ftoken=encoded_Ee5&scene=test
Opaque http://127.0.0.1:8765/?bare_Ff6
IPv6 http://[::1]:8765/?bearer=ipv6_Gg7
'''
        actual = redact_browser_log(source)
        for secret in secrets:
            with self.subTest(secret=secret):
                self.assertNotIn(secret, actual)
        self.assertIn('sha256=abc123&version=141.0', actual)
        self.assertIn('scene=test', actual)
        self.assertIn('Page.goto: http://127.0.0.1:8765/#[REDACTED]', actual)
        self.assertIn('[::1]:8765/?bearer=[REDACTED]', actual)

    def test_engine_hashes_and_noncredential_diagnostics_remain_intact(self):
        source = 'Firefox 141.0 WebKit 26.0 sha256=abc123: WebGL initialization failed at /api/snapshot\nhttps://example.com/docs#browser\nhttp://localhost:8765/api/snapshot?revision=4&sha256=abc123\n'
        self.assertEqual(redact_browser_log(source), source)

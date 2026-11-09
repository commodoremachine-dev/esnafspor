"""Telefon normalizasyonu, şablon doldurma ve gönderim sağlayıcı testleri."""

from __future__ import annotations

import json
import unittest
from unittest.mock import MagicMock, patch

import whatsapp


class PhoneTests(unittest.TestCase):
    def test_normalizes_common_turkish_formats(self):
        expected = "905551112233"
        for raw in (
            "0555 111 22 33",
            "+90 555 111 22 33",
            "90 555 111 22 33",
            "5551112233",
            "0090 555 111 22 33",
            "0555-111-22-33",
        ):
            with self.subTest(raw=raw):
                self.assertEqual(whatsapp.normalize_phone(raw), expected)

    def test_rejects_invalid_numbers(self):
        for raw in ("", "abc", "123", "0555 111 22", "0555 111 22 334", "0111 111 22 33"):
            with self.subTest(raw=raw):
                with self.assertRaises(ValueError):
                    whatsapp.normalize_phone(raw)

    def test_display_format(self):
        self.assertEqual(whatsapp.format_phone_display("905551112233"), "+90 555 111 22 33")
        self.assertEqual(whatsapp.wa_chat_id("905551112233"), "905551112233@c.us")


class TemplateTests(unittest.TestCase):
    def setUp(self):
        self.context = {
            "isim": "Ayşe Yılmaz",
            "saat": "14:30",
            "tarih": "10.11.2026",
            "isletme": "Kuaför Ayşe",
            "link": "https://g.page/r/test/review",
        }

    def test_default_templates(self):
        confirmation = whatsapp.render_message(
            "Merhaba {isim}, bugün saat {saat} randevunuzu onaylıyor musunuz?", self.context
        )
        self.assertEqual(
            confirmation, "Merhaba Ayşe Yılmaz, bugün saat 14:30 randevunuzu onaylıyor musunuz?"
        )
        review = whatsapp.render_message(
            "Hizmetimizi değerlendirmek için 1 dakikanızı ayırır mısınız? Google Yorum Linki: {link}",
            self.context,
        )
        self.assertIn("https://g.page/r/test/review", review)

    def test_turkish_and_case_insensitive_placeholders(self):
        template = "{İsim}|{Isim}|{isim}|{SAAT}|{İşletme}"
        rendered = whatsapp.render_message(template, self.context)
        self.assertEqual(rendered, "Ayşe Yılmaz|Ayşe Yılmaz|Ayşe Yılmaz|14:30|Kuaför Ayşe")

    def test_unknown_placeholder_is_kept(self):
        self.assertEqual(
            whatsapp.render_message("Merhaba {bilinmeyen}", self.context),
            "Merhaba {bilinmeyen}",
        )

    def test_clean_body_truncates(self):
        self.assertEqual(whatsapp.clean_body("  merhaba  "), "merhaba")
        long_text = "a" * (whatsapp.MAX_MESSAGE_LENGTH + 50)
        self.assertLessEqual(len(whatsapp.clean_body(long_text)), whatsapp.MAX_MESSAGE_LENGTH)


class ProviderTests(unittest.TestCase):
    def test_simulation_send(self):
        result = whatsapp.send("905551112233", "Merhaba", {"whatsapp_provider": "simulation"})
        self.assertTrue(result["ok"])
        self.assertTrue(result["simulated"])
        self.assertEqual(result["provider"], "simulation")

    def test_invalid_phone_rejected(self):
        result = whatsapp.send("123", "Merhaba", {"whatsapp_provider": "simulation"})
        self.assertFalse(result["ok"])
        self.assertIn("normalize", result["detail"])

    def test_empty_message_rejected(self):
        result = whatsapp.send("905551112233", "   ", {"whatsapp_provider": "simulation"})
        self.assertFalse(result["ok"])

    def test_provider_status(self):
        ready, _ = whatsapp.provider_status({"whatsapp_provider": "simulation"})
        self.assertTrue(ready)
        ready, message = whatsapp.provider_status(
            {"whatsapp_provider": "http", "http_api_url": ""}
        )
        self.assertFalse(ready)
        self.assertIn("geçersiz", message.lower())

    def _http_response(self, status=200, body=b'{"ok":true}'):
        response = MagicMock()
        response.status = status
        response.read.return_value = body
        response.__enter__ = MagicMock(return_value=response)
        response.__exit__ = MagicMock(return_value=False)
        return response

    @patch("whatsapp.urllib.request.urlopen")
    def test_http_generic_payload(self, urlopen_mock):
        urlopen_mock.return_value = self._http_response()
        settings = {
            "whatsapp_provider": "http",
            "http_api_url": "http://127.0.0.1:3000/send",
            "http_mode": "generic",
            "http_api_token": "gizli",
        }
        result = whatsapp.send("905551112233", "Merhaba", settings)
        self.assertTrue(result["ok"])
        request = urlopen_mock.call_args[0][0]
        payload = json.loads(request.data.decode("utf-8"))
        self.assertEqual(payload["to"], "905551112233")
        self.assertEqual(payload["message"], "Merhaba")
        self.assertEqual(request.get_header("Authorization"), "Bearer gizli")

    @patch("whatsapp.urllib.request.urlopen")
    def test_waha_payload(self, urlopen_mock):
        urlopen_mock.return_value = self._http_response()
        settings = {
            "whatsapp_provider": "http",
            "http_api_url": "http://127.0.0.1:3000/",
            "http_mode": "waha",
            "http_session": "esnaf",
        }
        result = whatsapp.send("905551112233", "Merhaba", settings)
        self.assertTrue(result["ok"])
        request = urlopen_mock.call_args[0][0]
        self.assertTrue(request.full_url.endswith("/api/sendText"))
        payload = json.loads(request.data.decode("utf-8"))
        self.assertEqual(payload["session"], "esnaf")
        self.assertEqual(payload["chatId"], "905551112233@c.us")

    @patch("whatsapp.urllib.request.urlopen")
    def test_http_error_is_reported(self, urlopen_mock):
        from urllib.error import HTTPError

        urlopen_mock.side_effect = HTTPError(
            "http://127.0.0.1:3000/send", 500, "Boom", {}, None
        )
        settings = {
            "whatsapp_provider": "http",
            "http_api_url": "http://127.0.0.1:3000/send",
        }
        result = whatsapp.send("905551112233", "Merhaba", settings)
        self.assertFalse(result["ok"])
        self.assertIn("500", result["detail"])


if __name__ == "__main__":
    unittest.main()

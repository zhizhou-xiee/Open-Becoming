import hashlib
import hmac
import json
import unittest
from unittest.mock import patch

from mobile_extensions import (
    MobilePushClient,
    MobilePushConfig,
    public_mobile_manifest,
    validate_push_webhook_url,
)


class FakeResponse:
    def __init__(self, status_code=204):
        self.status_code = status_code


class MobileExtensionTests(unittest.TestCase):
    def test_webhook_is_disabled_by_default_and_validated_when_enabled(self):
        self.assertFalse(MobilePushClient.from_env({}).enabled)
        self.assertEqual(
            validate_push_webhook_url("https://example.com/events"),
            "https://example.com/events",
        )
        with self.assertRaises(ValueError):
            validate_push_webhook_url("file:///tmp/events")
        with self.assertRaises(ValueError):
            MobilePushClient.from_env({
                "MOBILE_PUSH_ENABLED": "true",
                "MOBILE_PUSH_WEBHOOK_URL": "https://example.com/events",
                "MOBILE_PUSH_WEBHOOK_SECRET": "short",
            })

    @patch("mobile_extensions.requests.post")
    def test_push_event_is_minimal_and_signed_over_exact_body(self, post):
        post.return_value = FakeResponse()
        secret = "0123456789abcdef0123456789abcdef"
        client = MobilePushClient(MobilePushConfig(
            enabled=True,
            url="https://example.com/events",
            secret=secret,
            timeout=3,
        ))

        sent = client.send_message(
            character_id="char2",
            character_name="Char 2",
            text="第一行\n\n" + "很长" * 200,
            message_id=42,
            source="desire",
            now=1_700_000_000,
        )

        self.assertTrue(sent)
        request = post.call_args.kwargs
        body = request["data"]
        payload = json.loads(body)
        self.assertEqual(payload["event"], "message.created")
        self.assertEqual(payload["data"]["character_id"], "char2")
        self.assertEqual(payload["data"]["message_id"], 42)
        self.assertLessEqual(len(payload["data"]["preview"]), 240)
        self.assertNotIn("history", payload)
        self.assertNotIn("token", payload)

        expected = hmac.new(
            secret.encode(),
            b"1700000000." + body,
            hashlib.sha256,
        ).hexdigest()
        self.assertEqual(
            request["headers"]["X-Becoming-Signature"],
            f"sha256={expected}",
        )
        self.assertEqual(request["timeout"], 3)

    def test_public_manifest_never_contains_endpoint_or_secret(self):
        manifest = public_mobile_manifest(push_enabled=True)
        self.assertTrue(manifest["push"]["configured"])
        self.assertEqual(manifest["music"]["extension_point"], "custom_mcp")
        self.assertTrue(manifest["music"]["web_room_built_in"])
        self.assertTrue(manifest["phone"]["read_only"])
        self.assertEqual(
            manifest["voice"]["directions"],
            ["speech_to_text", "text_to_speech"],
        )
        self.assertIn("mobile_companion", manifest["voice"]["extension_points"])
        self.assertTrue(manifest["voice"]["built_in"])
        self.assertFalse(manifest["voice"]["default_enabled"])
        self.assertTrue(manifest["voice"]["stores_audio"])
        self.assertEqual(manifest["voice"]["credential_storage"], "server_only")
        self.assertTrue(manifest["voice"]["requires_user_gesture"])
        self.assertEqual(
            manifest["voice"]["tools"],
            ["send_voice", "voice_get_capabilities", "voice_transcribe", "voice_synthesize"],
        )
        rendered = json.dumps(manifest)
        self.assertNotIn("secret", rendered.lower())
        self.assertNotIn("webhook_url", rendered.lower())


if __name__ == "__main__":
    unittest.main()

import base64
import unittest

from voice_service import (
    VoiceServiceError,
    synthesize_speech,
    transcribe_speech,
    validate_voice_endpoint,
)


class FakeResponse:
    def __init__(self, *, status=200, content=b"", content_type="audio/mpeg", payload=None, text=""):
        self.status_code = status
        self.content = content
        self.headers = {"content-type": content_type}
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class VoiceServiceTests(unittest.TestCase):
    def test_endpoint_validation_accepts_self_hosting_but_rejects_unsafe_schemes(self):
        self.assertEqual(
            validate_voice_endpoint("http://127.0.0.1:8080/tts"),
            "http://127.0.0.1:8080/tts",
        )
        with self.assertRaises(ValueError):
            validate_voice_endpoint("file:///tmp/audio")
        with self.assertRaises(ValueError):
            validate_voice_endpoint("https://user:pass@example.com/tts")
        with self.assertRaises(ValueError):
            validate_voice_endpoint("http://169.254.1.1/tts")

    def test_openai_compatible_tts_uses_expected_contract(self):
        captured = {}

        def send(url, **kwargs):
            captured.update(url=url, **kwargs)
            return FakeResponse(content=b"mp3")

        result = synthesize_speech(
            provider="openai_compatible",
            endpoint="https://voice.example/v1/audio/speech",
            token="secret-token",
            model="tts-model",
            voice_id="alloy",
            text="你好",
            request_func=send,
        )
        self.assertEqual(result.content, b"mp3")
        self.assertEqual(captured["headers"]["Authorization"], "Bearer secret-token")
        self.assertEqual(captured["json"], {
            "model": "tts-model",
            "input": "你好",
            "voice": "alloy",
            "response_format": "mp3",
        })

    def test_custom_tts_accepts_base64_json(self):
        def send(_url, **_kwargs):
            return FakeResponse(
                content_type="application/json",
                payload={
                    "audio_base64": base64.b64encode(b"custom-audio").decode(),
                    "mime_type": "audio/ogg",
                },
            )

        result = synthesize_speech(
            provider="custom_http",
            endpoint="https://voice.example/tts",
            token="",
            model="local-model",
            voice_id="char-one",
            text="你好",
            response_format="opus",
            request_func=send,
        )
        self.assertEqual(result.content, b"custom-audio")
        self.assertEqual(result.mime_type, "audio/ogg")

    def test_stt_uses_multipart_contract(self):
        captured = {}

        def send(url, **kwargs):
            captured.update(url=url, **kwargs)
            return FakeResponse(content_type="application/json", payload={"text": "听写完成"})

        text = transcribe_speech(
            provider="openai_compatible",
            endpoint="https://voice.example/v1/audio/transcriptions",
            token="stt-secret",
            model="transcribe-model",
            filename="iphone.m4a",
            mime_type="audio/mp4",
            content=b"recording",
            request_func=send,
        )
        self.assertEqual(text, "听写完成")
        self.assertEqual(captured["data"], {"model": "transcribe-model", "response_format": "json"})
        self.assertEqual(captured["files"]["file"], ("iphone.m4a", b"recording", "audio/mp4"))
        self.assertEqual(captured["headers"]["Authorization"], "Bearer stt-secret")

    def test_provider_error_redacts_saved_token(self):
        def send(_url, **_kwargs):
            return FakeResponse(
                status=401,
                content_type="application/json",
                payload={"error": {"message": "bad secret-token"}},
            )

        with self.assertRaises(VoiceServiceError) as raised:
            synthesize_speech(
                provider="openai_compatible",
                endpoint="https://voice.example/tts",
                token="secret-token",
                model="m",
                voice_id="v",
                text="x",
                request_func=send,
            )
        self.assertNotIn("secret-token", str(raised.exception))


if __name__ == "__main__":
    unittest.main()

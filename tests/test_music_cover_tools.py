import struct
import unittest

from scripts.extract_macos_music_covers import largest_png_from_icns


def fake_png(width, height, marker):
    return b"\x89PNG\r\n\x1a\n" + b"\x00" * 8 + struct.pack(">II", width, height) + marker


class MusicCoverToolsTests(unittest.TestCase):
    def test_largest_png_is_selected_from_icns(self):
        small = fake_png(64, 64, b"small")
        large = fake_png(512, 512, b"large")
        chunks = b"ic12" + struct.pack(">I", len(small) + 8) + small
        chunks += b"ic09" + struct.pack(">I", len(large) + 8) + large
        icns = b"icns" + struct.pack(">I", len(chunks) + 8) + chunks
        self.assertEqual(largest_png_from_icns(b"prefix" + icns), large)


if __name__ == "__main__":
    unittest.main()

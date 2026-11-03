import base64
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from transport import Base64Stdin


class BinaryStdinTests(unittest.TestCase):
    def test_every_byte_survives_irregular_chunks_and_windows_newlines(self):
        original = bytes(range(256)) * 37 + b'\x1a\x00\r\n\xff'
        encoder = Base64Stdin()
        pieces = []
        cursor = 0
        sizes = [1, 2, 5, 97, 768, 384]
        while cursor < len(original):
            size = sizes[len(pieces) % len(sizes)]
            pieces.append(encoder.encode(original[cursor:cursor + size]))
            cursor += size
        pieces.append(encoder.finish())
        wire = b''.join(pieces)
        self.assertNotIn(b'\x1a', wire)
        self.assertTrue(all(b < 128 for b in wire))
        self.assertEqual(base64.b64decode(wire.replace(b'\n', b'\r\n')), original)

    def test_one_and_two_byte_tails_are_flushed(self):
        for data in [b'x', b'xy', b'xyz', b'xyzq', b'']:
            encoder = Base64Stdin()
            output = b''.join(encoder.encode(bytes([value])) for value in data) + encoder.finish()
            self.assertEqual(base64.b64decode(output), data)
            self.assertEqual(encoder.finish(), b'')


if __name__ == '__main__':
    unittest.main()

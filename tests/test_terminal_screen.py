import unittest

try:
    import pyte
    from terminal_screen import WideScreen
except ImportError:
    pyte = None


@unittest.skipUnless(pyte, "install .[test] for terminal decoding")
class WideScreenTests(unittest.TestCase):
    def test_overwrite_and_partial_erasure_clear_the_whole_old_glyph(self):
        cases = {
            "narrow_over_leader": ("中\rA", "A     "),
            "narrow_over_tail": ("中\r\x1b[CA", " A    "),
            "wide_overlap": ("A中B\r中", "中 B  "),
            "erase_leader": ("中Z\r\x1b[X", "  Z   "),
            "erase_tail": ("中Z\r\x1b[C\x1b[X", "  Z   "),
            "erase_line_start": ("中Z\r\x1b[1K", "  Z   "),
            "erase_display_start": ("中Z\r\x1b[1J", "  Z   "),
        }
        for name, (sequence, expected) in cases.items():
            with self.subTest(name=name):
                screen = WideScreen(6, 1)
                pyte.ByteStream(screen).feed(sequence.encode())
                self.assertEqual(screen.display, [expected])

    def test_insert_and_delete_preserve_unsplit_glyphs_and_clear_split_edges(self):
        cases = {
            "delete_leader": ("中Z\r\x1b[P", " Z    "),
            "delete_tail": ("中Z\r\x1b[C\x1b[P", " Z    "),
            "delete_whole": ("中Z\r\x1b[2P", "Z     "),
            "insert_at_tail": ("中Z\r\x1b[C\x1b[@", "   Z  "),
            "insert_clips_tail": ("AB中ZQ\r\x1b[3@", "   AB "),
            "insert_preserves_whole": ("中Z\r\x1b[@", " 中Z  "),
        }
        for name, (sequence, expected) in cases.items():
            with self.subTest(name=name):
                screen = WideScreen(6, 1)
                pyte.ByteStream(screen).feed(sequence.encode())
                self.assertEqual(screen.display, [expected])

    def test_fragmented_repaint_is_renderable_and_preserves_adjacent_wide_attributes(self):
        screen = WideScreen(6, 1)
        stream = pyte.ByteStream(screen)
        # Every byte boundary can be a PTY read boundary, including UTF-8/CSI.
        for byte in "中\x1b[31m文\x1b[0mZ\rA".encode():
            stream.feed(bytes([byte]))
            screen.display
        self.assertEqual(screen.display, ["A 文Z "])
        self.assertEqual(screen.buffer[0][2].data, "文")
        self.assertEqual(screen.buffer[0][2].fg, "red")
        self.assertEqual(screen.buffer[0][3].data, "")

    def test_pending_wrap_targets_the_next_row_without_erasing_the_previous_row(self):
        screen = WideScreen(6, 2)
        pyte.ByteStream(screen).feed("123456中文".encode())
        self.assertEqual(screen.display, ["123456", "中文  "])

"""PTY decoder with atomic wide-glyph mutations missing from pyte 0.8.2.

Keep display strict: repair the old glyph's footprint before overwriting,
erasing or splitting it, rather than hiding orphan continuation cells at readout.
This helper is not used by the application renderer.
"""

import unicodedata

from pyte import Screen, modes
from pyte.screens import wcwidth


class WideScreen(Screen):
    def _clear_glyph(self, column):
        line = self.buffer[self.cursor.y]
        for x in (column, column + 1):
            if 0 <= x < self.columns:
                line[x] = self.cursor.attrs

    def _clear_intersections(self, start, end):
        line = self.buffer[self.cursor.y]
        for x in range(max(0, start), min(end, self.columns)):
            data = line[x].data
            if data == "":
                self._clear_glyph(x - 1)
            elif wcwidth(data[0]) == 2:
                self._clear_glyph(x)

    def _clear_split(self, column):
        if 0 < column < self.columns and self.buffer[self.cursor.y][column].data == "":
            self._clear_glyph(column - 1)

    def draw(self, data):
        for char in data:
            translated = char.translate(self.g1_charset if self.charset else self.g0_charset)
            width = wcwidth(translated)
            # Resolve pyte's pending wrap before inspecting the target cells.
            if self.cursor.x == self.columns:
                if modes.DECAWM in self.mode:
                    self.carriage_return()
                    self.linefeed()
                elif width > 0:
                    self.cursor.x -= width
            if width > 0 and modes.IRM not in self.mode:
                self._clear_intersections(self.cursor.x, self.cursor.x + width)
            super().draw(char)
            if width < 0 or (width == 0 and not unicodedata.combining(translated)):
                break

    def erase_characters(self, count=None):
        self._clear_intersections(self.cursor.x, self.cursor.x + (count or 1))
        super().erase_characters(count)

    def erase_in_line(self, how=0, private=False):
        if how == 0:
            self._clear_intersections(self.cursor.x, self.columns)
        elif how == 1:
            self._clear_intersections(0, self.cursor.x + 1)
        elif how == 2:
            self._clear_intersections(0, self.columns)
        super().erase_in_line(how, private)

    def delete_characters(self, count=None):
        self._clear_split(self.cursor.x)
        self._clear_split(min(self.columns, self.cursor.x + (count or 1)))
        super().delete_characters(count)

    def insert_characters(self, count=None):
        self._clear_split(self.cursor.x)
        self._clear_split(max(0, self.columns - (count or 1)))
        super().insert_characters(count)

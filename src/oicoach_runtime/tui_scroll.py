"""Mouse and page scrolling for a read-only, wrapping conversation window."""

from bisect import bisect_right

from prompt_toolkit.layout.containers import Window
from prompt_toolkit.mouse_events import MouseButton, MouseEventType


class TranscriptWindow(Window):
    def __init__(self, content, *, following, set_following, invalidate):
        super().__init__(content, wrap_lines=True, always_hide_cursor=True)
        self.following, self.set_following, self.invalidate = following, set_following, invalidate
        self.top, self.total, self.viewport_height = 0, 1, 1
        self.drag_offset = None
        self._heights_key, self._offsets = None, [0, 1]

    @property
    def maximum(self):
        return max(0, self.total - self.viewport_height)

    def _scroll_when_linewrapping(self, ui_content, width, height):
        # The native Window always keeps its buffer cursor visible. This pane
        # is never focused: scrolling it must not move focus out of the composer
        # or snap back to the hidden cursor. Only tail mode uses native tracking.
        key = (self.content.buffer.text, width)
        if key != self._heights_key:
            offsets = [0]
            for row in range(ui_content.line_count):
                offsets.append(offsets[-1] + ui_content.get_height_for_line(row, max(1, width), None))
            self._offsets, self._heights_key = offsets, key
        self.total, self.viewport_height = self._offsets[-1], max(1, height)
        if self.following():
            super()._scroll_when_linewrapping(ui_content, width, height)
            self.top = min(self.maximum, self._offsets[self.vertical_scroll] + self.vertical_scroll_2)
        else:
            self.top = max(0, min(self.maximum, self.top))
            row = min(len(self._offsets) - 2, bisect_right(self._offsets, self.top) - 1)
            self.horizontal_scroll = 0
            self.vertical_scroll, self.vertical_scroll_2 = row, self.top - self._offsets[row]

    def scroll_to(self, top):
        self.top = max(0, min(self.maximum, round(top)))
        tail = self.top == self.maximum
        self.set_following(tail)
        if tail:
            self.content.buffer.cursor_position = len(self.content.buffer.text)
        self.invalidate()

    def scroll(self, amount):
        self.scroll_to(self.top + amount)

    def page(self, direction):
        self.scroll(direction * max(1, self.viewport_height - 1))

    def _geometry(self):
        track = max(1, self.viewport_height - 2)
        thumb = max(1, min(track, round(track * self.viewport_height / max(1, self.total))))
        travel = max(0, track - thumb)
        top = round(travel * self.top / self.maximum) if self.maximum else 0
        return track, thumb, travel, top

    def _drag(self, row):
        _, _, travel, _ = self._geometry()
        self.scroll_to((row - 1 - self.drag_offset) / max(1, travel) * self.maximum)

    def bar_mouse(self, event):
        row, kind = event.position.y, event.event_type
        if kind in (MouseEventType.SCROLL_UP, MouseEventType.SCROLL_DOWN):
            self.scroll(-3 if kind == MouseEventType.SCROLL_UP else 3)
        elif kind == MouseEventType.MOUSE_DOWN and event.button == MouseButton.LEFT:
            if row == 0:
                self.scroll(-3)
            elif row >= self.viewport_height - 1:
                self.scroll(3)
            else:
                _, thumb, _, top = self._geometry()
                self.drag_offset = row - 1 - top if top <= row - 1 < top + thumb else thumb // 2
                self._drag(row)
        elif kind == MouseEventType.MOUSE_MOVE and event.button == MouseButton.LEFT and self.drag_offset is not None:
            self._drag(row)
        elif kind == MouseEventType.MOUSE_UP:
            self.drag_offset = None
        else:
            return NotImplemented
        return None

    def _mouse_handler(self, event):
        if event.event_type in (MouseEventType.SCROLL_UP, MouseEventType.SCROLL_DOWN):
            self.scroll(-3 if event.event_type == MouseEventType.SCROLL_UP else 3)
            return None
        if self.drag_offset is not None:
            if event.event_type == MouseEventType.MOUSE_UP:
                self.drag_offset = None
                return None
            if event.event_type == MouseEventType.MOUSE_MOVE and event.button == MouseButton.LEFT and self.render_info:
                # Continue dragging when the pointer leaves the thin scrollbar.
                self._drag(event.position.y - self.render_info._y_offset)
                return None
        return NotImplemented

    def scrollbar(self):
        _, thumb, _, top = self._geometry()
        rows = []
        for row in range(self.viewport_height):
            if row == 0:
                style, text = "class:scrollbar.arrow", "▲"
            elif row == self.viewport_height - 1:
                style, text = "class:scrollbar.arrow", "▼"
            elif top <= row - 1 < top + thumb:
                style, text = "class:scrollbar.thumb", "█"
            else:
                style, text = "class:scrollbar.track", "│"
            rows.append((style, text, self.bar_mouse))
            if row < self.viewport_height - 1:
                rows.append(("", "\n"))
        return rows

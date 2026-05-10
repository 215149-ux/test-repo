"""
OccupancyTracker: holds the current board state and relays
CellEvents to any number of subscribers.
"""

from typing import Callable, List
from .matrix_scanner import CellEvent


class OccupancyTracker:
    def __init__(self, rows: int, cols: int):
        self.rows = rows
        self.cols = cols
        self._grid: List[List[int]] = [[0] * cols for _ in range(rows)]
        self._subs: List[Callable[[CellEvent], None]] = []

    # Scanner-facing entry point
    def on_cell_event(self, ev: CellEvent):
        self._grid[ev.row][ev.col] = ev.state
        for cb in self._subs:
            try:
                cb(ev)
            except Exception:
                import traceback; traceback.print_exc()

    # Subscription API for higher layers
    def subscribe(self, cb: Callable[[CellEvent], None]):
        self._subs.append(cb)

    def snapshot(self) -> List[List[int]]:
        return [row[:] for row in self._grid]

    def count_occupied(self) -> int:
        return sum(sum(row) for row in self._grid)

    def at(self, row: int, col: int) -> int:
        return self._grid[row][col]

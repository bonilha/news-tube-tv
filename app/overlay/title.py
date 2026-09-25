"""Fit the on-air title to one line, ending with an ellipsis when it would leave the canvas."""
from __future__ import annotations

import ctypes
from ctypes import wintypes

_ELLIPSIS = "…"
_LOGFONT_FACE = 32


class _LOGFONTW(ctypes.Structure):
    _fields_ = [
        ("lfHeight", wintypes.LONG),
        ("lfWidth", wintypes.LONG),
        ("lfEscapement", wintypes.LONG),
        ("lfOrientation", wintypes.LONG),
        ("lfWeight", wintypes.LONG),
        ("lfItalic", wintypes.BYTE),
        ("lfUnderline", wintypes.BYTE),
        ("lfStrikeOut", wintypes.BYTE),
        ("lfCharSet", wintypes.BYTE),
        ("lfOutPrecision", wintypes.BYTE),
        ("lfClipPrecision", wintypes.BYTE),
        ("lfQuality", wintypes.BYTE),
        ("lfPitchAndFamily", wintypes.BYTE),
        ("lfFaceName", wintypes.WCHAR * _LOGFONT_FACE),
    ]


class _SIZE(ctypes.Structure):
    _fields_ = [("cx", wintypes.LONG), ("cy", wintypes.LONG)]


def text_width(face: str, size: int, bold: bool, text: str) -> int:
    """Pixel width of one line in the same face and size the OBS text source uses."""
    if not text:
        return 0
    user = ctypes.windll.user32
    gdi = ctypes.windll.gdi32
    hdc = user.GetDC(0)
    if not hdc:
        return _approx_width(size, bold, text)
    font = _LOGFONTW()
    font.lfHeight = -max(1, int(size))
    font.lfWeight = 700 if bold else 400
    font.lfQuality = 5
    font.lfFaceName = (face or "Arial")[: _LOGFONT_FACE - 1]
    handle = gdi.CreateFontIndirectW(ctypes.byref(font))
    if not handle:
        user.ReleaseDC(0, hdc)
        return _approx_width(size, bold, text)
    old = gdi.SelectObject(hdc, handle)
    size_out = _SIZE()
    ok = gdi.GetTextExtentPoint32W(hdc, text, len(text), ctypes.byref(size_out))
    gdi.SelectObject(hdc, old)
    gdi.DeleteObject(handle)
    user.ReleaseDC(0, hdc)
    if not ok:
        return _approx_width(size, bold, text)
    return int(size_out.cx)


def _approx_width(size: int, bold: bool, text: str) -> int:
    factor = 0.62 if bold else 0.55
    return int(len(text) * max(1, size) * factor)


def fit_line(text: str, face: str, size: int, bold: bool, max_px: float) -> str:
    """One line. A title that fits is unchanged. A longer one is cut and ends with …."""
    single = " ".join((text or "").split())
    if not single or max_px <= 0:
        return single
    if text_width(face, size, bold, single) <= max_px:
        return single
    if text_width(face, size, bold, _ELLIPSIS) > max_px:
        return _ELLIPSIS
    lo, hi = 0, len(single)
    best = _ELLIPSIS
    while lo <= hi:
        mid = (lo + hi) // 2
        candidate = single[:mid].rstrip() + _ELLIPSIS
        if text_width(face, size, bold, candidate) <= max_px:
            best = candidate
            lo = mid + 1
        else:
            hi = mid - 1
    return best

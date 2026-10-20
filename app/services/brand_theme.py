"""Accessible design tokens derived from a brand's colours (01-white-label §5.3).

A brand never edits CSS variables directly. It supplies a primary colour (and
optionally an accent and a sidebar style); this module derives the light and dark
token sets so that text keeps WCAG 2.1 AA contrast (4.5:1) and non-text UI keeps
3:1. Status colours (success, warning, danger, info) are never derived or changed.

Token values are "H S% L%" triples, the format used by src/app/globals.css.
"""

import colorsys
import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

HEX = re.compile(r"^#[0-9a-fA-F]{6}$")
RGB = Tuple[float, float, float]
HSL = Tuple[float, float, float]  # hue 0-360, saturation 0-100, lightness 0-100

WHITE: RGB = (1.0, 1.0, 1.0)
DANGER_HUE = 356.0
TEXT_AA = 4.5
NON_TEXT_AA = 3.0
SIDEBAR_BRAND_TARGET = 7.0

# Fixed neutrals from globals.css that brand colours are checked against.
DARK_TEXT_HSL: HSL = (224, 32, 12)  # --foreground (light theme)
SURFACE_LIGHT_HSL: HSL = (0, 0, 100)  # --surface (light theme)
SURFACE_DARK_HSL: HSL = (228, 26, 11)  # --surface (dark theme)
DARK_PRIMARY_FG_HSL: HSL = (240, 30, 8)

TOKEN_NAMES = (
    "primary", "primary-foreground", "primary-soft", "ring", "accent", "accent-foreground",
    "sidebar", "sidebar-foreground", "sidebar-muted", "sidebar-active", "sidebar-active-foreground", "shadow",
)


def is_hex(value: Optional[str]) -> bool:
    return bool(value) and bool(HEX.match(value))


def hex_to_rgb(value: str) -> RGB:
    if not is_hex(value):
        raise ValueError(f"Not a #RRGGBB colour: {value!r}")
    return tuple(int(value[i : i + 2], 16) / 255 for i in (1, 3, 5))  # type: ignore[return-value]


def rgb_to_hex(rgb: RGB) -> str:
    return "#" + "".join(f"{round(max(0.0, min(1.0, c)) * 255):02X}" for c in rgb)


def rgb_to_hsl(rgb: RGB) -> HSL:
    h, l, s = colorsys.rgb_to_hls(*rgb)
    return (h * 360, s * 100, l * 100)


def hsl_to_rgb(hsl: HSL) -> RGB:
    h, s, l = hsl
    return colorsys.hls_to_rgb((h % 360) / 360, max(0, min(100, l)) / 100, max(0, min(100, s)) / 100)


def _channel(c: float) -> float:
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def relative_luminance(rgb: RGB) -> float:
    r, g, b = (_channel(c) for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a: RGB, b: RGB) -> float:
    la, lb = relative_luminance(a), relative_luminance(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def token(hsl: HSL) -> str:
    h, s, l = hsl
    return f"{round(h % 360)} {round(max(0, min(100, s)))}% {round(max(0, min(100, l)))}%"


def _rgb(hsl: HSL) -> RGB:
    # Contrast is always measured on the rounded token that the browser will render.
    h, s, l = (round(hsl[0] % 360), round(hsl[1]), round(hsl[2]))
    return hsl_to_rgb((h, s, l))


@dataclass
class Check:
    check: str
    ratio: float
    required: float
    adjusted: bool = False

    @property
    def passed(self) -> bool:
        return self.ratio >= self.required

    def as_dict(self) -> dict:
        return {
            "check": self.check, "ratio": round(self.ratio, 2), "required": self.required,
            "pass": self.passed, "adjusted": self.adjusted,
        }


def _light_primary(hsl: HSL) -> Tuple[HSL, HSL, bool]:
    """(background, foreground, adjusted): white text if reachable within 15 points, else dark text."""
    h, s, l = hsl
    candidate = (h, s, l)
    for step in range(16):
        candidate = (h, s, l - step)
        if contrast(WHITE, _rgb(candidate)) >= TEXT_AA:
            return candidate, (0, 0, 100), step > 0
    dark = DARK_TEXT_HSL
    # Light colours (yellow, lime, sky): keep the brand lightness and use dark text,
    # lightening further only if the original is still too dark for dark text.
    for step in range(0, 101):
        candidate = (h, s, min(100, l + step))
        if contrast(_rgb(dark), _rgb(candidate)) >= TEXT_AA:
            return candidate, dark, step > 0
    return (h, s, 100), dark, True  # unreachable: white always passes with dark text


def _dark_primary(hsl: HSL) -> Tuple[HSL, bool]:
    h, s, _ = hsl
    s = min(s, 90)
    surface = _rgb(SURFACE_DARK_HSL)
    fg = _rgb(DARK_PRIMARY_FG_HSL)
    for lightness in range(68, 101):
        candidate = (h, s, lightness)
        rgb = _rgb(candidate)
        if contrast(rgb, surface) >= TEXT_AA and contrast(rgb, fg) >= TEXT_AA:
            return candidate, lightness != 68
    return (h, s, 100), True


def _ui_colour(hsl: HSL, against: RGB, start_lightness: Optional[float], direction: int, target: float) -> Tuple[HSL, bool]:
    h, s, l = hsl
    lightness = l if start_lightness is None else start_lightness
    for step in range(0, 101):
        candidate = (h, s, max(0, min(100, lightness + direction * step)))
        if contrast(_rgb(candidate), against) >= target:
            return candidate, step > 0 or start_lightness is not None
    return (h, s, 0 if direction < 0 else 100), True


def _foreground_for(background: HSL) -> HSL:
    rgb = _rgb(background)
    return (0, 0, 100) if contrast(WHITE, rgb) >= contrast(_rgb(DARK_TEXT_HSL), rgb) else DARK_TEXT_HSL


def derive_tokens(primary_hex: str, accent_hex: Optional[str] = None, sidebar_style: str = "DARK") -> dict:
    """Light and dark token sets, a contrast report and warnings for one brand."""
    sidebar_style = (sidebar_style or "DARK").upper()
    primary = rgb_to_hsl(hex_to_rgb(primary_hex))
    h, s, _ = primary
    report: List[Check] = []
    warnings: List[str] = []

    # -- light theme ---------------------------------------------------------------------
    light_primary, light_fg, adjusted = _light_primary(primary)
    report.append(Check("Text on primary button (light)", contrast(_rgb(light_fg), _rgb(light_primary)), TEXT_AA, adjusted))
    if adjusted:
        warnings.append(
            "Your brand colour was adjusted for buttons and links so that text stays readable. "
            "The exact colour is still used for logo areas, emails and documents."
        )
    light = {
        "primary": token(light_primary),
        "primary-foreground": token(light_fg),
        "primary-soft": token((h, min(s, 100), 96)),
        "ring": token(light_primary),
        "shadow": token((h, 40, 20)),
    }

    # -- dark theme ----------------------------------------------------------------------
    dark_primary, dark_adjusted = _dark_primary(primary)
    report.append(Check("Primary on dark surface", contrast(_rgb(dark_primary), _rgb(SURFACE_DARK_HSL)), TEXT_AA, dark_adjusted))
    dark = {
        "primary": token(dark_primary),
        "primary-foreground": token(DARK_PRIMARY_FG_HSL),
        "primary-soft": token((h, 45, 18)),
        "ring": token(dark_primary),
        "shadow": token((h, 50, 2)),
    }

    # -- accent (non-text UI: 3:1) ---------------------------------------------------------
    accent = rgb_to_hsl(hex_to_rgb(accent_hex)) if is_hex(accent_hex) else ((h + 32) % 360, max(55, min(s, 80)), 45)
    light_accent, a_adj = _ui_colour(accent, _rgb(SURFACE_LIGHT_HSL), None, -1, NON_TEXT_AA)
    dark_accent, _ = _ui_colour(accent, _rgb(SURFACE_DARK_HSL), 55, +1, NON_TEXT_AA)
    report.append(Check("Accent on light surface", contrast(_rgb(light_accent), _rgb(SURFACE_LIGHT_HSL)), NON_TEXT_AA, a_adj))
    light.update({"accent": token(light_accent), "accent-foreground": token(_foreground_for(light_accent))})
    dark.update({"accent": token(dark_accent), "accent-foreground": token(_foreground_for(dark_accent))})

    # -- sidebar ---------------------------------------------------------------------------
    active_fg = light_fg
    if sidebar_style == "LIGHT":
        light.update({
            "sidebar": "0 0% 100%", "sidebar-foreground": token(DARK_TEXT_HSL), "sidebar-muted": "220 10% 40%",
        })
    elif sidebar_style == "BRAND":
        sidebar_bg, _ = _ui_colour((h, s, primary[2]), WHITE, None, -1, SIDEBAR_BRAND_TARGET)
        light.update({
            "sidebar": token(sidebar_bg), "sidebar-foreground": "0 0% 100%",
            "sidebar-muted": token(_muted_on(sidebar_bg)),
        })
    else:
        light.update({"sidebar": token((h, 40, 12)), "sidebar-foreground": "228 30% 92%", "sidebar-muted": "230 20% 62%"})
    light.update({"sidebar-active": token(light_primary), "sidebar-active-foreground": token(active_fg)})
    dark_sidebar = (h, min(s, 45), 9) if sidebar_style == "BRAND" else (h, 30, 6)
    dark.update({
        "sidebar": token(dark_sidebar), "sidebar-foreground": "228 30% 92%", "sidebar-muted": "230 15% 62%",
        "sidebar-active": token(light_primary), "sidebar-active-foreground": token(active_fg),
    })
    report.append(Check(
        "Navigation text on sidebar (light)",
        contrast(_rgb(_parse_token(light["sidebar-foreground"])), _rgb(_parse_token(light["sidebar"]))), TEXT_AA,
    ))
    report.append(Check(
        "Active navigation item",
        contrast(_rgb(_parse_token(light["sidebar-active-foreground"])), _rgb(_parse_token(light["sidebar-active"]))), TEXT_AA,
    ))

    # -- warnings -------------------------------------------------------------------------
    hue_gap = min(abs(h - DANGER_HUE), 360 - abs(h - DANGER_HUE))
    if hue_gap <= 15 and s > 50:
        warnings.append(
            "Your brand colour is close to the error colour. Errors will still show an icon and a label."
        )

    return {
        "light": light,
        "dark": dark,
        "exact": {"primary": primary_hex.upper(), "accent": accent_hex.upper() if is_hex(accent_hex) else None},
        "theme_color": primary_hex.upper(),
        "contrast_report": [c.as_dict() for c in report],
        "warnings": warnings,
    }


def _muted_on(background: HSL) -> HSL:
    h = background[0]
    for lightness in range(85, 49, -1):
        candidate = (h, 30, lightness)
        if contrast(_rgb(candidate), _rgb(background)) >= TEXT_AA:
            return candidate
    return (0, 0, 100)


def _parse_token(value: str) -> HSL:
    h, s, l = value.replace("%", "").split()
    return (float(h), float(s), float(l))


def button_colors(primary_hex: str) -> Tuple[str, str]:
    """(background, text) hex pair used for primary buttons in the light theme."""
    background, foreground, _ = _light_primary(rgb_to_hsl(hex_to_rgb(primary_hex)))
    return rgb_to_hex(hsl_to_rgb(background)), rgb_to_hex(hsl_to_rgb(foreground))


def accessible_accent_hex(hex_value: str) -> str:
    """Exact brand colour, darkened only as much as needed for white text (PDF bands, 01 §5.7)."""
    hsl = rgb_to_hsl(hex_to_rgb(hex_value))
    h, s, l = hsl
    for step in range(0, 101):
        candidate = (h, s, max(0, l - step))
        if contrast(WHITE, hsl_to_rgb(candidate)) >= TEXT_AA:
            return rgb_to_hex(hsl_to_rgb(candidate))
    return "#000000"


TOKEN_VALUE = re.compile(r"^\d{1,3} \d{1,3}% \d{1,3}%$")


def tokens_are_safe(tokens: Dict[str, Dict[str, str]]) -> bool:
    """Defence in depth before tokens are ever written into a <style> element."""
    for mode in ("light", "dark"):
        values = tokens.get(mode) or {}
        for name, value in values.items():
            if name not in TOKEN_NAMES or not isinstance(value, str) or not TOKEN_VALUE.match(value):
                return False
    return True

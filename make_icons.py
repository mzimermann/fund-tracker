"""Génère les icônes de la PWA Smart Money Radar.
Motif : fond sombre + arcs « radar » + courbe ascendante (signal) verte + point lumineux.
Sorties : icon-192.png, icon-512.png, icon-512-maskable.png, apple-touch-icon.png (180)."""
from PIL import Image, ImageDraw, ImageFilter
import math

BG_TOP   = (16, 21, 27)     # #10151b
BG_BOT   = (9, 12, 16)      # #090c10
GREEN    = (78, 214, 161)   # #4ed6a1
GREEN_D  = (38, 120, 92)
GRID     = (44, 54, 65)     # #2c3641
TIP_CORE = (224, 248, 238)

SPARK = [(0.05, 0.76), (0.26, 0.52), (0.42, 0.63), (0.60, 0.33), (0.78, 0.45), (0.95, 0.13)]


def _vgrad(size):
    """Fond dégradé vertical."""
    img = Image.new("RGB", (size, size), BG_BOT)
    top, bot = BG_TOP, BG_BOT
    px = img.load()
    for y in range(size):
        t = y / max(1, size - 1)
        r = int(top[0] + (bot[0] - top[0]) * t)
        g = int(top[1] + (bot[1] - top[1]) * t)
        b = int(top[2] + (bot[2] - top[2]) * t)
        for x in range(size):
            px[x, y] = (r, g, b)
    return img


def draw_icon(size, pad_frac=0.0):
    """pad_frac : marge de sécurité (pour le maskable)."""
    S = size
    base = _vgrad(S).convert("RGBA")

    # zone de contenu (réduite si maskable)
    pad = int(S * pad_frac)
    x0, y0 = pad, pad
    cw = ch = S - 2 * pad

    def P(fx, fy):
        return (x0 + fx * cw, y0 + fy * ch)

    # --- arcs radar (centre bas-gauche) ---
    arcs = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    ad = ImageDraw.Draw(arcs)
    cx, cy = P(0.07, 0.97)
    lw_arc = max(2, int(S * 0.012))
    for i, rf in enumerate((0.42, 0.62, 0.82)):
        rad = rf * cw
        bbox = [cx - rad, cy - rad, cx + rad, cy + rad]
        alpha = 120 - i * 28
        ad.arc(bbox, start=268, end=357, fill=GRID + (alpha,), width=lw_arc)
    base.alpha_composite(arcs)

    # --- lueur de la courbe ---
    glow = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    pts = [P(fx, fy) for fx, fy in SPARK]
    gd.line(pts, fill=GREEN + (180,), width=max(6, int(S * 0.075)), joint="curve")
    glow = glow.filter(ImageFilter.GaussianBlur(radius=S * 0.03))
    base.alpha_composite(glow)

    # --- courbe nette ---
    line = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    ld = ImageDraw.Draw(line)
    lw = max(4, int(S * 0.05))
    ld.line(pts, fill=GREEN + (255,), width=lw, joint="curve")
    # petits nœuds aux sommets
    for fx, fy in SPARK[:-1]:
        px, py = P(fx, fy)
        r = lw * 0.42
        ld.ellipse([px - r, py - r, px + r, py + r], fill=GREEN_D + (255,))
    base.alpha_composite(line)

    # --- point lumineux au sommet ---
    tip = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    td = ImageDraw.Draw(tip)
    tx, ty = P(*SPARK[-1])
    halo = lw * 1.7
    td.ellipse([tx - halo, ty - halo, tx + halo, ty + halo], fill=GREEN + (90,))
    rr = lw * 0.95
    td.ellipse([tx - rr, ty - rr, tx + rr, ty + rr], fill=GREEN + (255,))
    cr = lw * 0.42
    td.ellipse([tx - cr, ty - cr, tx + cr, ty + cr], fill=TIP_CORE + (255,))
    tip = tip.filter(ImageFilter.GaussianBlur(radius=max(1, S * 0.004)))
    base.alpha_composite(tip)

    return base.convert("RGB")


draw_icon(192).save("icon-192.png")
draw_icon(512).save("icon-512.png")
draw_icon(512, pad_frac=0.14).save("icon-512-maskable.png")   # zone de sécurité maskable
draw_icon(180).save("apple-touch-icon.png")                    # iOS (plein cadre, sans transparence)
print("Icônes générées : icon-192, icon-512, icon-512-maskable, apple-touch-icon")

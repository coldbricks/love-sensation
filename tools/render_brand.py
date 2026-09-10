"""Build the original Love Sensation vector banner and social image.

Requires the app's PySide6 installation. Typography is outlined in the SVG, so
the generated banner has no external font or image dependencies. Rendering
uses an OpenGL framebuffer; software rendering is used only without OpenGL.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import random

os.environ.setdefault("QT_OPENGL", "desktop")

from PySide6.QtCore import QByteArray, QRectF, QSize
from PySide6.QtGui import (
    QColor, QFont, QFontDatabase, QGuiApplication, QImage, QOffscreenSurface,
    QOpenGLContext, QPainter, QPainterPath, QSurfaceFormat, QTransform,
)
from PySide6.QtOpenGL import QOpenGLFramebufferObject, QOpenGLFramebufferObjectFormat, QOpenGLPaintDevice
from PySide6.QtSvg import QSvgRenderer


ROOT = Path(__file__).resolve().parents[1]
BLACK, SILVER, GOLD, ROSE = "#101014", "#d3d2d7", "#d8b477", "#b96b8b"


def path_data(path: QPainterPath) -> str:
    values = []
    index = 0
    while index < path.elementCount():
        element = path.elementAt(index)
        if element.isMoveTo():
            values.append(f"M{element.x:.3f},{element.y:.3f}")
        elif element.isLineTo():
            values.append(f"L{element.x:.3f},{element.y:.3f}")
        elif element.isCurveTo():
            second, third = path.elementAt(index + 1), path.elementAt(index + 2)
            values.append(f"C{element.x:.3f},{element.y:.3f} {second.x:.3f},{second.y:.3f} {third.x:.3f},{third.y:.3f}")
            index += 2
        index += 1
    return " ".join(values)


def lettering(text: str, x: float, y: float, size: int, fill: str,
              *, family="Century Gothic", weight=QFont.Bold, tracking=0.0, max_width=1000) -> str:
    available = QFontDatabase.families()
    if family not in available:
        family = "Arial"
    font = QFont(family)
    font.setPixelSize(size)
    font.setWeight(weight)
    font.setLetterSpacing(QFont.AbsoluteSpacing, tracking)
    path = QPainterPath()
    path.addText(0, 0, font, text)
    bounds = path.boundingRect()
    scale = min(1.0, max_width / bounds.width())
    transform = QTransform()
    transform.translate(x, y)
    transform.scale(scale, scale)
    transform.translate(-bounds.x(), -bounds.y())
    return f'<path aria-label="{text}" fill="{fill}" fill-rule="evenodd" d="{path_data(transform.map(path))}"/>'


def build_banner() -> str:
    # Hand-drawn crescent profile and spoon. This is original artwork, not a
    # reproduction of a venue logo, invitation, photograph, or stage prop.
    moon = """
    <g id="moon-and-spoon" aria-label="A silver crescent face and a silver spoon">
      <g id="resting-spoon" transform="translate(970 288) rotate(20)" opacity=".24">
        <path d="M27 -4 C58 -4 104 -7 138 -8 Q152 -8 154 -1 Q155 7 141 8
                 C108 6 58 4 27 4Z" fill="url(#spoonMetal)" stroke="#b7b5bf" stroke-width=".7"/>
        <path d="M-33 0 C-33 -17 -13 -24 9 -21 C29 -18 37 -10 34 0
                 C32 12 17 19 -4 17 C-22 15 -33 10 -33 0Z" fill="url(#spoonBowl)" stroke="#eeeaf0" stroke-width=".8"/>
        <path d="M-27 -2 C-24 -15 -6 -19 12 -15 C24 -12 28 -6 26 -1
                 C23 7 3 12 -11 8 C-20 6 -25 3 -27 -2Z" fill="url(#spoonInside)"/>
        <path d="M-25 -5 Q-10 -20 15 -12 M37 -2 L142 -5" fill="none" stroke="#fffdf5" stroke-width="1.1" opacity=".85"/>
      </g>
      <path d="M1033 47 C939 42 863 110 861 191 C859 275 922 338 1027 337
               C986 319 967 299 966 279 C966 267 985 263 991 254
               C995 248 987 244 982 243 C997 240 1000 234 991 229
               L973 222 C975 214 981 208 990 205 L1030 196
               Q1043 190 1030 184 L983 158 C981 140 987 121 995 105
               C1005 83 1019 63 1033 47Z" fill="url(#moonSilver)"/>
      <path d="M1017 54 C921 69 878 134 880 198 C882 267 928 310 1009 331"
            fill="none" stroke="#f3eee4" stroke-width="1.1" opacity=".65"/>
      <path d="M1004 80 C936 110 926 199 947 252 C957 277 973 298 997 316"
            fill="none" stroke="#7c7885" stroke-width="1.5" opacity=".62"/>
      <path d="M951 141 Q967 131 981 140" fill="none" stroke="#514d5b" stroke-width="3"/>
      <path d="M951 153 Q965 164 980 151 Q966 152 951 153Z" fill="#3d3946"/>
      <path d="M955 153 Q967 158 977 152" fill="none" stroke="#f3eee4" stroke-width="1.3"/>
      <circle cx="971" cy="155" r="2.6" fill="#b96b8b"/>
      <path d="M982 164 Q987 183 1008 190" fill="none" stroke="#736e7a" stroke-width="1.5"/>
      <path d="M975 222 Q983 230 990 229 M979 242 Q985 239 990 241" fill="none" stroke="#5c5665" stroke-width="1.3"/>
      <ellipse cx="926" cy="203" rx="18" ry="29" fill="#79727f" opacity=".12" transform="rotate(-17 926 203)"/>
      <path d="M902 133 Q899 142 903 150 M899 245 Q901 254 908 260 M924 99 Q931 93 938 94"
            fill="none" stroke="#fff1d9" stroke-width="1.2" opacity=".30"/>
    </g>"""
    rng = random.Random(54)
    # Fine ink texture rather than glitter: sparse subpixel marks, fixed seed.
    grain = "".join(f'<path d="M{rng.uniform(0,1280):.2f} {rng.uniform(0,420):.2f}h.65"/>' for _ in range(900))
    return f'''<svg xmlns="http://www.w3.org/2000/svg" width="1280" height="420" viewBox="0 0 1280 420" role="img" aria-labelledby="title desc">
  <title id="title">Love Sensation</title>
  <desc id="desc">Your library. After dark. Private NSFW image organizer. Original silver moon and spoon artwork on black with geometric silver and champagne lettering.</desc>
  <defs>
    <linearGradient id="silverInk" x1="0" y1="0" x2=".25" y2="1">
      <stop stop-color="#f3eee4"/><stop offset=".44" stop-color="#d3d2d7"/><stop offset="1" stop-color="#9a98a2"/>
    </linearGradient>
    <linearGradient id="moonSilver" x1=".08" y1=".1" x2=".98" y2=".48">
      <stop stop-color="#87818d"/><stop offset=".24" stop-color="#d3d2d7"/><stop offset=".49" stop-color="#eeebe3"/>
      <stop offset=".74" stop-color="#b1adb9"/><stop offset="1" stop-color="#6e6876"/>
    </linearGradient>
    <linearGradient id="spoonMetal" x1="0" y1="0" x2="0" y2="1">
      <stop stop-color="#77727e"/><stop offset=".22" stop-color="#eeedf0"/><stop offset=".52" stop-color="#d3d2d7"/>
      <stop offset=".75" stop-color="#5f5b68"/><stop offset="1" stop-color="#ece5d8"/>
    </linearGradient>
    <linearGradient id="spoonBowl" x1="0" y1="0" x2=".3" y2="1">
      <stop stop-color="#f3eee4"/><stop offset=".35" stop-color="#a6a0ae"/><stop offset=".7" stop-color="#dad6de"/><stop offset="1" stop-color="#716b7c"/>
    </linearGradient>
    <linearGradient id="spoonInside" x1="0" y1="0" x2=".3" y2="1">
      <stop stop-color="#5a5465"/><stop offset=".65" stop-color="#c8c1cc"/><stop offset="1" stop-color="#e5ddd4"/>
    </linearGradient>
  </defs>
  <rect width="1280" height="420" fill="#101014"/>
  <path d="M826 0 H1280 V420 H986 Z" fill="#141418"/>
  <g fill="none" stroke="#49464a" stroke-width=".65" opacity=".20">
    <path d="M854 -36 C1090 -96 1306 63 1291 294"/>
    <path d="M871 -18 C1093 -74 1290 72 1278 290"/>
    <path d="M887 0 C1096 -50 1274 80 1265 286"/>
  </g>
  <g stroke="#d3d2d7" stroke-width=".5" opacity=".065">{grain}</g>
  {lettering('PRIVATE NSFW IMAGE ORGANIZER', 67, 43, 13, '#aaa6aa', weight=QFont.Bold, tracking=2.5)}
  {lettering('LOVE', 62, 81, 138, 'url(#silverInk)', tracking=3.0, max_width=735)}
  {lettering('SENSATION', 63, 193, 109, 'url(#silverInk)', tracking=.1, max_width=732)}
  {lettering('Your library.  After dark.', 68, 294, 28, GOLD, family='Gill Sans MT', weight=QFont.Normal, tracking=.3)}
  <path d="M68 345 H797" stroke="#d8b477" stroke-width="1" opacity=".58"/>
  {lettering('WINDOWS  /  LOCAL  /  GPU ACCELERATED', 68, 367, 12, '#aaa6aa', weight=QFont.Bold, tracking=1.7)}
  {moon}
  <path d="M821 98h25 M833.5 85.5v25" fill="none" stroke="#d8b477" stroke-width="1.1"/>
  <path d="M1190 85h14 M1197 78v14" fill="none" stroke="#d3d2d7" stroke-width=".8" opacity=".72"/>
  <path d="M914 367 H1095" stroke="#49464a" stroke-width=".65"/>
  {lettering('LOVE SENSATION', 943, 384, 9, '#77727e', weight=QFont.Bold, tracking=2.2)}
</svg>'''


def render(svg: bytes, width: int, height: int, *, social=False) -> QImage:
    renderer = QSvgRenderer(QByteArray(svg))
    if not renderer.isValid():
        raise RuntimeError("Generated SVG is invalid.")
    surface_format = QSurfaceFormat()
    surface_format.setVersion(3, 3)
    surface_format.setProfile(QSurfaceFormat.CompatibilityProfile)
    surface_format.setSamples(4)
    surface = QOffscreenSurface()
    surface.setFormat(surface_format)
    surface.create()
    context = QOpenGLContext()
    context.setFormat(surface_format)
    gpu = context.create() and context.makeCurrent(surface)
    framebuffer = None
    if gpu:
        print(f"Brand render GPU: {context.functions().glGetString(0x1F01)}", flush=True)
        framebuffer_format = QOpenGLFramebufferObjectFormat()
        framebuffer_format.setAttachment(QOpenGLFramebufferObject.CombinedDepthStencil)
        framebuffer_format.setSamples(4)
        framebuffer = QOpenGLFramebufferObject(QSize(width, height), framebuffer_format)
        if not framebuffer.isValid() or not framebuffer.bind():
            raise RuntimeError("OpenGL framebuffer creation failed.")
        device = QOpenGLPaintDevice(width, height)
        painter = QPainter(device)
    else:
        print("Brand render CPU fallback: OpenGL context unavailable", flush=True)
        image = QImage(width, height, QImage.Format_ARGB32_Premultiplied)
        painter = QPainter(image)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.setRenderHint(QPainter.TextAntialiasing)
    painter.fillRect(QRectF(0, 0, width, height), QColor(BLACK))
    if social:
        # Preserve the full wide artwork; use its surrounding negative space
        # as a record-sleeve margin instead of cropping either the name or moon.
        renderer.render(painter, QRectF(0, 96, width, 420))
        painter.setPen(QColor(GOLD))
        painter.drawLine(68, 65, 1212, 65)
        painter.setPen(QColor("#49464a"))
        painter.drawLine(68, 568, 1212, 568)
    else:
        renderer.render(painter, QRectF(0, 0, width, height))
    painter.end()
    if gpu:
        image = framebuffer.toImage()
        framebuffer.release()
        del framebuffer
        context.doneCurrent()
    return image


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ROOT / "docs" / "images")
    args = parser.parse_args()
    app = QGuiApplication.instance() or QGuiApplication([])
    args.output.mkdir(parents=True, exist_ok=True)
    svg = build_banner().encode("utf-8")
    (args.output / "banner.svg").write_bytes(svg)
    image = render(svg, 1280, 640, social=True)
    path = args.output / "social-preview.png"
    if not image.save(str(path), "PNG"):
        raise RuntimeError("Could not save social preview.")
    print(path)


if __name__ == "__main__":
    main()

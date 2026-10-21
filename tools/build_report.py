#!/usr/bin/env python3
"""Render REPORT.md and saved figures into the single Brightspace submission PDF."""

import re
from importlib.util import find_spec
import textwrap
import xml.etree.ElementTree as ET
from pathlib import Path
from xml.sax.saxutils import escape

import markdown
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    Image, KeepTogether, PageBreak, Paragraph, Preformatted, SimpleDocTemplate,
    Spacer, Table, TableStyle,
)

ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = "https://github.com/vrajpatel-18/CS422-hw3"
PAGE = (612, 792)
MARGIN = 44
WIDTH = PAGE[0] - 2 * MARGIN
HEIGHT = PAGE[1] - 2 * MARGIN


def register_fonts():
    directory = Path(find_spec("matplotlib").origin).parent / "mpl-data" / "fonts" / "ttf"
    for name, file in [("Body", "DejaVuSans.ttf"), ("BodyBold", "DejaVuSans-Bold.ttf"),
                       ("BodyItalic", "DejaVuSans-Oblique.ttf"),
                       ("BodyBoldItalic", "DejaVuSans-BoldOblique.ttf"),
                       ("Mono", "DejaVuSansMono.ttf")]:
        pdfmetrics.registerFont(TTFont(name, str(directory / file)))
    pdfmetrics.registerFontFamily("Body", normal="Body", bold="BodyBold",
                                  italic="BodyItalic", boldItalic="BodyBoldItalic")


def link_target(target):
    if re.match(r"https?://", target):
        return target
    path, marker, anchor = target.partition("#")
    kind = "tree" if (ROOT / path).is_dir() else "blob"
    return f"{REPOSITORY}/{kind}/main/{path}" + (marker + anchor if marker else "")


def inline(element):
    result = escape(element.text or "")
    for child in element:
        contents = inline(child)
        if child.tag == "a":
            href = escape(link_target(child.attrib["href"]), {'"': '&quot;'})
            contents = f'<a href="{href}" color="#215c93">{contents}</a>'
        elif child.tag == "code":
            contents = f'<font name="Mono" size="7.5">{contents}</font>'
        elif child.tag in ("strong", "b"):
            contents = f"<b>{contents}</b>"
        elif child.tag in ("em", "i"):
            contents = f"<i>{contents}</i>"
        elif child.tag == "br":
            contents = "<br/>"
        result += contents + escape(child.tail or "")
    return result


def footer(canvas, document):
    canvas.saveState()
    canvas.setFont("Body", 8)
    canvas.setFillColor(colors.HexColor("#596574"))
    canvas.drawString(MARGIN, 25, "CS 422 · Assignment 3 · DART in eBPF")
    canvas.drawRightString(PAGE[0] - MARGIN, 25, str(document.page))
    canvas.restoreState()


def main():
    register_fonts()
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle("BodyTextA3", fontName="Body", fontSize=9, leading=13.2,
                              spaceAfter=8))
    styles.add(ParagraphStyle("TitleA3", fontName="BodyBold", fontSize=19, leading=25,
                              spaceAfter=16))
    styles.add(ParagraphStyle("HeadingA3", fontName="BodyBold", fontSize=13, leading=18,
                              spaceBefore=12, spaceAfter=8, keepWithNext=True))
    styles.add(ParagraphStyle("SubheadingA3", fontName="BodyBold", fontSize=10.5,
                              leading=15, spaceBefore=9, spaceAfter=7, keepWithNext=True))
    styles.add(ParagraphStyle("CellA3", fontName="Body", fontSize=7, leading=10))
    styles.add(ParagraphStyle("CodeA3", fontName="Mono", fontSize=7, leading=10,
                              backColor=colors.HexColor("#f2f5f8"), borderPadding=7,
                              spaceBefore=5, spaceAfter=12))
    styles.add(ParagraphStyle("CaptionA3", fontName="Body", fontSize=9, leading=13,
                              alignment=TA_CENTER, spaceAfter=12))
    html = markdown.markdown((ROOT / "REPORT.md").read_text(), extensions=["tables", "fenced_code"])
    tree = ET.fromstring("<document>" + html + "</document>")
    story = []
    captions = {
        "q3-timeseries-1.png": "Figure 1. Time series at the representative Los Angeles destination. Each metric has the same scale across all four algorithms; three repetitions are shown.",
        "q3-scatter-1.png": "Figure 2. Congestion window, RTT, and retransmissions against acknowledged goodput at the same destination, using consistent scales across algorithms.",
        "q3-compare-1.png": "Figure 3. Comparison of algorithms across all five destinations, including the estimated bandwidth-delay product.",
    }

    def add(element):
        if element.tag in ("h1", "h2", "h3"):
            style = {"h1": "TitleA3", "h2": "HeadingA3", "h3": "SubheadingA3"}[element.tag]
            story.append(Paragraph(inline(element), styles[style]))
        elif element.tag == "p":
            images = list(element.iter("img"))
            if images:
                for image in images:
                    path = ROOT / image.attrib["src"]
                    iw, ih = ImageReader(str(path)).getSize()
                    scale = min((WIDTH - 12) / iw, (HEIGHT - 85) / ih)
                    story.extend([KeepTogether([
                        Paragraph(captions.get(path.name, image.attrib.get("alt", "Figure")),
                                  styles["CaptionA3"]),
                        Image(str(path), width=iw * scale, height=ih * scale),
                    ]), PageBreak()])
            else:
                story.append(Paragraph(inline(element), styles["BodyTextA3"]))
        elif element.tag == "pre":
            lines = []
            for line in "".join(element.itertext()).rstrip().splitlines():
                lines.extend(textwrap.wrap(line.expandtabs(4), width=110, subsequent_indent="    ",
                                           replace_whitespace=False, drop_whitespace=False) or [""])
            story.append(Preformatted("\n".join(lines), styles["CodeA3"]))
        elif element.tag == "table":
            rows = element.findall(".//tr")
            cells = [list(row) for row in rows]
            # Balance columns by their longest cell, allowing prose to wrap.
            weights = [max(12, min(42, max(len("".join(row[c].itertext())) for row in cells)))
                       for c in range(len(cells[0]))]
            widths = [WIDTH * w / sum(weights) for w in weights]
            data = [[Paragraph(("<b>" + inline(cell) + "</b>") if n == 0 else inline(cell),
                               styles["CellA3"]) for cell in row] for n, row in enumerate(cells)]
            table = Table(data, colWidths=widths, repeatRows=1, hAlign="LEFT")
            table.setStyle(TableStyle([
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e6edf5")),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f7f9fb")]),
                ("LINEBELOW", (0, 0), (-1, 0), 0.6, colors.HexColor("#9aaabd")),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                ("TOPPADDING", (0, 0), (-1, -1), 6),
                ("LEFTPADDING", (0, 0), (-1, -1), 5),
                ("RIGHTPADDING", (0, 0), (-1, -1), 5),
            ]))
            story.extend([table, Spacer(1, 10)])
        elif element.tag in ("ul", "ol"):
            for index, item in enumerate(element, 1):
                bullet = f"{index}." if element.tag == "ol" else "•"
                story.append(Paragraph(inline(item), styles["BodyTextA3"], bulletText=bullet))
        else:
            raise ValueError(f"Unsupported Markdown block: {element.tag}")

    for element in tree:
        add(element)
    # Adjacent figures each request boundaries; collapse duplicate page breaks.
    story = [item for index, item in enumerate(story)
             if not (isinstance(item, PageBreak) and index > 0
                     and isinstance(story[index - 1], PageBreak))]
    output = ROOT / "REPORT.pdf"
    document = SimpleDocTemplate(str(output), pagesize=PAGE, rightMargin=MARGIN,
                                 leftMargin=MARGIN, topMargin=MARGIN, bottomMargin=MARGIN,
                                 title="Assignment 3: DART in eBPF (Option 1)",
                                 author="CS 422 Assignment 3", pageCompression=1)
    document.build(story, onFirstPage=footer, onLaterPages=footer)
    print(f"Wrote {output}")


if __name__ == "__main__":
    main()

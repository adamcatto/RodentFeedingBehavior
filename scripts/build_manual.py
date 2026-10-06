"""Build the Word manual (docs/Rodent-Feeding-Behavior-Manual.docx) from the Markdown chapters.

    uv run python scripts/build_manual.py

Needs pandoc (https://pandoc.org) on the PATH. The chapters in docs/manual are the
single source: they are also shown on the app's Help page and read well on GitHub.
Links between chapters become links within the document.
"""

from __future__ import annotations

import io
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANUAL = ROOT / "docs" / "manual"
OUT = ROOT / "docs" / "Rodent-Feeding-Behavior-Manual.docx"
sys.path.insert(0, str(ROOT / "src"))

from feeding.docs import chapters, slugify  # noqa: E402

ACCENT = "1C5CAB"


def chapter_markdown(c: dict, slugs: dict[str, str]) -> str:
    """One chapter, with heading ids prefixed by the chapter (ids repeat across chapters) and
    links to other chapters turned into links within the combined document."""
    own = c["slug"]
    text = (MANUAL / c["file"]).read_text(encoding="utf-8")
    text = re.sub(r"<kbd>(.*?)</kbd>", r"**\1**", text)

    def heading(m):
        level, title = m[1], m[2]
        return f"{level} {title} {{#{own if level == '#' else own + '--' + slugify(title)}}}"

    text = re.sub(r"^(#{1,3}) (.+)$", heading, text, flags=re.M)

    def link(m):
        file, anchor = m[1], m[2]
        target = slugs.get(file, own) if file else own
        return f"](#{target}--{anchor})" if anchor else f"](#{target})"

    return re.sub(r"\]\(([0-9]+-[a-z0-9-]+\.md)?(?:#([^)\s]+))?\)", lambda m: link(m) if (m[1] or m[2]) else m[0], text)


def reference_doc(tmp: Path) -> Path:
    """pandoc's default reference.docx with our fonts and heading colour."""
    ref = tmp / "reference.docx"
    ref.write_bytes(subprocess.run(["pandoc", "--print-default-data-file", "reference.docx"],
                                   capture_output=True, check=True).stdout)
    buf = io.BytesIO()
    with zipfile.ZipFile(ref) as zin, zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zout:
        for item in zin.infolist():
            data = zin.read(item.filename)
            if item.filename == "word/styles.xml":
                xml = data.decode("utf-8")
                xml = re.sub(r'w:ascii="[^"]*"', 'w:ascii="Calibri"', xml)
                xml = re.sub(r'w:hAnsi="[^"]*"', 'w:hAnsi="Calibri"', xml)
                xml = re.sub(r'w:eastAsia="[^"]*"', 'w:eastAsia="Calibri"', xml)
                xml = re.sub(r'w:cs="[^"]*"', 'w:cs="Calibri"', xml)
                xml = re.sub(r'\s+w:(?:ascii|hAnsi|eastAsia|cs)Theme="[^"]*"', "", xml)  # theme fonts would win
                xml = re.sub(r'<w:color w:val="[0-9A-Fa-f]{6}"', f'<w:color w:val="{ACCENT}"', xml)
                data = xml.encode("utf-8")
            if item.filename == "word/theme/theme1.xml":  # body and heading fonts
                data = re.sub(rb'(<a:(?:major|minor)Font>\s*<a:latin typeface=")[^"]*"', rb'\1Calibri"', data)
            zout.writestr(item, data)
    ref.write_bytes(buf.getvalue())
    return ref


# Schema order of the children that pandoc 2.x sometimes writes out of order (Word tolerates
# it, strict validators do not).
_ORDER = {
    "w:settings": """writeProtection view zoom removePersonalInformation removeDateAndTime doNotDisplayPageBoundaries
        displayBackgroundShape printPostScriptOverText printFractionalCharacterWidth printFormsData embedTrueTypeFonts
        embedSystemFonts saveSubsetFonts saveFormsData mirrorMargins alignBordersAndEdges bordersDoNotSurroundHeader
        bordersDoNotSurroundFooter gutterAtTop hideSpellingErrors hideGrammaticalErrors activeWritingStyle proofState
        formsDesign attachedTemplate linkStyles stylePaneFormatFilter stylePaneSortMethod documentType mailMerge
        revisionView trackRevisions doNotTrackMoves doNotTrackFormatting documentProtection autoFormatOverride
        styleLockTheme styleLockQFSet defaultTabStop autoHyphenation consecutiveHyphenLimit hyphenationZone
        doNotHyphenateCaps showEnvelope summaryLength clickAndTypeStyle defaultTableStyle evenAndOddHeaders
        bookFoldRevPrinting bookFoldPrinting bookFoldPrintingSheets drawingGridHorizontalSpacing
        drawingGridVerticalSpacing displayHorizontalDrawingGridEvery displayVerticalDrawingGridEvery
        doNotUseMarginsForDrawingGridOrigin drawingGridHorizontalOrigin drawingGridVerticalOrigin doNotShadeFormData
        noPunctuationKerning characterSpacingControl printTwoOnOne strictFirstAndLastChars noLineBreaksAfter
        noLineBreaksBefore savePreviewPicture doNotValidateAgainstSchema saveInvalidXml ignoreMixedContent
        alwaysShowPlaceholderText doNotDemarcateInvalidXml saveXmlDataOnly useXSLTWhenSaving saveThroughXslt
        showXMLTags alwaysMergeEmptyNamespace updateFields hdrShapeDefaults footnotePr endnotePr compat docVars rsids
        mathPr attachedSchema themeFontLang clrSchemeMapping doNotIncludeSubdocsInStats doNotAutoCompressPictures
        forceUpgrade captions readModeInkLockDown smartTagType schemaLibrary shapeDefaults doNotEmbedSmartTags
        decimalSymbol listSeparator""".split(),
    "w:style": """name aliases basedOn next link autoRedefine hidden uiPriority semiHidden unhideWhenUsed qFormat locked
        personal personalCompose personalReply rsid pPr rPr tblPr trPr tcPr tblStylePr""".split(),
    "w:tcPr": "cnfStyle tcW gridSpan hMerge vMerge tcBorders shd noWrap tcMar textDirection tcFitText vAlign hideMark".split(),
}
_CHILD = re.compile(r"\s*(<(\w+):(\w+)\b[^>]*?(?:/>|>.*?</\2:\3>))", re.S)


def _reorder(xml: str, parent: str) -> str:
    order = {n: i for i, n in enumerate(_ORDER[parent])}

    def fix(m):
        kids, pos, body = [], 0, m[2]
        while body[pos:].strip():
            k = _CHILD.match(body, pos)
            if not k:
                return m[0]  # not a simple sequence of elements: leave as is
            kids.append(k[1])
            pos = k.end()
        kids.sort(key=lambda e: order.get(re.match(r"<\w+:(\w+)", e)[1], len(order)))
        return m[1] + "".join(kids) + m[3]

    return re.sub(rf"(<{parent}\b[^>]*>)(.*?)(</{parent}>)", fix, xml, flags=re.S)


def tidy_docx(path: Path) -> None:
    """Fix pandoc's schema-order slips and declare the PNG content type."""
    buf = io.BytesIO()
    with zipfile.ZipFile(path) as zin, zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zout:
        for item in zin.infolist():
            data = zin.read(item.filename)
            name = item.filename
            if name in ("[Content_Types].xml", "word/document.xml", "word/settings.xml", "word/styles.xml",
                        "word/numbering.xml"):
                xml = data.decode("utf-8")
                if name == "[Content_Types].xml" and 'Extension="png"' not in xml:
                    xml = xml.replace("<Default ", '<Default Extension="png" ContentType="image/png" /><Default ', 1)
                if name == "word/document.xml":  # pStyle comes first in a paragraph's properties
                    xml = re.sub(r"<w:pPr>((?:(?!</w:pPr>).)*?)(<w:pStyle [^>]*/>)", r"<w:pPr>\2\1", xml, flags=re.S)
                if name == "word/settings.xml":
                    xml = _reorder(xml, "w:settings")
                if name == "word/styles.xml":
                    xml = _reorder(_reorder(xml, "w:tcPr"), "w:style")
                if name == "word/numbering.xml":  # nsid is 8 hex digits
                    xml = re.sub(r'(<w:nsid w:val=")([0-9A-Fa-f]{1,7})(")', lambda m: m[1] + m[2].zfill(8) + m[3], xml)
                data = xml.encode("utf-8")
            zout.writestr(item, data)
    path.write_bytes(buf.getvalue())


def main() -> None:
    if not shutil.which("pandoc"):
        sys.exit("pandoc is not installed: see https://pandoc.org/installing.html")
    chs = chapters()
    slugs = {c["file"]: c["slug"] for c in chs}
    page_break = '\n\n```{=openxml}\n<w:p><w:r><w:br w:type="page"/></w:r></w:p>\n```\n\n'
    md = [page_break + chapter_markdown(c, slugs) for c in chs]  # each chapter on a new page
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        src = tmp / "manual.md"
        meta = (f"---\ntitle: Rodent Feeding Behavior\nsubtitle: User manual\n"
                f"date: {date.today():%B %Y}\n---\n\n")
        src.write_text(meta + "\n\n".join(md), encoding="utf-8")
        subprocess.run(["pandoc", str(src), "-f", "markdown-implicit_figures", "-t", "docx", "-o", str(OUT),
                        "--toc", "--toc-depth=2", "--resource-path", str(MANUAL),
                        "--reference-doc", str(reference_doc(tmp))], check=True)
    tidy_docx(OUT)
    print(f"wrote {OUT.relative_to(ROOT)} ({OUT.stat().st_size / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()

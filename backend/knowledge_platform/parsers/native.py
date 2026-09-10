"""Dependency-light local parsers for bounded file ingestion."""
from __future__ import annotations

import csv
import io
import posixpath
import zipfile
from lxml import etree

from .contracts import ParsedDocument, ParsedMedia


class NativeParserError(ValueError):
    pass


class NativeParser:
    parser_id = "native"
    version = "native-v1"

    def __init__(self, *, max_bytes: int = 32 * 1024 * 1024, max_zip_files: int = 256, max_uncompressed_bytes: int = 64 * 1024 * 1024):
        if any(type(value) is not int or value <= 0 for value in (max_bytes, max_zip_files, max_uncompressed_bytes)):
            raise ValueError("native parser limits must be positive integers")
        self.max_bytes, self.max_zip_files, self.max_uncompressed_bytes = max_bytes, max_zip_files, max_uncompressed_bytes

    def health(self) -> bool:
        return True

    def parse(self, filename: str, content: bytes) -> ParsedDocument:
        if not isinstance(filename, str) or not filename or not isinstance(content, bytes): raise NativeParserError("native input is invalid")
        if len(content) > self.max_bytes: raise NativeParserError("native input exceeds size limit")
        suffix = filename.rsplit(".", 1)[-1].lower() if "." in filename.rsplit("/", 1)[-1] else ""
        if suffix in {"md", "markdown", "txt"}: return ParsedDocument(self._text(content), (), self.parser_id, self.version)
        if suffix in {"csv", "tsv"}: return ParsedDocument(self._delimited(content, "\t" if suffix == "tsv" else ","), (), self.parser_id, self.version)
        if suffix == "docx": return self._docx(content)
        raise NativeParserError(f"native parser does not support .{suffix or 'unknown'}")

    @staticmethod
    def _text(content: bytes) -> bytes:
        try: return content.decode("utf-8").encode("utf-8")
        except UnicodeDecodeError as exc: raise NativeParserError("text is not valid UTF-8") from exc

    @staticmethod
    def _delimited(content: bytes, delimiter: str) -> bytes:
        try: text = content.decode("utf-8")
        except UnicodeDecodeError as exc: raise NativeParserError("delimited file is not valid UTF-8") from exc
        rows = list(csv.reader(io.StringIO(text), delimiter=delimiter))
        if not rows: return b""
        width = len(rows[0]); lines = ["| " + " | ".join(str(cell).replace("|", "\\|").replace("\n", " ") for cell in row) + " |" for row in rows]
        separator = "| " + " | ".join("---" for _ in range(width)) + " |"
        return ("\n".join([lines[0], separator, *lines[1:]]) + "\n").encode("utf-8")

    def _docx(self, content: bytes) -> ParsedDocument:
        try: archive = zipfile.ZipFile(io.BytesIO(content))
        except zipfile.BadZipFile as exc: raise NativeParserError("DOCX is not a valid ZIP") from exc
        if len(archive.infolist()) > self.max_zip_files: raise NativeParserError("DOCX contains too many ZIP entries")
        total = 0; names = set()
        for info in archive.infolist():
            name = info.filename.rstrip("/")
            if not name or name in names: raise NativeParserError("DOCX contains duplicate paths")
            names.add(name)
            if "\\" in name or name.startswith("/") or any(part in {"", ".", ".."} for part in name.split("/")): raise NativeParserError("DOCX contains an unsafe ZIP path")
            if (info.external_attr >> 16) & 0o170000 == 0o120000: raise NativeParserError("DOCX contains a ZIP link")
            if info.is_dir(): continue
            if info.file_size > self.max_uncompressed_bytes or total + info.file_size > self.max_uncompressed_bytes: raise NativeParserError("DOCX exceeds ZIP size limit")
            total += info.file_size
        try: xml = archive.read("word/document.xml")
        except KeyError as exc: raise NativeParserError("DOCX document.xml is missing") from exc
        parser = etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False, recover=False)
        try: root = etree.fromstring(xml, parser)
        except etree.XMLSyntaxError as exc: raise NativeParserError("DOCX XML is invalid") from exc
        ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main", "a": "http://schemas.openxmlformats.org/drawingml/2006/main", "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships"}
        rels = {}
        try: rel_xml = archive.read("word/_rels/document.xml.rels")
        except KeyError: rel_xml = None
        if rel_xml is not None:
            try: rel_root = etree.fromstring(rel_xml, parser)
            except etree.XMLSyntaxError as exc: raise NativeParserError("DOCX relationships XML is invalid") from exc
            for rel in rel_root:
                rid = rel.get("Id"); target = rel.get("Target"); mode = rel.get("TargetMode"); kind = rel.get("Type", "")
                if not rid or not target or rid in rels: raise NativeParserError("DOCX contains duplicate or invalid relationships")
                rels[rid] = (target, mode, kind)
        assets = {}; blocks = []
        def image_for(rid):
            if rid not in rels: raise NativeParserError("DOCX image relationship is missing")
            target, mode, kind = rels[rid]
            if not kind.endswith("/image"): raise NativeParserError("DOCX drawing relationship is not an image")
            if mode == "External" or target.startswith(("http:", "https:", "//")): raise NativeParserError("DOCX external image is not allowed")
            if any(part in {"", ".", ".."} for part in target.replace("\\", "/").split("/")): raise NativeParserError("DOCX image target is unsafe")
            joined = posixpath.normpath(posixpath.join("word", target))
            if not joined.startswith("word/") or joined == "word/document.xml" or ".." in joined.split("/"): raise NativeParserError("DOCX image target escapes package")
            relative = joined[len("word/"):]
            if relative in assets: return relative
            try: body = archive.read(joined)
            except KeyError as exc: raise NativeParserError("DOCX image target is missing") from exc
            suffix = "." + relative.rsplit(".", 1)[-1].lower() if "." in relative else ""
            mime = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp"}.get(suffix)
            if mime is None: raise NativeParserError("DOCX image type is unsupported")
            assets[relative] = ParsedMedia(relative, body, mime); return relative
        for child in root.xpath("./w:body/*", namespaces=ns):
            if child.tag == "{" + ns["w"] + "}p":
                text = "".join(child.xpath(".//w:t/text()", namespaces=ns)).strip()
                images = [image_for(rid) for rid in child.xpath(".//a:blip/@r:embed", namespaces=ns)]
                rendered = " ".join(part for part in [text, *[f"![image]({path})" for path in images]] if part)
                if rendered: blocks.append(rendered)
            elif child.tag == "{" + ns["w"] + "}tbl":
                rows = []
                for row in child.xpath("./w:tr", namespaces=ns): rows.append("| " + " | ".join("".join(cell.xpath(".//w:t/text()", namespaces=ns)).replace("|", "\\|") for cell in row.xpath("./w:tc", namespaces=ns)) + " |")
                if rows:
                    width = rows[0].count("|") - 1; blocks.extend([rows[0], "| " + " | ".join("---" for _ in range(width)) + " |", *rows[1:]])
        return ParsedDocument(("\n\n".join(blocks) + ("\n" if blocks else "")).encode(), tuple(assets.values()), self.parser_id, self.version)

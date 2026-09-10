import io, zipfile
import pytest
from knowledge_platform.parsers.native import NativeParser, NativeParserError

def docx(body):
    out=io.BytesIO()
    with zipfile.ZipFile(out,"w") as z: z.writestr("word/document.xml", body)
    return out.getvalue()

def test_text_csv_tsv_and_docx_outputs():
    parser=NativeParser()
    assert parser.parse("a.md", "你好".encode()).markdown == "你好".encode()
    assert b"| a | b |" in parser.parse("a.csv", b"a,b\n1,2\n").markdown
    assert b"| a | b |" in parser.parse("a.tsv", b"a\tb\n1\t2\n").markdown
    xml=b'<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>Hello</w:t></w:r></w:p></w:body></w:document>'
    assert b"Hello" in parser.parse("a.docx", docx(xml)).markdown

def test_native_rejects_unsupported_bad_utf8_and_malicious_docx():
    parser=NativeParser()
    with pytest.raises(NativeParserError): parser.parse("a.pdf", b"%PDF")
    with pytest.raises(NativeParserError): parser.parse("a.txt", b"\xff")
    out=io.BytesIO()
    with zipfile.ZipFile(out,"w") as z: z.writestr("../evil", b"x")
    with pytest.raises(NativeParserError): parser.parse("a.docx", out.getvalue())

def test_native_explicitly_rejects_docx_media_instead_of_dropping_it():
    out=io.BytesIO()
    with zipfile.ZipFile(out,"w") as z:
        z.writestr("word/document.xml", b'''<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><w:body><w:p><w:r><w:drawing><a:blip r:embed="rId1"/></w:drawing></w:r></w:p></w:body></w:document>''')
        z.writestr("word/_rels/document.xml.rels", b'''<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="media/image1.png"/></Relationships>''')
        z.writestr("word/media/image1.png", b"png")
    result = NativeParser().parse("a.docx", out.getvalue())
    assert result.assets[0].relative_path == "media/image1.png" and b"![image](media/image1.png)" in result.markdown

@pytest.mark.parametrize("target,extra", [("https://example.com/a.png", []), ("media/missing.png", [])])
def test_docx_external_or_missing_image_fails(target, extra):
    out=io.BytesIO()
    xml=b'''<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><w:body><w:p><w:r><w:drawing><a:blip r:embed="rId1"/></w:drawing></w:r></w:p></w:body></w:document>'''
    rel=f'''<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="{target}" TargetMode="{'External' if target.startswith('http') else ''}"/></Relationships>'''.encode()
    with zipfile.ZipFile(out,"w") as z: z.writestr("word/document.xml",xml); z.writestr("word/_rels/document.xml.rels",rel)
    with pytest.raises(NativeParserError): NativeParser().parse("a.docx", out.getvalue())

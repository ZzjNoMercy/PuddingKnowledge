from __future__ import annotations

from knowledge_platform.connector_sync.feishu_blocks import convert_feishu_blocks_to_markdown


def test_docx_block_tree_converts_structure_and_surfaces_image_assets() -> None:
    blocks = [
        {"block_id": "page", "block_type": 1, "children": ["h1", "p1", "todo", "img", "table"]},
        {
            "block_id": "h1",
            "block_type": 3,
            "heading1": {"elements": [{"text_run": {"content": "产品手册"}}]},
        },
        {
            "block_id": "p1",
            "block_type": 2,
            "text": {
                "elements": [
                    {"text_run": {"content": "查看 ", "text_element_style": {}}},
                    {
                        "text_run": {
                            "content": "官方说明",
                            "text_element_style": {"bold": True, "link": {"url": "https://example.com"}},
                        }
                    },
                ]
            },
        },
        {
            "block_id": "todo",
            "block_type": 17,
            "todo": {"elements": [{"text_run": {"content": "完成同步"}}], "style": {"done": True}},
        },
        {"block_id": "img", "block_type": 27, "image": {"token": "img-token"}},
        {
            "block_id": "table",
            "block_type": 31,
            "children": ["cell1", "cell2"],
            "table": {"property": {"row_size": 1, "column_size": 2}},
        },
        {"block_id": "cell1", "block_type": 32, "children": ["cell1text"]},
        {"block_id": "cell2", "block_type": 32, "children": ["cell2text"]},
        {"block_id": "cell1text", "block_type": 2, "text": {"elements": [{"text_run": {"content": "字段"}}]}},
        {"block_id": "cell2text", "block_type": 2, "text": {"elements": [{"text_run": {"content": "说明"}}]}},
    ]

    result = convert_feishu_blocks_to_markdown(blocks)

    assert "# 产品手册" in result.markdown
    assert "[**官方说明**](https://example.com)" in result.markdown
    assert "- [x] 完成同步" in result.markdown
    assert "| 字段 | 说明 |" in result.markdown
    assert result.assets == [
        {
            "type": "image",
            "token": "img-token",
            "block_id": "img",
            "filename": "feishu-image-img.bin",
        }
    ]


def test_text_styles_mentions_lists_quotes_code_and_file_descriptors_are_preserved() -> None:
    blocks = [
        {"block_id": "root", "block_type": 1, "children": ["bullet", "ordered", "code", "quote", "file", "line"]},
        {"block_id": "bullet", "block_type": 12, "bullet": {"elements": [{"text_run": {"content": "item"}}]}},
        {"block_id": "ordered", "block_type": 13, "ordered": {"elements": [{"text_run": {"content": "step"}}]}},
        {
            "block_id": "code",
            "block_type": 14,
            "code": {"style": {"language": "python"}, "elements": [{"text_run": {"content": "print('x')"}}]},
        },
        {"block_id": "quote", "block_type": 15, "quote": {"elements": [{"text_run": {"content": "quoted"}}]}},
        {"block_id": "file", "block_type": 23, "file": {"token": "file-token", "name": "design draft.pdf"}},
        {"block_id": "line", "block_type": 22},
        {
            "block_id": "styled",
            "block_type": 2,
            "text": {
                "elements": [
                    {"text_run": {"content": "bold", "text_element_style": {"bold": True}}},
                    {"mention_doc": {"title": "Doc", "url": "https://example.com/doc"}},
                    {"mention_user": {"name": "Alice"}},
                    {"equation": {"content": "x^2"}},
                    {"reminder": {"text": "later"}},
                ]
            },
        },
    ]

    result = convert_feishu_blocks_to_markdown(blocks)

    assert "- item" in result.markdown
    assert "1. step" in result.markdown
    assert "```python\nprint('x')\n```" in result.markdown
    assert "> quoted" in result.markdown
    assert "[design draft.pdf](./assets/design%20draft.pdf)" in result.markdown
    assert "---" in result.markdown
    assert result.assets == [
        {"type": "file", "token": "file-token", "block_id": "file", "filename": "design draft.pdf"}
    ]


def test_nested_children_and_cycles_are_bounded_with_warning() -> None:
    blocks = [
        {"block_id": "root", "block_type": 1, "children": ["unknown"]},
        {"block_id": "unknown", "block_type": 999, "children": ["text", "root"]},
        {"block_id": "text", "block_type": 2, "text": {"elements": [{"text_run": {"content": "kept"}}]}},
    ]

    result = convert_feishu_blocks_to_markdown(blocks)

    assert "kept" in result.markdown
    assert any("循环块引用：root" in warning for warning in result.warnings)
    assert any("暂未原生支持飞书块类型 999" in warning for warning in result.warnings)


def test_empty_or_malformed_block_payloads_are_safe_and_deterministic() -> None:
    blocks = [
        None,
        {"block_id": "text", "block_type": 2, "text": {"elements": ["bad", {"unknown": True}]}},
        {"block_id": "bad-table", "block_type": 31, "table": {"property": {"row_size": 0, "column_size": 2}}},
        {"block_id": "missing-type"},
    ]

    result = convert_feishu_blocks_to_markdown(blocks)  # type: ignore[arg-type]

    assert result.markdown == ""
    assert result.assets == []
    assert any("999" not in warning for warning in result.warnings)

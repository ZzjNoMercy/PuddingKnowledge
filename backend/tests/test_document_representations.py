from knowledge_platform.catalog.document_representations import legacy_document_representation


ORIGINAL = "a" * 64
MARKDOWN = "b" * 64


def converted_pdf(**overrides):
    row = {
        "source_type": "pdf_mineru",
        "mime_type": "text/markdown",
        "doc_metadata": {
            "mode": "multimodal_pdf",
            "original_sha256": ORIGINAL,
            "markdown_sha256": MARKDOWN,
            "original_path": "/raw/source.pdf",
        },
        "source_path": "/raw/source.pdf",
        "storage_path": "/derived/source.md",
        "content_sha256": ORIGINAL,
        "model_validation": {"valid": True},
    }
    row.update(overrides)
    return row


def test_complete_contract_returns_unprefixed_representation():
    assert legacy_document_representation(converted_pdf()) == {
        "body_sha256": MARKDOWN,
        "original_sha256": ORIGINAL,
        "original_path": "/raw/source.pdf",
    }


def test_sha256_prefix_is_accepted_and_output_is_bare_lowercase():
    upper_original = ORIGINAL.upper()
    upper_markdown = MARKDOWN.upper()
    row = converted_pdf(
        content_sha256=f"sha256:{upper_original}",
        doc_metadata={
            "mode": "multimodal_pdf",
            "original_sha256": f"sha256:{upper_original}",
            "markdown_sha256": f"sha256:{upper_markdown}",
            "original_path": "/raw/source.pdf",
        },
    )
    assert legacy_document_representation(row)["body_sha256"] == MARKDOWN


def test_ordinary_document_has_no_representation():
    row = converted_pdf(source_type="local_markdown", doc_metadata={"mode": "plain"})
    assert legacy_document_representation(row) is None


def test_pdf_marker_requires_every_contract_field():
    required = [
        "source_type",
        "mime_type",
        "doc_metadata",
        "source_path",
        "storage_path",
        "content_sha256",
    ]
    for field in required:
        row = converted_pdf()
        if field == "doc_metadata":
            row[field] = None
        else:
            row[field] = None
        import pytest
        with pytest.raises(ValueError):
            legacy_document_representation(row)
    row = converted_pdf(doc_metadata={
        "mode": "multimodal_pdf",
        "original_sha256": ORIGINAL,
        "markdown_sha256": MARKDOWN,
        "original_path": None,
    })
    import pytest
    with pytest.raises(ValueError):
        legacy_document_representation(row)


def test_multimodal_mode_alone_is_also_a_pdf_marker_but_requires_pdf_source_type():
    row = converted_pdf(source_type="legacy_document")
    import pytest
    with pytest.raises(ValueError):
        legacy_document_representation(row)


def test_source_and_storage_paths_must_bind_distinct_objects():
    import pytest
    with pytest.raises(ValueError):
        legacy_document_representation(converted_pdf(source_path="/other/source.pdf"))
    with pytest.raises(ValueError):
        legacy_document_representation(converted_pdf(storage_path="/raw/source.pdf"))


def test_digest_binding_and_metadata_digests_are_fail_closed():
    import pytest
    with pytest.raises(ValueError):
        legacy_document_representation(converted_pdf(content_sha256="c" * 64))
    with pytest.raises(ValueError):
        legacy_document_representation(
            converted_pdf(
                doc_metadata={
                    "mode": "multimodal_pdf",
                    "original_sha256": ORIGINAL,
                    "markdown_sha256": "not-a-digest",
                    "original_path": "/raw/source.pdf",
                }
            )
        )
    with pytest.raises(ValueError):
        legacy_document_representation(
            converted_pdf(
                doc_metadata={
                    "mode": "multimodal_pdf",
                    "original_sha256": "c" * 64,
                    "markdown_sha256": MARKDOWN,
                    "original_path": "/raw/source.pdf",
                }
            )
        )


def test_model_fields_cannot_upgrade_an_invalid_contract():
    row = converted_pdf(
        source_path="/attacker/claimed-source.pdf",
        content_sha256="not-valid",
        model_validation={"valid": True, "original_sha256": ORIGINAL},
        verified=True,
    )
    import pytest
    with pytest.raises(ValueError):
        legacy_document_representation(row)

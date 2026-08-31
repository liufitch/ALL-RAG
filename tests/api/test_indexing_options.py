def test_indexing_options_expose_enabled_models_without_secrets(client):
    response = client.get("/api/indexing/options")

    assert response.status_code == 200
    payload = response.json()
    assert payload["embedding_models"][0]["provider"] == "openai_compatible"
    assert payload["defaults"]["general"]["max_chunk_length"] == 1024
    assert payload["supported_files"] == [
        ".txt",
        ".md",
        ".pdf",
        ".docx",
        ".xls",
        ".xlsx",
        ".csv",
    ]
    assert ".doc" not in payload["supported_files"]
    assert not {".png", ".jpg", ".jpeg", ".tif", ".tiff"} & set(payload["supported_files"])
    serialized = response.text.lower()
    assert "api_key" not in serialized
    assert "base_url" not in serialized
    assert "milvus" not in serialized

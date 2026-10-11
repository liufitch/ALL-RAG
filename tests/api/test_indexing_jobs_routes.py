from main import app

def test_indexing_job_routes_are_registered():
    paths = app.openapi()["paths"]
    assert "/api/knowledge_base/{dataset_id}/indexing/jobs" in paths
    assert "get" in paths["/api/knowledge_base/{dataset_id}/indexing/jobs"]
    assert "post" in paths["/api/knowledge_base/{dataset_id}/indexing/jobs"]
    assert "post" in paths["/api/knowledge_base/{dataset_id}/indexing/jobs/{job_id}/retry"]
    assert "post" in paths["/api/knowledge_base/{dataset_id}/indexing/jobs/{job_id}/cancel"]

def test_unified_ingest_route_is_registered():
    assert "/api/knowledge_base/{dataset_id}/ingest" in app.openapi()["paths"]

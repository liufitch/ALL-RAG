from __future__ import annotations

from pathlib import Path
import subprocess
import sys
import textwrap


PROJECT_ROOT = Path(__file__).resolve().parents[3]


def test_economy_indexing_imports_do_not_load_engine_vector_or_pymilvus_modules():
    script = textwrap.dedent(
        """
        import importlib.abc
        import sys

        class BlockPyMilvus(importlib.abc.MetaPathFinder):
            def find_spec(self, fullname, path=None, target=None):
                if fullname == "pymilvus" or fullname.startswith("pymilvus."):
                    raise RuntimeError("PyMilvus import is forbidden")
                return None

        sys.meta_path.insert(0, BlockPyMilvus())

        import rag_modules.indexing
        from rag_modules.indexing import KeywordExtractor
        from rag_modules.indexing.ids import stable_segment_id
        from rag_modules.indexing.keywords import KeywordExtractor as DirectKeywordExtractor

        assert KeywordExtractor is DirectKeywordExtractor
        assert KeywordExtractor().extract("graph graph") == ["graph"]
        assert len(stable_segment_id("index", "document", None, 0, "hash")) == 32
        assert "rag_modules.indexing.engine" not in sys.modules
        assert not any(name.startswith("rag_modules.vector_stores") for name in sys.modules)
        assert not any(name.startswith("pymilvus") for name in sys.modules)
        """
    )

    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=PROJECT_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == ""
    assert completed.stderr == ""

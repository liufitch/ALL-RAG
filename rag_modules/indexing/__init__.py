"""按需导出依赖较少的索引基础组件。"""

from importlib import import_module


_EXPORTS = {
    "DocumentIndexingEngine": (".engine", "DocumentIndexingEngine"),
    "DocumentIndexingError": (".engine", "DocumentIndexingError"),
    "IndexDocumentCommand": (".models", "IndexDocumentCommand"),
    "IndexDocumentResult": (".models", "IndexDocumentResult"),
    "KeywordExtractor": (".keywords", "KeywordExtractor"),
    "ProgressReporter": (".models", "ProgressReporter"),
    "SegmentStagingCommand": (".models", "SegmentStagingCommand"),
    "VectorTarget": (".models", "VectorTarget"),
    "VectorTargetResolver": (".models", "VectorTargetResolver"),
}

__all__ = [
    "DocumentIndexingEngine",
    "DocumentIndexingError",
    "IndexDocumentCommand",
    "IndexDocumentResult",
    "KeywordExtractor",
    "ProgressReporter",
    "SegmentStagingCommand",
    "VectorTarget",
    "VectorTargetResolver",
]


def __getattr__(name: str):
    try:
        module_name, attribute_name = _EXPORTS[name]
    except KeyError as error:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from error
    value = getattr(import_module(module_name, __name__), attribute_name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))

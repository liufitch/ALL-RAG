from __future__ import annotations

from dataclasses import dataclass
import importlib
import inspect
import math
from collections.abc import Iterable, Mapping, Sequence
from threading import Lock
from typing import Any, Protocol

from rag_modules.parsing.models import DocumentParseError


@dataclass(frozen=True)
class OcrLine:
    """稳定的单行 OCR 结果，不暴露第三方 SDK 类型。"""

    text: str
    confidence: float
    box: tuple[tuple[float, float], ...] | None = None


class OcrEngine(Protocol):
    def recognize(self, image: object) -> tuple[OcrLine, ...]:
        """识别一张图片并返回经过适配的文本行。"""


class PaddleOcrEngine:
    """按需加载 PaddleOCR，并把 SDK 结果转换成内部 OCR 行。"""

    def __init__(
        self,
        *,
        device: str = "cpu",
        language: str = "ch",
        model_dir: str | None = None,
    ) -> None:
        if device not in {"cpu", "gpu"}:
            raise ValueError("OCR device must be cpu or gpu.")
        self.device = device
        self.language = language
        self.model_dir = model_dir
        self._backend: Any = None
        self._backend_lock = Lock()
        self._inference_lock = Lock()

    def recognize(self, image: object) -> tuple[OcrLine, ...]:
        backend = self._get_backend()
        with self._inference_lock:
            try:
                image = _paddle_input(image)
                if callable(getattr(backend, "predict", None)):
                    result = backend.predict(image)
                elif callable(getattr(backend, "ocr", None)):
                    result = backend.ocr(image, cls=False)
                else:
                    raise RuntimeError("OCR backend has no supported recognition method.")
                return tuple(_adapt_result(result))
            except DocumentParseError:
                raise
            except Exception as error:
                raise DocumentParseError(
                    "OCR_FAILED", "Optical character recognition failed.", retryable=True
                ) from error

    def _get_backend(self) -> Any:
        if self._backend is not None:
            return self._backend
        with self._backend_lock:
            if self._backend is not None:
                return self._backend
            self._backend = self._initialize_backend()
            return self._backend

    def _initialize_backend(self) -> Any:
        try:
            module = importlib.import_module("paddleocr")
        except (ImportError, ModuleNotFoundError) as error:
            raise DocumentParseError(
                "OCR_DEPENDENCY_MISSING", "OCR dependencies are not installed."
            ) from error

        try:
            factory = module.PaddleOCR
            parameters = inspect.signature(factory).parameters
            accepts_kwargs = any(
                parameter.kind is inspect.Parameter.VAR_KEYWORD
                for parameter in parameters.values()
            )
            kwargs: dict[str, Any] = {"lang": self.language}
            if "ocr_version" in parameters or accepts_kwargs:
                kwargs["ocr_version"] = "PP-OCRv5"
            for option in (
                "use_doc_orientation_classify",
                "use_doc_unwarping",
                "use_textline_orientation",
            ):
                if option in parameters or accepts_kwargs:
                    kwargs[option] = False
            if self.model_dir:
                model_key = next(
                    (
                        key
                        for key in (
                            "ocr_model_dir",
                            "text_detection_model_dir",
                            "det_model_dir",
                        )
                        if key in parameters
                    ),
                    "text_detection_model_dir" if accepts_kwargs else None,
                )
                if model_key is not None:
                    kwargs[model_key] = self.model_dir
            if "device" in parameters or accepts_kwargs:
                kwargs["device"] = "gpu:0" if self.device == "gpu" else "cpu"
            elif "use_gpu" in parameters:
                kwargs["use_gpu"] = self.device == "gpu"
            return factory(**kwargs)
        except DocumentParseError:
            raise
        except Exception as error:
            raise DocumentParseError(
                "OCR_INITIALIZATION_FAILED", "The OCR engine could not be initialized."
            ) from error


def _paddle_input(image: object) -> object:
    """把 Pillow 图像转换为 PaddleOCR 使用的连续 BGR 数组。"""
    if image.__class__.__module__.split(".", 1)[0] != "PIL":
        return image
    try:
        import numpy as np
    except ImportError as error:
        raise DocumentParseError(
            "OCR_DEPENDENCY_MISSING", "OCR image dependencies are not installed."
        ) from error
    return np.ascontiguousarray(np.asarray(image.convert("RGB"))[:, :, ::-1])


def _adapt_result(result: object) -> list[OcrLine]:
    lines: list[OcrLine] = []
    _collect_lines(result, lines)
    return lines


def _collect_lines(value: object, output: list[OcrLine]) -> None:
    mapping = _as_mapping(value)
    if mapping is not None and isinstance(mapping.get("res"), Mapping):
        _collect_lines(mapping["res"], output)
        return
    texts = _as_sequence(mapping.get("rec_texts")) if mapping is not None else None
    if texts is not None:
        scores = _as_sequence(mapping.get("rec_scores")) or ()
        boxes = _as_sequence(mapping.get("rec_polys", mapping.get("dt_polys"))) or ()
        for index, text in enumerate(texts):
            confidence = scores[index] if index < len(scores) else 1.0
            box = boxes[index] if index < len(boxes) else None
            line = _make_line(text, confidence, box)
            if line is not None:
                output.append(line)
        return

    candidate = _line_candidate(value)
    if candidate is not None:
        output.append(candidate)
        return

    sequence = _as_sequence(value)
    if sequence is not None:
        for item in sequence:
            _collect_lines(item, output)
        return
    if isinstance(value, Iterable) and not isinstance(
        value, (str, bytes, bytearray, Mapping)
    ):
        for item in value:
            _collect_lines(item, output)


def _as_sequence(value: object) -> Sequence | None:
    if isinstance(value, Sequence) and not isinstance(
        value, (str, bytes, bytearray)
    ):
        return value
    to_list = getattr(value, "tolist", None)
    if callable(to_list):
        try:
            converted = to_list()
        except Exception:
            return None
        if isinstance(converted, Sequence) and not isinstance(
            converted, (str, bytes, bytearray)
        ):
            return converted
    return None


def _as_mapping(value: object) -> Mapping[str, Any] | None:
    if isinstance(value, Mapping):
        return value
    serialized = getattr(value, "json", None)
    if isinstance(serialized, str):
        try:
            import json

            loaded = json.loads(serialized)
        except (TypeError, ValueError):
            return None
        return loaded if isinstance(loaded, Mapping) else None
    if isinstance(serialized, Mapping):
        return serialized
    return None


def _line_candidate(value: object) -> OcrLine | None:
    value = _as_sequence(value)
    if value is None:
        return None
    if len(value) == 2 and isinstance(value[0], str):
        return _make_line(value[0], value[1], None)
    recognition = _as_sequence(value[1]) if len(value) == 2 else None
    if recognition is None:
        return None
    if len(recognition) < 2:
        return None
    return _make_line(recognition[0], recognition[1], value[0])


def _make_line(text: object, confidence: object, box: object) -> OcrLine | None:
    if not isinstance(text, str):
        return None
    try:
        score = float(confidence)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(score):
        return None
    return OcrLine(text, max(0.0, min(1.0, score)), _normalize_box(box))


def _normalize_box(value: object) -> tuple[tuple[float, float], ...] | None:
    value = _as_sequence(value)
    if value is None:
        return None
    points: list[tuple[float, float]] = []
    for point in value:
        point = _as_sequence(point)
        if point is None or len(point) < 2:
            return None
        try:
            x, y = float(point[0]), float(point[1])
        except (TypeError, ValueError):
            return None
        if not math.isfinite(x) or not math.isfinite(y):
            return None
        points.append((x, y))
    return tuple(points) if points else None

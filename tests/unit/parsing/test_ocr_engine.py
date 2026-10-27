import sys
import types
from concurrent.futures import ThreadPoolExecutor
from threading import current_thread

import pytest

from rag_modules.parsing.models import DocumentParseError
from rag_modules.parsing.ocr import OcrLine, PaddleOcrEngine


def test_cached_paddle_backend_stays_on_its_initialization_thread(monkeypatch):
    """连续 Celery 任务创建新线程池时，也须在同一线程初始化和使用预测器。"""
    class ThreadBoundBackend:
        def __init__(self, **kwargs):
            self.owner = current_thread()

        def predict(self, image):
            if current_thread() is not self.owner:
                raise RuntimeError("predictor used from a different thread")
            return [{"rec_texts": ["正文"], "rec_scores": [0.95]}]

    monkeypatch.setitem(sys.modules, "paddleocr", types.SimpleNamespace(PaddleOCR=ThreadBoundBackend))
    engine = PaddleOcrEngine()
    for _ in range(3):
        with ThreadPoolExecutor(max_workers=1) as caller:
            assert caller.submit(engine.recognize, object()).result() == (OcrLine("正文", 0.95),)


def test_paddle_engine_reports_missing_optional_dependency(monkeypatch):
    monkeypatch.setitem(sys.modules, "paddleocr", None)
    engine = PaddleOcrEngine(device="cpu")

    with pytest.raises(DocumentParseError) as error:
        engine.recognize(object())

    assert error.value.code == "OCR_DEPENDENCY_MISSING"


def test_paddle_engine_adapts_common_ocr_result_shape(monkeypatch):
    class FakePaddleBackend:
        def ocr(self, image, cls=False):
            assert image is payload
            return [[([(1, 2), (3, 2), (3, 4), (1, 4)], ("标题", 0.97))]]

    payload = object()
    fake_module = types.SimpleNamespace(
        PaddleOCR=lambda **kwargs: FakePaddleBackend()
    )
    monkeypatch.setitem(sys.modules, "paddleocr", fake_module)

    lines = PaddleOcrEngine(device="cpu").recognize(payload)

    assert lines == (
        OcrLine(
            "标题",
            0.97,
            ((1.0, 2.0), (3.0, 2.0), (3.0, 4.0), (1.0, 4.0)),
        ),
    )


def test_paddle_engine_adapts_iterator_results(monkeypatch):
    class FakePaddleBackend:
        def predict(self, image):
            return iter(
                [
                    types.SimpleNamespace(
                        json={"res": {"rec_texts": ["正文"], "rec_scores": [0.93]}}
                    )
                ]
            )

    fake_module = types.SimpleNamespace(
        PaddleOCR=lambda **kwargs: FakePaddleBackend()
    )
    monkeypatch.setitem(sys.modules, "paddleocr", fake_module)

    assert PaddleOcrEngine(device="cpu").recognize(object()) == (
        OcrLine("正文", 0.93, None),
    )


def test_paddle_engine_configures_v3_detection_model_directory(monkeypatch):
    captured = {}

    class FakePaddleBackend:
        def predict(self, image):
            return []

    def create_backend(**kwargs):
        captured.update(kwargs)
        return FakePaddleBackend()

    monkeypatch.setitem(
        sys.modules,
        "paddleocr",
        types.SimpleNamespace(PaddleOCR=create_backend),
    )

    PaddleOcrEngine(device="cpu", model_dir="/models/detection").recognize(object())

    assert captured["text_detection_model_dir"] == "/models/detection"
    assert captured["ocr_version"] == "PP-OCRv5"
    assert captured["use_doc_orientation_classify"] is False
    assert captured["use_doc_unwarping"] is False
    assert captured["use_textline_orientation"] is False


def test_paddle_engine_keeps_numpy_recognition_polygons(monkeypatch):
    np = pytest.importorskip("numpy")
    box = np.array([[10, 20], [30, 20], [30, 40], [10, 40]])
    backend = types.SimpleNamespace(predict=lambda image: [{
        "rec_texts": ["正文"], "rec_scores": np.array([0.95]),
        "rec_polys": [box],
    }])
    monkeypatch.setitem(sys.modules, "paddleocr", types.SimpleNamespace(PaddleOCR=lambda **kw: backend))

    assert PaddleOcrEngine().recognize(object())[0].box == (
        (10.0, 20.0), (30.0, 20.0), (30.0, 40.0), (10.0, 40.0)
    )


def test_paddle_engine_uses_recognition_boxes_after_sdk_filtering(monkeypatch):
    dropped_box = [[0, 0], [10, 0], [10, 10], [0, 10]]
    kept_box = [[0, 30], [10, 30], [10, 40], [0, 40]]
    backend = types.SimpleNamespace(predict=lambda image: [{
        "rec_texts": ["正文"], "rec_scores": [0.95],
        "dt_polys": [dropped_box, kept_box], "rec_polys": [kept_box],
    }])
    monkeypatch.setitem(sys.modules, "paddleocr", types.SimpleNamespace(PaddleOCR=lambda **kw: backend))

    assert PaddleOcrEngine().recognize(object())[0].box == (
        (0.0, 30.0), (10.0, 30.0), (10.0, 40.0), (0.0, 40.0)
    )


def test_paddle_engine_converts_pillow_rgb_pixels_to_bgr(monkeypatch):
    np = pytest.importorskip("numpy")

    class ImageInput:
        __module__ = "PIL.Image"

        def convert(self, mode):
            assert mode == "RGB"
            return np.array([[[255, 20, 10]]], dtype=np.uint8)

    def predict(image):
        assert image.tolist() == [[[10, 20, 255]]]
        assert image.flags.c_contiguous
        return [{"rec_texts": ["正文"], "rec_scores": [0.95]}]

    backend = types.SimpleNamespace(predict=predict)
    monkeypatch.setitem(sys.modules, "paddleocr", types.SimpleNamespace(PaddleOCR=lambda **kw: backend))
    assert PaddleOcrEngine().recognize(ImageInput())[0].text == "正文"

"""app/model/ の純 Python 単体テスト（dataclass roundtrip・Document・serialize）。"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from app.model.document import Artboard, Document, Physical
from app.model.objects import (
    OBJECT_REGISTRY,
    BaseObject,
    ConnectorObject,
    EllipseObject,
    FreehandObject,
    ImageObject,
    LineObject,
    MathObject,
    RectObject,
    TextObject,
    new_object,
)
from app.model.serialize import (
    document_from_json,
    document_to_json,
    load_document,
    save_document,
)

# --------------------------------------------------------------------------
# dataclass roundtrip（全種別）
# --------------------------------------------------------------------------


def test_rect_roundtrip() -> None:
    obj = RectObject(id=1, x=1, y=2, width=10, height=20, fill="#FF0000", corner_radius=3.0)
    d = obj.to_dict()
    assert d["type"] == "rect"
    restored = BaseObject.from_dict(d)
    assert isinstance(restored, RectObject)
    assert restored.fill == "#FF0000"
    assert restored.corner_radius == 3.0
    assert restored.x == 1 and restored.y == 2


def test_ellipse_roundtrip() -> None:
    obj = EllipseObject(id=2, x=5, y=5, width=30, height=15, stroke="#123456")
    d = obj.to_dict()
    assert d["type"] == "ellipse"
    restored = BaseObject.from_dict(d)
    assert isinstance(restored, EllipseObject)
    assert restored.stroke == "#123456"


def test_line_roundtrip() -> None:
    obj = LineObject(id=3, p1=[0.0, 0.0], p2=[10.0, 20.0], arrow_end="triangle")
    d = obj.to_dict()
    assert d["type"] == "line"
    restored = BaseObject.from_dict(d)
    assert isinstance(restored, LineObject)
    assert restored.p1 == [0.0, 0.0]
    assert restored.p2 == [10.0, 20.0]
    assert restored.arrow_end == "triangle"


def test_arrow_type_preserved_as_lineobject() -> None:
    """type="arrow" は LineObject に登録されるが type 文字列は保持される。"""
    obj = new_object("arrow", id=4, p1=[1.0, 1.0], p2=[2.0, 2.0])
    assert isinstance(obj, LineObject)
    assert obj.type == "arrow"
    d = obj.to_dict()
    assert d["type"] == "arrow"
    restored = BaseObject.from_dict(d)
    assert isinstance(restored, LineObject)
    assert restored.type == "arrow"


def test_image_roundtrip() -> None:
    obj = ImageObject(id=5, src="assets/img_001.png", crop=[0, 0, 100, 100], has_alpha=True)
    d = obj.to_dict()
    assert d["type"] == "image"
    restored = BaseObject.from_dict(d)
    assert isinstance(restored, ImageObject)
    assert restored.src == "assets/img_001.png"
    assert restored.has_alpha is True


def test_freehand_roundtrip() -> None:
    obj = FreehandObject(id=6, points=[[0, 0], [1, 1], [2, 3]], smoothing=0.5)
    d = obj.to_dict()
    assert d["type"] == "freehand"
    restored = BaseObject.from_dict(d)
    assert isinstance(restored, FreehandObject)
    assert restored.points == [[0, 0], [1, 1], [2, 3]]


def test_text_roundtrip() -> None:
    obj = TextObject(id=7, text="hello", font_size=24.0, bold=True, align="center")
    d = obj.to_dict()
    assert d["type"] == "text"
    restored = BaseObject.from_dict(d)
    assert isinstance(restored, TextObject)
    assert restored.text == "hello"
    assert restored.bold is True
    assert restored.align == "center"


def test_math_roundtrip_excludes_underscore_field() -> None:
    obj = MathObject(id=8, latex=r"\alpha^2", font_size=20.0)
    obj._svg_cache = "<svg>cached</svg>"
    d = obj.to_dict()
    assert "_svg_cache" not in d
    assert d["latex"] == r"\alpha^2"
    restored = BaseObject.from_dict(d)
    assert isinstance(restored, MathObject)
    assert restored.latex == r"\alpha^2"
    # _svg_cache は除外されるため既定値に戻る
    assert restored._svg_cache == ""


def test_connector_roundtrip() -> None:
    obj = ConnectorObject(id=9, source_id=1, target_id=2, routing="orthogonal")
    d = obj.to_dict()
    assert d["type"] == "connector"
    restored = BaseObject.from_dict(d)
    assert isinstance(restored, ConnectorObject)
    assert restored.source_id == 1
    assert restored.target_id == 2
    assert restored.routing == "orthogonal"


def test_from_dict_unknown_type_raises() -> None:
    with pytest.raises(ValueError):
        BaseObject.from_dict({"type": "unknown_type_xyz"})


def test_from_dict_ignores_unknown_keys_and_uses_defaults() -> None:
    d = {"type": "rect", "id": 1, "bogus_field": 123}
    restored = BaseObject.from_dict(d)
    assert isinstance(restored, RectObject)
    assert restored.id == 1
    assert restored.fill is None  # 既定値


def test_new_object_helper() -> None:
    obj = new_object("ellipse", id=10, width=5, height=5)
    assert isinstance(obj, EllipseObject)
    assert obj.id == 10
    assert obj.width == 5


def test_object_registry_has_all_types() -> None:
    for t in ("rect", "ellipse", "line", "arrow", "image", "freehand", "text", "math", "connector"):
        assert t in OBJECT_REGISTRY


# --------------------------------------------------------------------------
# Document
# --------------------------------------------------------------------------


def test_document_add_remove_and_z_normalize() -> None:
    doc = Document()
    r1 = RectObject(id=doc.new_id(), x=0, y=0, width=10, height=10)
    r2 = RectObject(id=doc.new_id(), x=10, y=10, width=10, height=10)
    r3 = RectObject(id=doc.new_id(), x=20, y=20, width=10, height=10)

    doc.add_object(r1)
    doc.add_object(r2)
    doc.add_object(r3)

    assert doc.objects == [r1, r2, r3]
    assert [o.z for o in doc.objects] == [0, 1, 2]

    assert doc.object_by_id(r2.id) is r2
    assert doc.index_of(r2) == 1

    doc.remove_object(r2)
    assert doc.objects == [r1, r3]
    assert [o.z for o in doc.objects] == [0, 1]
    assert doc.object_by_id(r2.id) is None


def test_document_move_to_index() -> None:
    doc = Document()
    r1 = RectObject(id=doc.new_id())
    r2 = RectObject(id=doc.new_id())
    r3 = RectObject(id=doc.new_id())
    doc.add_object(r1)
    doc.add_object(r2)
    doc.add_object(r3)

    doc.move_to_index(r3, 0)
    assert doc.objects == [r3, r1, r2]
    assert [o.z for o in doc.objects] == [0, 1, 2]


def test_document_new_id_increments() -> None:
    doc = Document()
    assert doc.new_id() == 1
    assert doc.new_id() == 2
    assert doc.next_id == 3


def test_document_to_dict_from_dict_roundtrip() -> None:
    physical = Physical(width_mm=100, target_dpi=150)
    artboard = Artboard(width_px=800, height_px=600, physical=physical)
    doc = Document(artboard=artboard)
    doc.add_object(RectObject(id=doc.new_id(), x=1, y=2, width=3, height=4))
    doc.add_object(LineObject(id=doc.new_id(), p1=[0, 0], p2=[5, 5]))

    d = document_to_json(doc)
    restored = document_from_json(d)

    assert restored.artboard.width_px == 800
    assert restored.artboard.height_px == 600
    assert restored.artboard.physical.width_mm == 100
    assert restored.artboard.physical.target_dpi == 150
    assert len(restored.objects) == 2
    assert restored.next_id == doc.next_id


# --------------------------------------------------------------------------
# serialize
# --------------------------------------------------------------------------


def test_save_and_load_document(tmp_path: Path) -> None:
    doc = Document()
    doc.add_object(RectObject(id=doc.new_id(), x=1, y=2, width=10, height=20, fill="#ABCDEF"))
    doc.add_object(EllipseObject(id=doc.new_id(), x=3, y=4, width=15, height=25))
    doc.add_object(LineObject(id=doc.new_id(), p1=[1, 1], p2=[9, 9], arrow_end="open"))

    project_dir = tmp_path / "myproject"
    save_document(doc, project_dir)

    assert (project_dir / "project.json").exists()
    assert (project_dir / "assets").is_dir()
    assert (project_dir / "exports").is_dir()

    loaded = load_document(project_dir)

    assert len(loaded.objects) == len(doc.objects)
    for orig, loaded_obj in zip(doc.objects, loaded.objects, strict=True):
        assert loaded_obj.type == orig.type
        assert loaded_obj.id == orig.id
        assert loaded_obj.x == orig.x
        assert loaded_obj.y == orig.y

    assert loaded.next_id == doc.next_id
    assert loaded.artboard.width_px == doc.artboard.width_px


def test_save_document_with_tempdir() -> None:
    """tempfile.TemporaryDirectory との併用でも問題なく保存/読込できることを確認。"""
    doc = Document()
    doc.add_object(RectObject(id=doc.new_id()))
    with tempfile.TemporaryDirectory() as tmp:
        project_dir = Path(tmp) / "proj2"
        save_document(doc, project_dir)
        loaded = load_document(project_dir)
        assert len(loaded.objects) == 1

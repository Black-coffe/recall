"""Тести хелперів імпорту папки документів (Phase 16E).

Лёгкі — app.blueprints.documents імпортується без torch (flask/utils лише).
_collect_documents/_copy_into_docs не потребують app context.
"""
import os

from app.blueprints import documents as docs


def test_collect_documents_non_recursive(tmp_path):
    (tmp_path / "a.md").write_text("x", encoding="utf-8")
    (tmp_path / "b.pdf").write_bytes(b"%PDF-1.4")
    (tmp_path / "c.mp3").write_bytes(b"\x00")        # не документ
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "d.txt").write_text("y", encoding="utf-8")  # підпапка — не береться

    found = docs._collect_documents(str(tmp_path), recursive=False)
    names = sorted(os.path.basename(f) for f in found)
    assert names == ["a.md", "b.pdf"]


def test_collect_documents_recursive(tmp_path):
    (tmp_path / "a.md").write_text("x", encoding="utf-8")
    (tmp_path / "c.mp3").write_bytes(b"\x00")
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "d.txt").write_text("y", encoding="utf-8")

    found = docs._collect_documents(str(tmp_path), recursive=True)
    names = sorted(os.path.basename(f) for f in found)
    assert names == ["a.md", "d.txt"]


def test_copy_into_docs_preserves_content(tmp_path):
    src = tmp_path / "orig.txt"
    src.write_text("hello world", encoding="utf-8")
    docs_dir = tmp_path / "store"

    dest = docs._copy_into_docs(str(src), str(docs_dir))
    assert os.path.exists(dest)
    assert dest.startswith(os.path.abspath(str(docs_dir)))
    with open(dest, encoding="utf-8") as f:
        assert f.read() == "hello world"

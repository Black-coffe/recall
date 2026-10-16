"""Catalog: server texts of retrieval that reach the UI (comment target labels in citations)."""


def _m(uk: str, en: str) -> dict:
    return {"uk": uk, "en": en}


MESSAGES: dict = {
    "svc_retrieval.comment.target_record": _m("Запис #{id}", "Record #{id}"),
    "svc_retrieval.comment.target_audio": _m("Аудіо #{id}", "Audio #{id}"),
}

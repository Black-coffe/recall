"""Catalog: server texts of the taxonomy (direction tree, tags, filter parsing).

Typed errors (`CategoryError`) and the `ValueError` texts of the filter parsers reach the web UI
(SPA toast) and the API; the uk value is the former literal verbatim.
"""


def _m(uk: str, en: str) -> dict:
    return {"uk": uk, "en": en}


MESSAGES: dict = {
    # categories.py: filter parsing
    "svc_taxonomy.err.bad_category_id": _m(
        "category_id: очікую id, 'none' або 'all' (отримав {raw})",
        "category_id: expected an id, 'none' or 'all' (got {raw})"),
    "svc_taxonomy.err.bad_date": _m(
        "Невалідна дата «{s}»: очікується YYYY-MM-DD",
        "Invalid date «{s}»: expected YYYY-MM-DD"),
    # categories.py: direction tree
    "svc_taxonomy.err.direction_not_found": _m(
        "Напрямок {cid} не знайдено", "Direction {cid} not found"),
    "svc_taxonomy.err.ambiguous": _m(
        "«{text}» є в кількох напрямках — уточніть повний шлях: {paths}",
        "«{text}» exists in several directions - specify the full path: {paths}"),
    "svc_taxonomy.err.self_parent": _m(
        "Напрямок не може бути власним батьком", "A direction cannot be its own parent"),
    "svc_taxonomy.err.parent_is_project": _m(
        "«{name}» — уже проєкт; третього рівня немає",
        "«{name}» is already a project; there is no third level"),
    "svc_taxonomy.err.sep_in_name": _m(
        "Назва вузла не може містити «{sep}» — це роздільник повного шляху",
        "A node name cannot contain «{sep}» - it is the separator of the full path"),
    "svc_taxonomy.err.empty_name": _m("Порожня назва", "Empty name"),
    "svc_taxonomy.err.dup_direction": _m(
        "Напрямок «{name}» уже існує", "Direction «{name}» already exists"),
    "svc_taxonomy.err.dup_project": _m(
        "Проєкт «{name}» уже є в напрямку «{parent}»",
        "Project «{name}» already exists in direction «{parent}»"),
    "svc_taxonomy.err.has_projects_n": _m(
        "«{name}» має проєкти ({n}) — спершу перенесіть їх",
        "«{name}» has projects ({n}) - move them first"),
    "svc_taxonomy.err.nothing_to_update": _m("Нічого оновлювати", "Nothing to update"),
    "svc_taxonomy.err.move_projects_first": _m(
        "{action}: спершу перенесіть проєкти ({names})",
        "{action}: move the projects first ({names})"),
    "svc_taxonomy.action.delete_direction": _m("Видалення напрямку", "Deleting a direction"),
    "svc_taxonomy.action.merge_direction": _m("Обʼєднання напрямку", "Merging a direction"),
    # categories.py: tags
    "svc_taxonomy.err.empty_tag_name": _m("Порожня назва тега", "Empty tag name"),
    "svc_taxonomy.err.tag_too_long": _m(
        "Назва тега довша за {max} символів", "Tag name is longer than {max} characters"),
    "svc_taxonomy.err.tag_not_found": _m("Тег {tag_id} не знайдено", "Tag {tag_id} not found"),
    "svc_taxonomy.err.dup_tag": _m("Тег «{name}» уже існує", "Tag «{name}» already exists"),
    "svc_taxonomy.err.merge_tag_self": _m(
        "Не можна злити тег із самим собою", "A tag cannot be merged with itself"),
    "svc_taxonomy.err.unknown_tag_source": _m(
        "Невідоме джерело тега: {source}", "Unknown tag source: {source}"),
    "svc_taxonomy.err.not_suggested": _m(
        "Не запропоновано: {names}", "Not suggested: {names}"),
}

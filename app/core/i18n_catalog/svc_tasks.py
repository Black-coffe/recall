"""Server text catalog: area svc_tasks (task-action errors shown to the user)."""
MESSAGES: dict = {
    "svc_tasks.err.itemNotFound": {
        "uk": "задачу {id} не знайдено",
        "en": "task {id} not found",
    },
    "svc_tasks.err.unknownAction": {
        "uk": "невідома дія: {action}",
        "en": "unknown action: {action}",
    },
    "svc_tasks.err.eventNotFound": {
        "uk": "подію {id} не знайдено",
        "en": "event {id} not found",
    },
    "svc_tasks.err.undoLastOnly": {
        "uk": "скасувати можна лише останню дію над задачею",
        "en": "only the last action on a task can be undone",
    },
    "svc_tasks.err.invalidTransition": {
        "uk": "дію {action} не можна застосувати до задачі зі статусом {status}",
        "en": "action {action} is not allowed for a task with status {status}",
    },
}

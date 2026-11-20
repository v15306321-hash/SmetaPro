"""Централизованная модель прав доступа (роль → набор разрешений).

Подробные права ролей «администратор», «мастер», «менеджер» — открытый вопрос ТЗ (п. 26);
ниже — предложенная по умолчанию матрица, меняется в одном месте.
"""
from app.models import Role

# Полный перечень разрешений внутри предприятия
ALL_PERMISSIONS: dict[str, str] = {
    "orders.view": "Просмотр заказов",
    "orders.edit": "Создание и редактирование заказов",
    "orders.status": "Смена статусов заказов",
    "estimates.create": "Формирование PDF-смет",
    "materials.view": "Просмотр базы материалов",
    "materials.edit": "Редактирование базы материалов",
    "expenses.view": "Просмотр производственных затрат",
    "expenses.edit": "Учёт производственных затрат",
    "waste.edit": "Учёт расхода и отходов",
    "analytics.view": "Финансовая аналитика",
    "settings.edit": "Настройки предприятия и наценки",
    "staff.view": "Просмотр сотрудников",
    "staff.manage": "Управление сотрудниками",
    # billing.manage — только владелец, администратору выдать нельзя
    "billing.manage": "Подписка и оплаты",
}

OWNER_ONLY = {"billing.manage", "staff.manage"}

DEFAULT_ADMIN_PERMISSIONS = [p for p in ALL_PERMISSIONS if p not in OWNER_ONLY]

ROLE_PERMISSIONS: dict[Role, set[str]] = {
    Role.owner: set(ALL_PERMISSIONS),
    Role.master: {"orders.view", "orders.status", "materials.view", "waste.edit",
                  "expenses.view", "expenses.edit"},
    Role.manager: {"orders.view", "orders.edit", "orders.status", "estimates.create",
                   "materials.view"},
    # Сотрудник, которому владелец разрешил составлять сметы: расчёты, сметы, база материалов.
    # Подписка, сотрудники, настройки и финансы предприятия ему недоступны.
    Role.employee: {"orders.view", "orders.edit", "orders.status", "estimates.create",
                    "materials.view", "materials.edit", "waste.edit"},
}

ROLE_TITLES = {
    Role.owner: "Владелец",
    Role.admin: "Администратор",
    Role.master: "Мастер",
    Role.manager: "Менеджер",
    Role.employee: "Сотрудник",
}


def permissions_for(role: Role, admin_permissions: list | None = None) -> set[str]:
    if role == Role.admin:
        granted = admin_permissions if admin_permissions is not None else DEFAULT_ADMIN_PERMISSIONS
        return {p for p in granted if p in ALL_PERMISSIONS and p not in OWNER_ONLY}
    return set(ROLE_PERMISSIONS[role])

from sqlalchemy.orm import Session

from app.models import AuditLog, Notification


def log(db: Session, action: str, *, actor_id: int | None = None, enterprise_id: int | None = None,
        entity: str | None = None, entity_id=None, **data) -> None:
    """Журнал действий. Не записывать сюда секреты и содержимое файлов."""
    db.add(AuditLog(actor_id=actor_id, enterprise_id=enterprise_id, action=action, entity=entity,
                    entity_id=str(entity_id) if entity_id is not None else None,
                    data={k: (str(v) if not isinstance(v, (int, float, bool, str, type(None), list, dict)) else v)
                          for k, v in data.items()} or None))


def notify(db: Session, user_id: int, text: str, dedup_key: str | None = None) -> None:
    """Поставить уведомление в очередь. dedup_key исключает повторную отправку."""
    if dedup_key and db.query(Notification.id).filter_by(dedup_key=dedup_key).first():
        return
    db.add(Notification(user_id=user_id, text=text, dedup_key=dedup_key))

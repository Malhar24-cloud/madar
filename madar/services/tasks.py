from ..models import Task, db, utcnow


def ensure(key, role, title, detail="", employee_id=None, due=None, link=None, assignee_id=None):
    """ينشئ المهمة مرة واحدة فقط لكل مفتاح، فتشغيل الأتمتة مرتين لا يكرر المهام."""
    if key and Task.query.filter_by(key=key).first():
        return None
    t = Task(key=key, role=role, title=title[:200], detail=(detail or "")[:500], employee_id=employee_id,
             due=due, link=link, assignee_id=assignee_id)
    db.session.add(t)
    return t


def close(prefix):
    Task.query.filter(Task.key.like(prefix + "%"), Task.done.is_(False)) \
        .update({"done": True, "done_at": utcnow()}, synchronize_session=False)


TASK_ROLE_PERM = {"payroll": "payroll", "recruit": "hiring", "hiring": "hiring", "attendance": "attendance",
                  "leaves": "leaves", "letters": "letters", "changes": "changes"}


def visible_query(user):
    """المهام المسندة للشخص نفسه، أو غير المسندة لأحد لكنها من نوع صلاحياته."""
    q = Task.query
    if user.can("admin"):
        return q
    roles = [r for r, perm in TASK_ROLE_PERM.items() if user.can(perm)]
    if user.is_manager:
        roles.append("manager")
    return q.filter((Task.assignee_id == user.id) | ((Task.assignee_id.is_(None)) & (Task.role.in_(roles or ["-"]))))

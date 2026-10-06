from flask import Blueprint, current_app, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from ..models import ChatMessage, db
from ..security import limited
from ..services import ai, audit

bp = Blueprint("assistant", __name__)

SUGGEST = {
    "employee": ["كم باقي من إجازتي؟", "كم مكافأتي لو استقلت؟", "وش حالة طلباتي؟", "كم راتب بندر؟"],
    "manager": ["وش الطلبات اللي تنتظر موافقتي؟", "مين من فريقي عقده ينتهي قريب؟", "كم رصيد إجازات خالد؟"],
    "default": ["وش الطلبات اللي تنتظر قراري؟", "مين العقود اللي تنتهي خلال 90 يوم؟", "احسب نهاية خدمة بندر لو استقال",
                "كم رصيد إجازات سلطان؟"],
}


def _history(limit=16):
    rows = ChatMessage.query.filter_by(user_id=current_user.id).order_by(ChatMessage.id.desc()).limit(limit).all()
    return [{"role": r.role, "text": r.text} for r in reversed(rows)]


@bp.route("/assistant", methods=["GET", "POST"])
@login_required
def chat():
    if request.method == "POST":
        from .. import validate as v
        q = v.clean_text(request.form.get("q"), 800, multiline=True)
        if q:
            db.session.add(ChatMessage(user_id=current_user.id, role="user", text=q))
            db.session.flush()
            history = _history()
            try:
                if ai.enabled() and limited("ai", current_user.id, 60, 3600):
                    answer = "وصلت الحد المسموح من الأسئلة لهذي الساعة (60 سؤال). حاول بعد شوي."
                else:
                    answer = ai.assistant_reply(current_user, history)
            except Exception as ex:
                current_app.logger.warning("assistant failed: %s", ex)
                answer = "تعذّر الوصول للذكاء الاصطناعي الآن. حاول بعد قليل."
            db.session.add(ChatMessage(user_id=current_user.id, role="assistant", text=answer[:4000]))
            audit.log("سؤال للمساعد", q[:200])
            db.session.commit()
        return redirect(url_for("assistant.chat") + "#end")
    return render_template("assistant/chat.html", history=_history(30), ai_on=ai.enabled(),
                           suggest=SUGGEST["default"] if current_user.is_staff else SUGGEST["manager" if current_user.is_manager else "employee"])


@bp.route("/assistant/clear", methods=["POST"])
@login_required
def clear():
    ChatMessage.query.filter_by(user_id=current_user.id).delete()
    db.session.commit()
    return redirect(url_for("assistant.chat"))

from flask import Blueprint, abort, jsonify, render_template, request

from ..extensions import db
from ..models import utcnow
from ..security import role_required
from ..services.ledger import audit_source
from ..services.weapons import MODEL_TAG, TARGET_LABEL
from ..weapon_models import WeaponAlert, weapon_snapshot

bp = Blueprint("weapons", __name__)


@bp.get("/admin/alerts")
@role_required("admin")
def alerts():
    return render_template("weapon_alerts.html")


@bp.get("/api/admin/weapon-alerts")
@role_required("admin")
def recent_alerts():
    latest = db.select(WeaponAlert).order_by(WeaponAlert.observed_at.desc(), WeaponAlert.id.desc())
    pending = db.session.scalar(
        latest.where(
            WeaponAlert.model_tag == MODEL_TAG,
            WeaponAlert.label == TARGET_LABEL,
            WeaponAlert.acknowledged_at.is_(None),
        ).limit(1)
    )
    pending_snapshot = weapon_snapshot(pending) if pending else None
    if request.args.get("pending") == "1":
        return jsonify(pending=pending_snapshot)
    return jsonify(
        pending=pending_snapshot,
        alerts=[
            {**weapon_snapshot(alert), "acknowledged": alert.acknowledged_at is not None}
            for alert in db.session.scalars(latest.limit(100))
        ],
    )


@bp.post("/api/admin/weapon-alerts/<int:alert_id>/acknowledge")
@role_required("admin")
def acknowledge(alert_id):
    updated = db.session.execute(
        db.update(WeaponAlert)
        .where(WeaponAlert.id == alert_id, WeaponAlert.acknowledged_at.is_(None))
        .values(acknowledged_at=utcnow())
    )
    if updated.rowcount:
        audit_source(db.session.get(WeaponAlert, alert_id), "weapon.acknowledged")
        db.session.commit()
    elif db.session.get(WeaponAlert, alert_id) is None:
        abort(404)
    return jsonify(status="acknowledged")

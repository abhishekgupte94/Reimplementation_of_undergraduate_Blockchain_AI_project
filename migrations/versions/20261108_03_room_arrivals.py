"""Record confirmed room arrivals and retain legacy presence history."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.mysql import DATETIME

revision = "20261108_03_room_arrivals"
down_revision = "20261104_02_weapon_detection"
branch_labels = None
depends_on = None


def upgrade():
    timestamp = sa.DateTime().with_variant(DATETIME(fsp=6), "mysql")
    op.add_column("capture_session", sa.Column("arrival_candidates", sa.JSON()))
    op.create_table(
        "room_arrival",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("user.id"), nullable=False),
        sa.Column("capture_id", sa.String(36), sa.ForeignKey("capture_session.id"), nullable=False),
        sa.Column("model_id", sa.Integer(), sa.ForeignKey("model_version.id"), nullable=False),
        sa.Column("room", sa.Integer(), nullable=False),
        sa.Column("arrived_at", timestamp, nullable=False),
        sa.Column("score", sa.Float(), nullable=False),
    )
    op.create_index("ix_room_arrival_user_id_id", "room_arrival", ["user_id", "id"])
    op.create_index("ix_room_arrival_user_id_arrived_at", "room_arrival", ["user_id", "arrived_at"])


def downgrade():
    op.drop_table("room_arrival")
    op.drop_column("capture_session", "arrival_candidates")

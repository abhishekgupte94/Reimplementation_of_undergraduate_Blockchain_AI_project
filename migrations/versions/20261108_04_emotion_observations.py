"""Persist facial-expression estimates without changing existing audit records."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.mysql import DATETIME

revision = "20261108_04_emotion_observations"
down_revision = "20261108_03_room_arrivals"
branch_labels = None
depends_on = None


def upgrade():
    timestamp = sa.DateTime().with_variant(DATETIME(fsp=6), "mysql")
    op.create_table(
        "emotion_observation",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("user.id"), nullable=False),
        sa.Column("capture_id", sa.String(36), sa.ForeignKey("capture_session.id"), nullable=False),
        sa.Column("model_id", sa.Integer(), sa.ForeignKey("model_version.id"), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("frame_sequence", sa.Integer(), nullable=False),
        sa.Column("room", sa.Integer(), nullable=False),
        sa.Column("observed_at", timestamp, nullable=False),
        sa.Column("emotion", sa.String(16), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("model_version", sa.String(96), nullable=False),
        sa.UniqueConstraint(
            "capture_id",
            "generation",
            "user_id",
            "frame_sequence",
            name="uq_emotion_observation_sample",
        ),
    )
    op.create_index(
        "ix_emotion_observation_user_id_observed_at",
        "emotion_observation",
        ["user_id", "observed_at"],
    )


def downgrade():
    op.drop_table("emotion_observation")

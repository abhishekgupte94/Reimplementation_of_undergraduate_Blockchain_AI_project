"""Persist independent SSD weapon observations.

Revision ID: 20261104_02_weapon_detection
Revises: 20261104_01_mysql_ledger
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.mysql import DATETIME

revision = "20261104_02_weapon_detection"
down_revision = "20261104_01_mysql_ledger"
branch_labels = None
depends_on = None


def upgrade():
    timestamp = sa.DateTime().with_variant(DATETIME(fsp=6), "mysql")
    op.create_table(
        "weapon_alert",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("capture_id", sa.String(36), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("frame_sequence", sa.Integer(), nullable=False),
        sa.Column("room", sa.Integer(), nullable=False),
        sa.Column("label", sa.String(32), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("box", sa.JSON(), nullable=False),
        sa.Column("model_tag", sa.String(80), nullable=False),
        sa.Column("observed_at", timestamp, nullable=False),
        sa.Column("created_at", timestamp, nullable=False),
        sa.Column("acknowledged_at", timestamp),
        sa.ForeignKeyConstraint(
            ["capture_id"],
            ["capture_session.id"],
            name="fk_weapon_alert_capture_id_capture_session",
        ),
    )
    op.create_index("ix_weapon_alert_observed_at", "weapon_alert", ["observed_at"])


def downgrade():
    op.drop_index("ix_weapon_alert_observed_at", table_name="weapon_alert")
    op.drop_table("weapon_alert")

"""Transactional hash-chained audit log and Ethereum anchoring outbox."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.mysql import DATETIME

revision = "20261104_01_mysql_ledger"
down_revision = "99673899e8a8"
branch_labels = None
depends_on = None


def upgrade():
    timestamp = sa.DateTime().with_variant(DATETIME(fsp=6), "mysql")
    op.create_table(
        "ledger_state",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("head_hash", sa.String(64), nullable=False),
        sa.Column("chain_id", sa.String(32)),
        sa.Column("genesis_hash", sa.String(66)),
    )
    op.create_table(
        "audit_entry",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=False),
        sa.Column("kind", sa.String(64), nullable=False),
        sa.Column("source_type", sa.String(64)),
        sa.Column("source_id", sa.String(64)),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("previous_hash", sa.String(64), nullable=False),
        sa.Column("entry_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("created_at", timestamp, nullable=False),
    )
    op.create_index("ix_audit_entry_source_type", "audit_entry", ["source_type"])
    op.create_table(
        "ledger_anchor",
        sa.Column("entry_id", sa.Integer(), sa.ForeignKey("audit_entry.id"), primary_key=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("transaction_hash", sa.String(66)),
        sa.Column("block_number", sa.Integer()),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", timestamp, nullable=False),
        sa.Column("last_error", sa.String(500)),
        sa.Column("anchored_at", timestamp),
    )
    op.create_index("ix_ledger_anchor_status", "ledger_anchor", ["status"])
    op.execute(
        sa.text(
            "INSERT INTO ledger_state (id, sequence, head_hash) VALUES (1, 0, :head)"
        ).bindparams(head="0" * 64)
    )


def downgrade():
    op.drop_table("ledger_anchor")
    op.drop_table("audit_entry")
    op.drop_table("ledger_state")

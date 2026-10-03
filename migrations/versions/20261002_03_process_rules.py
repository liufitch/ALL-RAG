"""Add durable dataset process rules."""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "20261002_03"
down_revision: str | None = "20261002_02"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "dataset_process_rules",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("dataset_id", sa.String(length=36), nullable=False),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("mode", sa.String(length=32), nullable=False),
        sa.Column("rules", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("config_hash", sa.String(length=64), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("created_by", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["dataset_id"], ["datasets.id"], name="fk_dataset_process_rules_dataset"),
        sa.PrimaryKeyConstraint("id", name="pk_dataset_process_rules"),
    )
    op.create_index("ix_dataset_process_rules_dataset_hash", "dataset_process_rules", ["dataset_id", "config_hash"])
    op.create_foreign_key(
        "fk_documents_process_rule",
        "documents",
        "dataset_process_rules",
        ["dataset_process_rule_id"],
        ["id"],
    )


def downgrade() -> None:
    op.drop_constraint("fk_documents_process_rule", "documents", type_="foreignkey")
    op.drop_index("ix_dataset_process_rules_dataset_hash", table_name="dataset_process_rules")
    op.drop_table("dataset_process_rules")

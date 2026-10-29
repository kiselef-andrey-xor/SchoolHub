"""ПЭП через аутентификатор (TOTP) + политика подписи приказов.

Revision ID: 0013_totp_pep
Revises: 0012_certifications
Create Date: 2026-10-28

Колонки:
  users.totp_secret_enc / totp_enabled / totp_confirmed_at / recovery_word_set_at
  orders.signature_policy            — требуемый уровень подписи (NONE/CLICK/TOTP/CODE_WORD)
  order_acknowledgements.sign_method — фактический способ подписи (для листа ознакомления и печати)

DDL совместим с PostgreSQL и SQLite (типы, соответствующие ORM из app.models).
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0013_totp_pep"
down_revision: Union[str, None] = "0012_certifications"
branch_labels: Union[Sequence[str], None] = None
depends_on: Union[Sequence[str], None] = None


def _has_column(table: str, column: str) -> bool:
    bind = op.get_bind()
    return any(c["name"] == column for c in sa.inspect(bind).get_columns(table))


def upgrade() -> None:
    if not _has_column("users", "totp_secret_enc"):
        op.add_column("users", sa.Column("totp_secret_enc", sa.String(512), nullable=True))
    if not _has_column("users", "totp_enabled"):
        op.add_column("users", sa.Column("totp_enabled", sa.Boolean(), nullable=False, server_default=sa.false()))
    if not _has_column("users", "totp_confirmed_at"):
        op.add_column("users", sa.Column("totp_confirmed_at", sa.DateTime(timezone=True), nullable=True))
    if not _has_column("users", "recovery_word_set_at"):
        op.add_column("users", sa.Column("recovery_word_set_at", sa.DateTime(timezone=True), nullable=True))
    if not _has_column("orders", "signature_policy"):
        op.add_column(
            "orders", sa.Column("signature_policy", sa.String(20), nullable=False, server_default="CLICK")
        )
    if not _has_column("order_acknowledgements", "sign_method"):
        op.add_column("order_acknowledgements", sa.Column("sign_method", sa.String(20), nullable=True))


def downgrade() -> None:
    if _has_column("order_acknowledgements", "sign_method"):
        op.drop_column("order_acknowledgements", "sign_method")
    if _has_column("orders", "signature_policy"):
        op.drop_column("orders", "signature_policy")
    if _has_column("users", "recovery_word_set_at"):
        op.drop_column("users", "recovery_word_set_at")
    if _has_column("users", "totp_confirmed_at"):
        op.drop_column("users", "totp_confirmed_at")
    if _has_column("users", "totp_enabled"):
        op.drop_column("users", "totp_enabled")
    if _has_column("users", "totp_secret_enc"):
        op.drop_column("users", "totp_secret_enc")

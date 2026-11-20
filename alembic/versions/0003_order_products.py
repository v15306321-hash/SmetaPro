"""order products list

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-20
"""
from alembic import op
import sqlalchemy as sa

revision = '0003'
down_revision = '0002'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('orders') as batch_op:
        batch_op.add_column(sa.Column('products', sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column('products_planned', sa.Integer(), nullable=False, server_default='1'))


def downgrade() -> None:
    with op.batch_alter_table('orders') as batch_op:
        batch_op.drop_column('products_planned')
        batch_op.drop_column('products')

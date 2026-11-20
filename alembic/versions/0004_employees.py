"""employees, order workers, workshop fee pricing

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-21
"""
from alembic import op
import sqlalchemy as sa

revision = '0004'
down_revision = '0003'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'employees',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('enterprise_id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(length=120), nullable=False),
        sa.Column('position', sa.String(length=80), nullable=False),
        sa.Column('pay_type', sa.String(length=10), nullable=False),
        sa.Column('pay_rate', sa.Numeric(14, 2), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=True),
        sa.Column('is_active', sa.Boolean(), nullable=False),
        sa.Column('fired_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['enterprise_id'], ['enterprises.id']),
        sa.ForeignKeyConstraint(['user_id'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_employees_enterprise_id', 'employees', ['enterprise_id'])
    op.create_table(
        'order_workers',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('order_id', sa.Integer(), nullable=False),
        sa.Column('employee_id', sa.Integer(), nullable=True),
        sa.Column('name', sa.String(length=120), nullable=False),
        sa.Column('position', sa.String(length=80), nullable=False),
        sa.Column('pay_type', sa.String(length=10), nullable=False),
        sa.Column('pay_rate', sa.Numeric(14, 2), nullable=False),
        sa.Column('amount', sa.Numeric(16, 2), nullable=False),
        sa.ForeignKeyConstraint(['order_id'], ['orders.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['employee_id'], ['employees.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_order_workers_order_id', 'order_workers', ['order_id'])
    with op.batch_alter_table('orders') as b:
        b.add_column(sa.Column('workers_cost', sa.Numeric(16, 2), nullable=False, server_default='0'))
    with op.batch_alter_table('invitations') as b:
        b.add_column(sa.Column('employee_id', sa.Integer(), nullable=True))
        b.create_foreign_key('fk_invitations_employee', 'employees', ['employee_id'], ['id'])


def downgrade() -> None:
    with op.batch_alter_table('invitations') as b:
        b.drop_constraint('fk_invitations_employee', type_='foreignkey')
        b.drop_column('employee_id')
    with op.batch_alter_table('orders') as b:
        b.drop_column('workers_cost')
    op.drop_index('ix_order_workers_order_id', table_name='order_workers')
    op.drop_table('order_workers')
    op.drop_index('ix_employees_enterprise_id', table_name='employees')
    op.drop_table('employees')

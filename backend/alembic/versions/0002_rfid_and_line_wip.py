"""RFID badge intervals and line work-in-process

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-25 09:00:00
"""
from alembic import op
import sqlalchemy as sa


revision = '0002'
down_revision = '0001'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('rfid_intervals',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('employee_id', sa.Integer(), nullable=False),
    sa.Column('factory_id', sa.Integer(), nullable=False),
    sa.Column('line_id', sa.Integer(), nullable=True),
    sa.Column('date', sa.Date(), nullable=False),
    sa.Column('shift', sa.Integer(), nullable=False),
    sa.Column('state', sa.String(length=10), nullable=False),
    sa.Column('zone', sa.String(length=48), nullable=True),
    sa.Column('reader', sa.String(length=24), nullable=True),
    sa.Column('reason', sa.String(length=40), nullable=True),
    sa.Column('start', sa.DateTime(), nullable=False),
    sa.Column('end', sa.DateTime(), nullable=True),
    sa.Column('planned_end', sa.DateTime(), nullable=True),
    sa.ForeignKeyConstraint(['employee_id'], ['employees.id'], ),
    sa.ForeignKeyConstraint(['factory_id'], ['factories.id'], ),
    sa.ForeignKeyConstraint(['line_id'], ['production_lines.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('rfid_intervals', schema=None) as batch_op:
        batch_op.create_index('ix_rfid_emp_date', ['employee_id', 'date'], unique=False)
        batch_op.create_index('ix_rfid_factory_date', ['factory_id', 'date'], unique=False)
        batch_op.create_index('ix_rfid_open', ['end'], unique=False)

    op.create_table('line_wip',
    sa.Column('line_id', sa.Integer(), nullable=False),
    sa.Column('date', sa.Date(), nullable=True),
    sa.Column('sewing', sa.Float(), nullable=True),
    sa.Column('bagging', sa.Float(), nullable=True),
    sa.Column('updated_at', sa.DateTime(), nullable=True),
    sa.ForeignKeyConstraint(['line_id'], ['production_lines.id'], ),
    sa.PrimaryKeyConstraint('line_id')
    )


def downgrade() -> None:
    op.drop_table('line_wip')
    with op.batch_alter_table('rfid_intervals', schema=None) as batch_op:
        batch_op.drop_index('ix_rfid_open')
        batch_op.drop_index('ix_rfid_factory_date')
        batch_op.drop_index('ix_rfid_emp_date')
    op.drop_table('rfid_intervals')

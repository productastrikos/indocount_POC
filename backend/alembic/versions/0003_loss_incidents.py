"""Loss prevention: pilferage / unexplained loss cases

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-26 09:00:00
"""
from alembic import op
import sqlalchemy as sa


revision = '0003'
down_revision = '0002'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('loss_incidents',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('ref', sa.String(length=20), nullable=False),
    sa.Column('factory_id', sa.Integer(), nullable=False),
    sa.Column('kind', sa.String(length=28), nullable=False),
    sa.Column('stage', sa.String(length=20), nullable=True),
    sa.Column('severity', sa.String(length=10), nullable=True),
    sa.Column('status', sa.String(length=14), nullable=True),
    sa.Column('outcome', sa.String(length=20), nullable=True),
    sa.Column('title', sa.String(length=200), nullable=True),
    sa.Column('summary', sa.Text(), nullable=True),
    sa.Column('zone', sa.String(length=60), nullable=True),
    sa.Column('area', sa.String(length=20), nullable=True),
    sa.Column('area_ref', sa.String(length=24), nullable=True),
    sa.Column('channel', sa.String(length=60), nullable=True),
    sa.Column('item', sa.String(length=90), nullable=True),
    sa.Column('item_kind', sa.String(length=12), nullable=True),
    sa.Column('qty', sa.Float(), nullable=True),
    sa.Column('unit', sa.String(length=10), nullable=True),
    sa.Column('value_usd', sa.Float(), nullable=True),
    sa.Column('recovered_qty', sa.Float(), nullable=True),
    sa.Column('detected_at', sa.DateTime(), nullable=True),
    sa.Column('updated_at', sa.DateTime(), nullable=True),
    sa.Column('resolved_at', sa.DateTime(), nullable=True),
    sa.Column('assigned_to', sa.String(length=60), nullable=True),
    sa.Column('camera_code', sa.String(length=16), nullable=True),
    sa.Column('feed_t', sa.Float(), nullable=True),
    sa.Column('employee_id', sa.Integer(), nullable=True),
    sa.Column('line_id', sa.Integer(), nullable=True),
    sa.Column('location_id', sa.Integer(), nullable=True),
    sa.Column('truck_id', sa.Integer(), nullable=True),
    sa.Column('shipment_id', sa.Integer(), nullable=True),
    sa.Column('evidence', sa.JSON(), nullable=True),
    sa.Column('timeline', sa.JSON(), nullable=True),
    sa.Column('note', sa.Text(), nullable=True),
    sa.ForeignKeyConstraint(['factory_id'], ['factories.id'], ),
    sa.ForeignKeyConstraint(['employee_id'], ['employees.id'], ),
    sa.ForeignKeyConstraint(['line_id'], ['production_lines.id'], ),
    sa.ForeignKeyConstraint(['location_id'], ['storage_locations.id'], ),
    sa.ForeignKeyConstraint(['truck_id'], ['truck_appointments.id'], ),
    sa.ForeignKeyConstraint(['shipment_id'], ['inbound_shipments.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('ref')
    )
    with op.batch_alter_table('loss_incidents', schema=None) as batch_op:
        batch_op.create_index('ix_loss_factory_status', ['factory_id', 'status'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('loss_incidents', schema=None) as batch_op:
        batch_op.drop_index('ix_loss_factory_status')
    op.drop_table('loss_incidents')

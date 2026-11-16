"""Versioned metric definitions and confirmed query plans."""
from alembic import op
import sqlalchemy as sa

revision = 'metric_tools_073'
down_revision = 'f898322341a5'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('metric_definition',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('oid', sa.BigInteger(), nullable=False),
        sa.Column('datasource_id', sa.BigInteger(), nullable=False),
        sa.Column('metric_code', sa.String(40), nullable=False),
        sa.Column('version', sa.Integer(), nullable=False),
        sa.Column('binding', sa.JSON(), nullable=False),
        sa.Column('created_by', sa.BigInteger(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.UniqueConstraint('oid', 'datasource_id', 'metric_code', 'version', name='uq_metric_version'))
    op.create_table('metric_query',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('oid', sa.BigInteger(), nullable=False),
        sa.Column('user_id', sa.BigInteger(), nullable=False),
        sa.Column('datasource_id', sa.BigInteger(), nullable=False),
        sa.Column('definition_id', sa.String(36), nullable=False),
        sa.Column('plan', sa.JSON(), nullable=False),
        sa.Column('status', sa.String(20), nullable=False),
        sa.Column('result', sa.JSON(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False))
    op.create_index('ix_metric_query_owner', 'metric_query', ['oid', 'user_id', 'created_at'])


def downgrade():
    op.drop_index('ix_metric_query_owner', table_name='metric_query')
    op.drop_table('metric_query')
    op.drop_table('metric_definition')

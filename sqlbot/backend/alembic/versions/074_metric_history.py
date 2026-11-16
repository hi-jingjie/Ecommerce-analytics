"""Retain metric resolution, stage timing and safe failure history."""
from alembic import op
import sqlalchemy as sa

revision = 'metric_history_074'
down_revision = 'metric_tools_073'
branch_labels = None
depends_on = None


def upgrade():
    op.alter_column('metric_query', 'definition_id', existing_type=sa.String(36), nullable=True)
    op.add_column('metric_query', sa.Column('question', sa.Text(), nullable=True))
    for name in ('resolution', 'events', 'error'):
        op.add_column('metric_query', sa.Column(name, sa.JSON(), nullable=True))


def downgrade():
    # A downgrade must not silently discard resolution-only history.
    connection = op.get_bind()
    if connection.execute(sa.text('SELECT count(*) FROM metric_query WHERE definition_id IS NULL')).scalar():
        raise RuntimeError('Resolution-only history exists; archive it before downgrading.')
    for name in ('error', 'events', 'resolution', 'question'):
        op.drop_column('metric_query', name)
    op.alter_column('metric_query', 'definition_id', existing_type=sa.String(36), nullable=False)

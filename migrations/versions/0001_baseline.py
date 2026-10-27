"""V0.5 baseline schema.

Fresh databases create the complete current schema here. Existing V0.4 databases are
stamped to this revision by scripts/migrate.py and are not recreated.
"""
from alembic import op

revision = "0001_baseline"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    from app.db import Base
    import app.models  # noqa: F401

    Base.metadata.create_all(bind=op.get_bind())


def downgrade() -> None:
    from app.db import Base
    import app.models  # noqa: F401

    Base.metadata.drop_all(bind=op.get_bind())

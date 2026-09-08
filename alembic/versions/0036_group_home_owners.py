"""Require each group home to have a guardian or resident-patient owner."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0036_group_home_owners"
down_revision = "0035_group_home_statuses"
branch_labels = None
depends_on = None

def upgrade():
    uuid = postgresql.UUID(as_uuid=True)
    op.add_column("group_homes", sa.Column("guardian_id", uuid, sa.ForeignKey("guardian_profiles.id", ondelete="SET NULL"), nullable=True))
    op.add_column("group_homes", sa.Column("owner_patient_id", uuid, sa.ForeignKey("patient_profiles.id", ondelete="SET NULL"), nullable=True))
    # Existing experimental homes are permitted temporarily; new writes are
    # validated in the API until historical rows have assigned owners.

def downgrade():
    op.drop_column("group_homes", "owner_patient_id")
    op.drop_column("group_homes", "guardian_id")

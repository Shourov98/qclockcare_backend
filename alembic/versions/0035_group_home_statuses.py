from alembic import op
import sqlalchemy as sa
revision = "0035_group_home_statuses"
down_revision = "0034_group_homes"
branch_labels = None
depends_on = None
def upgrade():
    op.add_column("group_homes", sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()))
    op.add_column("group_home_appointments", sa.Column("status", sa.String(16), nullable=False, server_default="SCHEDULED"))
def downgrade():
    op.drop_column("group_home_appointments", "status"); op.drop_column("group_homes", "is_active")

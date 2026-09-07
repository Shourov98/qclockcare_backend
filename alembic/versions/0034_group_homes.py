"""Add group homes, max-four memberships, and shared appointments."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0034_group_homes"
down_revision = "0033_appointment_location_id"
branch_labels = None
depends_on = None

def upgrade():
    uuid = postgresql.UUID(as_uuid=True)
    op.create_table("group_homes", sa.Column("id", uuid, primary_key=True), sa.Column("agency_id", uuid, sa.ForeignKey("agencies.id", ondelete="CASCADE"), nullable=False), sa.Column("location_id", uuid, sa.ForeignKey("locations.id", ondelete="RESTRICT"), nullable=False, unique=True), sa.Column("name", sa.Text(), nullable=False), sa.Column("capacity", sa.Integer(), nullable=False, server_default="4"), sa.CheckConstraint("capacity = 4", name="group_homes_capacity_four"), sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False), sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False))
    op.create_table("group_home_members", sa.Column("id", uuid, primary_key=True), sa.Column("group_home_id", uuid, sa.ForeignKey("group_homes.id", ondelete="CASCADE"), nullable=False), sa.Column("patient_id", uuid, sa.ForeignKey("patient_profiles.id", ondelete="CASCADE"), nullable=False), sa.Column("removed_at", sa.DateTime(timezone=True)), sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False), sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False), sa.UniqueConstraint("group_home_id", "patient_id", name="uq_group_home_member"))
    op.create_table("group_home_appointments", sa.Column("id", uuid, primary_key=True), sa.Column("agency_id", uuid, sa.ForeignKey("agencies.id", ondelete="CASCADE"), nullable=False), sa.Column("group_home_id", uuid, sa.ForeignKey("group_homes.id", ondelete="CASCADE"), nullable=False), sa.Column("staff_id", uuid, sa.ForeignKey("staff_profiles.id", ondelete="SET NULL")), sa.Column("service", sa.Text(), nullable=False), sa.Column("scheduled_start", sa.DateTime(timezone=True), nullable=False), sa.Column("scheduled_end", sa.DateTime(timezone=True), nullable=False), sa.Column("notes", sa.Text()), sa.CheckConstraint("scheduled_end > scheduled_start", name="group_appointment_window"), sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False), sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False))
    op.create_table("group_home_appointment_patients", sa.Column("id", uuid, primary_key=True), sa.Column("group_appointment_id", uuid, sa.ForeignKey("group_home_appointments.id", ondelete="CASCADE"), nullable=False), sa.Column("patient_id", uuid, sa.ForeignKey("patient_profiles.id", ondelete="RESTRICT"), nullable=False), sa.UniqueConstraint("group_appointment_id", "patient_id", name="uq_group_appointment_patient"))

def downgrade():
    op.drop_table("group_home_appointment_patients"); op.drop_table("group_home_appointments"); op.drop_table("group_home_members"); op.drop_table("group_homes")

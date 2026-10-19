-- One-time adoption of a database whose tables were created by an earlier app version
-- (SQLAlchemy create_all) so that Prisma can manage it. Brings names, types, defaults and the foreign
-- key into exactly the shape of migration 20261019000000_init; existing rows are kept.
-- Run automatically by scripts/db_migrate.py, which then marks that migration as applied.
ALTER INDEX IF EXISTS "ix_users_username" RENAME TO "users_username_key";
ALTER INDEX IF EXISTS "ix_assessments_user_id" RENAME TO "assessments_user_id_idx";
ALTER INDEX IF EXISTS "ix_assessments_created_at" RENAME TO "assessments_created_at_idx";

ALTER TABLE "users"
    ALTER COLUMN "role" SET DEFAULT 'clinician',
    ALTER COLUMN "active" SET DEFAULT true,
    ALTER COLUMN "created_at" TYPE TIMESTAMPTZ(6),
    ALTER COLUMN "created_at" SET DEFAULT CURRENT_TIMESTAMP;

ALTER TABLE "assessments"
    ALTER COLUMN "inputs" TYPE JSONB USING "inputs"::jsonb,
    ALTER COLUMN "results" TYPE JSONB USING "results"::jsonb,
    ALTER COLUMN "created_at" TYPE TIMESTAMPTZ(6),
    ALTER COLUMN "created_at" SET DEFAULT CURRENT_TIMESTAMP;

ALTER TABLE "assessments" DROP CONSTRAINT IF EXISTS "assessments_user_id_fkey";
ALTER TABLE "assessments" ADD CONSTRAINT "assessments_user_id_fkey"
    FOREIGN KEY ("user_id") REFERENCES "users"("id") ON DELETE RESTRICT ON UPDATE CASCADE;

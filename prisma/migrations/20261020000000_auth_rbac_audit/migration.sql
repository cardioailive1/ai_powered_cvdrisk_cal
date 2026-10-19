-- Accounts with approval workflow, MFA, lockout and forced password change; server-side sessions;
-- append-only audit trail; shared sign-in throttle.

-- AlterTable: users
ALTER TABLE "users" ALTER COLUMN "username" SET DATA TYPE VARCHAR(254),
ADD COLUMN     "full_name" VARCHAR(120) NOT NULL DEFAULT '',
ADD COLUMN     "organization" VARCHAR(160) NOT NULL DEFAULT '',
ADD COLUMN     "status" VARCHAR(20) NOT NULL DEFAULT 'pending',
ADD COLUMN     "mfa_secret" VARCHAR(255),
ADD COLUMN     "mfa_enabled" BOOLEAN NOT NULL DEFAULT false,
ADD COLUMN     "mfa_last_step" INTEGER,
ADD COLUMN     "recovery_codes" JSONB NOT NULL DEFAULT '[]',
ADD COLUMN     "failed_logins" INTEGER NOT NULL DEFAULT 0,
ADD COLUMN     "locked_until" TIMESTAMPTZ(6),
ADD COLUMN     "must_change_password" BOOLEAN NOT NULL DEFAULT false,
ADD COLUMN     "password_changed_at" TIMESTAMPTZ(6) NOT NULL DEFAULT CURRENT_TIMESTAMP,
ADD COLUMN     "last_login_at" TIMESTAMPTZ(6),
ADD COLUMN     "approved_by_id" INTEGER,
ADD COLUMN     "approved_at" TIMESTAMPTZ(6),
ADD COLUMN     "terms_accepted_at" TIMESTAMPTZ(6);

-- Existing accounts keep working: active -> status 'active', inactive -> 'disabled'. Their first sign-in after
-- this release enrols MFA. Usernames are matched case-insensitively from now on.
UPDATE "users" SET "status" = CASE WHEN "active" THEN 'active' ELSE 'disabled' END, "approved_at" = "created_at";
UPDATE "users" SET "username" = lower(trim("username"));
ALTER TABLE "users" DROP COLUMN "active";

-- CreateTable
CREATE TABLE "auth_sessions" (
    "id" SERIAL NOT NULL,
    "user_id" INTEGER NOT NULL,
    "token_hash" VARCHAR(64) NOT NULL,
    "scope" VARCHAR(20) NOT NULL DEFAULT 'full',
    "ip" VARCHAR(64) NOT NULL DEFAULT '',
    "user_agent" VARCHAR(255) NOT NULL DEFAULT '',
    "created_at" TIMESTAMPTZ(6) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "last_seen_at" TIMESTAMPTZ(6) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "expires_at" TIMESTAMPTZ(6) NOT NULL,
    "revoked_at" TIMESTAMPTZ(6),

    CONSTRAINT "auth_sessions_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "audit_events" (
    "id" SERIAL NOT NULL,
    "created_at" TIMESTAMPTZ(6) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "actor_id" INTEGER,
    "actor" VARCHAR(254) NOT NULL DEFAULT '',
    "action" VARCHAR(60) NOT NULL,
    "outcome" VARCHAR(20) NOT NULL,
    "target_type" VARCHAR(40) NOT NULL DEFAULT '',
    "target_id" VARCHAR(64) NOT NULL DEFAULT '',
    "ip" VARCHAR(64) NOT NULL DEFAULT '',
    "user_agent" VARCHAR(255) NOT NULL DEFAULT '',
    "details" JSONB NOT NULL DEFAULT '{}',
    "prev_hash" VARCHAR(64) NOT NULL,
    "hash" VARCHAR(64) NOT NULL,

    CONSTRAINT "audit_events_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "auth_throttle" (
    "id" SERIAL NOT NULL,
    "key" VARCHAR(128) NOT NULL,
    "created_at" TIMESTAMPTZ(6) NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "auth_throttle_pkey" PRIMARY KEY ("id")
);

-- CreateIndex
CREATE UNIQUE INDEX "auth_sessions_token_hash_key" ON "auth_sessions"("token_hash");

-- CreateIndex
CREATE INDEX "auth_sessions_user_id_idx" ON "auth_sessions"("user_id");

-- CreateIndex
CREATE UNIQUE INDEX "audit_events_hash_key" ON "audit_events"("hash");

-- CreateIndex
CREATE INDEX "audit_events_created_at_idx" ON "audit_events"("created_at");

-- CreateIndex
CREATE INDEX "audit_events_actor_id_idx" ON "audit_events"("actor_id");

-- CreateIndex
CREATE INDEX "audit_events_action_idx" ON "audit_events"("action");

-- CreateIndex
CREATE INDEX "auth_throttle_key_created_at_idx" ON "auth_throttle"("key", "created_at");

-- AddForeignKey
ALTER TABLE "auth_sessions" ADD CONSTRAINT "auth_sessions_user_id_fkey" FOREIGN KEY ("user_id") REFERENCES "users"("id") ON DELETE CASCADE ON UPDATE CASCADE;

-- Audit trail is append-only (HIPAA §164.312(b), SOC 2 CC7.2). Prisma does not model triggers, so this does not
-- affect `prisma migrate diff`. Removing it requires a new, reviewed migration.
CREATE FUNCTION "audit_events_append_only"() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'audit_events is append-only (% blocked)', TG_OP USING ERRCODE = 'insufficient_privilege';
END;
$$;

CREATE TRIGGER "audit_events_no_update_delete" BEFORE UPDATE OR DELETE ON "audit_events"
    FOR EACH ROW EXECUTE FUNCTION "audit_events_append_only"();

CREATE TRIGGER "audit_events_no_truncate" BEFORE TRUNCATE ON "audit_events"
    FOR EACH STATEMENT EXECUTE FUNCTION "audit_events_append_only"();

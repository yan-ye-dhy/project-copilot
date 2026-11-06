"""Isolate higher Context/proposal capacity to a valid personal grant."""
from alembic import op
import sqlalchemy as sa

revision = "0015_personal_capacity"
down_revision = "0014_execution_slots"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("ai_jobs", sa.Column("failure_type", sa.String(40), nullable=True))
    # No client-controlled GUC can select these limits. The administrator's
    # persisted singleton grant and its bounded snapshot are the sole source.
    op.execute("""CREATE FUNCTION personal_operation_limits(owner_id uuid, wall timestamptz)
    RETURNS integer[] LANGUAGE plpgsql AS $$
    DECLARE control release_control%ROWTYPE; limits jsonb; started timestamptz;
            result integer[] := ARRAY[5,3,10,3]; names text[] :=
              ARRAY['context_hourly','context_active','proposal_daily','proposal_pending'];
            ceilings integer[] := ARRAY[100,30,200,30]; value text; slot_number integer;
    BEGIN
      SELECT * INTO control FROM release_control WHERE id=1 FOR SHARE;
      IF NOT FOUND OR control.state IS DISTINCT FROM 'PERSONAL_DEMO'
        OR control.resource_id IS DISTINCT FROM 'public-demo-v2'
        OR control.allowed_user_ids IS DISTINCT FROM jsonb_build_array(owner_id::text)
        OR control.evidence->>'profile' IS DISTINCT FROM 'personal-demo-v2'
        OR control.expires_at IS NULL OR control.expires_at <= wall
        OR jsonb_typeof(control.evidence->'limits') IS DISTINCT FROM 'object'
        OR jsonb_typeof(control.evidence->'started_at') IS DISTINCT FROM 'string'
        OR jsonb_typeof(control.evidence->'grant_id') IS DISTINCT FROM 'string'
        OR control.evidence->>'grant_id' !~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
        OR control.code_fingerprint IS NULL OR control.code_fingerprint !~ '^[0-9a-f]{64}$'
        OR control.evidence->>'configuration_fingerprint' IS NULL
        OR control.evidence->>'configuration_fingerprint' !~ '^[0-9a-f]{64}$'
        OR NOT EXISTS (SELECT 1 FROM user_accounts WHERE id=owner_id AND active)
        THEN RETURN result; END IF;
      -- Require an explicit timezone. A session timezone must not silently
      -- change the maximum 24-hour administrator authorization.
      IF control.evidence->>'started_at' !~ '(Z|[+-][0-9]{2}:[0-9]{2})$'
        THEN RETURN result; END IF;
      started := (control.evidence->>'started_at')::timestamptz;
      IF started > wall OR control.expires_at > started + interval '24 hours'
        THEN RETURN result; END IF;
      limits := control.evidence->'limits';
      FOR slot_number IN 1..4 LOOP
        value := limits->>names[slot_number];
        IF jsonb_typeof(limits->names[slot_number]) IS DISTINCT FROM 'number'
          OR value IS NULL OR value !~ '^[1-9][0-9]{0,2}$'
          THEN RETURN ARRAY[5,3,10,3]; END IF;
        IF value::integer > ceilings[slot_number] THEN RETURN ARRAY[5,3,10,3]; END IF;
        result[slot_number] := value::integer;
      END LOOP;
      RETURN result;
    EXCEPTION WHEN invalid_text_representation OR invalid_datetime_format
        OR datetime_field_overflow OR numeric_value_out_of_range
      THEN RETURN ARRAY[5,3,10,3];
    END $$""")
    # Admission -> per-owner policy lock is shared with release transitions.
    # Sampling wall-clock after waits avoids crossed expiry/hour boundaries.
    op.execute("""CREATE OR REPLACE FUNCTION guard_public_context_limit() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE wall timestamptz; limits integer[];
    BEGIN
      IF NEW.owner_user_id IS NULL THEN RETURN NEW; END IF;
      PERFORM set_config('lock_timeout', '5s', true);
      PERFORM pg_advisory_xact_lock_shared(604116220001);
      PERFORM pg_advisory_xact_lock(hashtextextended('pc-public-operations:' || NEW.owner_user_id::text, 0));
      wall := clock_timestamp();
      limits := personal_operation_limits(NEW.owner_user_id, wall);
      IF (SELECT count(*) FROM agent_contexts WHERE owner_user_id=NEW.owner_user_id AND created_at >= wall - interval '1 hour') >= limits[1]
        THEN RAISE EXCEPTION 'public_operation_limit:context_hourly'; END IF;
      IF (SELECT count(*) FROM agent_contexts WHERE owner_user_id=NEW.owner_user_id AND expires_at > wall) >= limits[2]
        THEN RAISE EXCEPTION 'public_operation_limit:context_active'; END IF;
      NEW.created_at := wall;
      NEW.expires_at := wall + interval '24 hours';
      RETURN NEW;
    END $$""")
    op.execute("""CREATE OR REPLACE FUNCTION guard_public_action_limit() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE wall timestamptz; context_expiry timestamptz; limits integer[];
    BEGIN
      IF NEW.owner_user_id IS NULL THEN RETURN NEW; END IF;
      PERFORM set_config('lock_timeout', '5s', true);
      PERFORM pg_advisory_xact_lock_shared(604116220001);
      PERFORM pg_advisory_xact_lock(hashtextextended('pc-public-operations:' || NEW.owner_user_id::text, 0));
      wall := clock_timestamp();
      limits := personal_operation_limits(NEW.owner_user_id, wall);
      SELECT expires_at INTO context_expiry FROM agent_contexts WHERE id=NEW.context_id AND owner_user_id=NEW.owner_user_id;
      IF context_expiry IS NULL OR context_expiry <= wall THEN RAISE EXCEPTION 'public_context_expired'; END IF;
      IF (SELECT count(*) FROM pending_actions WHERE owner_user_id=NEW.owner_user_id AND created_at >= (date_trunc('day', wall AT TIME ZONE 'UTC') AT TIME ZONE 'UTC')) >= limits[3]
        THEN RAISE EXCEPTION 'public_operation_limit:proposal_daily'; END IF;
      IF (SELECT count(*) FROM pending_actions WHERE owner_user_id=NEW.owner_user_id AND status='pending' AND expires_at > wall) >= limits[4]
        THEN RAISE EXCEPTION 'public_operation_limit:proposal_pending'; END IF;
      NEW.created_at := wall;
      NEW.expires_at := LEAST(wall + interval '15 minutes', context_expiry);
      RETURN NEW;
    END $$""")


def downgrade():
    raise RuntimeError("Personal capacity audit and preserved task history cannot be downgraded automatically")

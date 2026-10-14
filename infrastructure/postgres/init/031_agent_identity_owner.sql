-- Platform governance: business identity + owner (not A2A Agent Card fields).
ALTER TABLE agents
  ADD COLUMN IF NOT EXISTS identity TEXT NOT NULL DEFAULT 'assistant',
  ADD COLUMN IF NOT EXISTS owner_user_id UUID REFERENCES users(id);

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint WHERE conname = 'agents_identity_check'
  ) THEN
    ALTER TABLE agents
      ADD CONSTRAINT agents_identity_check
      CHECK (identity IN ('assistant', 'manager', 'employee', 'student', 'support'));
  END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_agents_owner_user_id ON agents(owner_user_id);
CREATE INDEX IF NOT EXISTS idx_agents_identity ON agents(identity);

-- Backfill owner to tenant admin when present.
UPDATE agents a
SET owner_user_id = u.id
FROM users u
WHERE a.owner_user_id IS NULL
  AND a.tenant_id IS NOT NULL
  AND u.tenant_id = a.tenant_id
  AND lower(u.email) = 'admin@aop.local';

DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_schema = 'public' AND table_name = 'task_nodes' AND column_name = 'skill'
  ) AND NOT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_schema = 'public' AND table_name = 'task_nodes' AND column_name = 'agent_key'
  ) THEN
    ALTER TABLE task_nodes RENAME COLUMN skill TO agent_key;
  END IF;
END $$;

-- Create the uuid extension for future authentication features
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- Log that initialization completed
DO $$
BEGIN
    RAISE NOTICE 'Database initialisation script completed.';
END $$;
CREATE TABLE IF NOT EXISTS transactions (
         id SERIAL PRIMARY KEY,
         user_number TEXT,
         account_number TEXT,
         bank_name TEXT,
         account_name TEXT,
         amount INTEGER,
         status TEXT,
         timestamp TIMESTAMP WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP
     );
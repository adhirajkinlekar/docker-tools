-- ============================================================
-- PaymentDB initialisation
-- Creates schema + seeds client and transaction data that
-- matches the ACH company IDs used in the seed ACH files.
--
-- Transaction history covers 6 months (~180 days) to give
-- the agent meaningful context when analysing current files.
-- ============================================================

IF DB_ID('PaymentDB') IS NULL
    CREATE DATABASE PaymentDB;
GO

USE PaymentDB;
GO

-- ─────────────────────────────────────────────────────────────
-- Tables
-- ─────────────────────────────────────────────────────────────

IF OBJECT_ID('client_transactions', 'U') IS NULL
BEGIN
    CREATE TABLE client_transactions (
        transaction_id   INT IDENTITY(1,1) PRIMARY KEY,
        client_id        VARCHAR(20)    NOT NULL,
        transaction_date DATE           NOT NULL,
        amount           DECIMAL(15,2)  NOT NULL,
        transaction_type VARCHAR(10)    NOT NULL,   -- CREDIT | DEBIT
        status           VARCHAR(20)    NOT NULL DEFAULT 'COMPLETED',
        description      VARCHAR(200),
        reference_number VARCHAR(50),
        CONSTRAINT chk_txn_type   CHECK (transaction_type IN ('CREDIT','DEBIT')),
        CONSTRAINT chk_txn_status CHECK (status IN ('COMPLETED','PENDING','FAILED','REVERSED'))
    );
END
GO

IF OBJECT_ID('clients', 'U') IS NULL
BEGIN
    CREATE TABLE clients (
        client_id          VARCHAR(20)    PRIMARY KEY,
        company_name       VARCHAR(100)   NOT NULL,
        ach_company_id     VARCHAR(10)    NOT NULL,
        account_status     VARCHAR(20)    NOT NULL DEFAULT 'ACTIVE',
        credit_limit       DECIMAL(15,2),
        industry           VARCHAR(50),
        risk_rating        VARCHAR(10)    DEFAULT 'LOW',
        onboarded_date     DATE,
        last_activity_date DATE,
        CONSTRAINT chk_client_status CHECK (account_status IN ('ACTIVE','SUSPENDED','CLOSED','PENDING'))
    );

    ALTER TABLE client_transactions
        ADD CONSTRAINT fk_txn_client
        FOREIGN KEY (client_id) REFERENCES clients(client_id);
END
GO

-- ─────────────────────────────────────────────────────────────
-- Seed: Clients
-- ─────────────────────────────────────────────────────────────

MERGE clients AS tgt
USING (VALUES
    ('CLT001', 'ACME CORP',           '1234567890', 'ACTIVE',     500000.00, 'Manufacturing',      'LOW',    '2020-01-15'),
    ('CLT002', 'GLOBAL TECH INC',     '0987654321', 'ACTIVE',    1000000.00, 'Technology',         'LOW',    '2019-06-01'),
    ('CLT003', 'RETAIL SOLUTIONS',    '1122334455', 'SUSPENDED',   250000.00, 'Retail',             'HIGH',   '2021-03-10'),
    ('CLT004', 'HEALTH SERVICES LLC', '5566778899', 'ACTIVE',     750000.00, 'Healthcare',         'LOW',    '2018-11-20'),
    ('CLT005', 'PAYROLL EXPRESS',     '9988776655', 'ACTIVE',    2000000.00, 'Financial Services', 'MEDIUM', '2017-08-05')
) AS src (client_id, company_name, ach_company_id, account_status, credit_limit, industry, risk_rating, onboarded_date)
ON tgt.client_id = src.client_id
WHEN NOT MATCHED THEN INSERT
    (client_id, company_name, ach_company_id, account_status, credit_limit, industry, risk_rating, onboarded_date, last_activity_date)
    VALUES (src.client_id, src.company_name, src.ach_company_id, src.account_status, src.credit_limit,
            src.industry, src.risk_rating, src.onboarded_date, GETDATE());
GO

-- ─────────────────────────────────────────────────────────────
-- Seed: Transactions – 6 months of history per client
-- ─────────────────────────────────────────────────────────────

-- CLT001 – ACME CORP
-- Steady credits, ~5% month-on-month growth; occasional small debits.
IF NOT EXISTS (SELECT 1 FROM client_transactions WHERE client_id = 'CLT001')
BEGIN
    INSERT INTO client_transactions (client_id, transaction_date, amount, transaction_type, status, description, reference_number)
    SELECT 'CLT001', DATEADD(day, -n, GETDATE()),
           -- Amount grows as days-ago shrinks (more recent = larger)
           ROUND(950.00 + ((180 - n) * 1.8), 2),
           CASE WHEN n % 7 = 0 THEN 'DEBIT' ELSE 'CREDIT' END,
           'COMPLETED',
           'ACH Payment – ACME CORP',
           CONCAT('ACME-', FORMAT(DATEADD(day, -n, GETDATE()), 'yyyyMMdd'))
    FROM (VALUES
        (1),(3),(5),(7),(9),(12),(14),(16),(18),(21),(24),(27),(30),
        (35),(40),(45),(50),(55),(60),(70),(80),(90),(100),(110),(120),
        (130),(140),(150),(160),(170),(180)
    ) v(n);
END
GO

-- CLT002 – GLOBAL TECH INC
-- High-volume credits accelerating; rare large debits.
IF NOT EXISTS (SELECT 1 FROM client_transactions WHERE client_id = 'CLT002')
BEGIN
    INSERT INTO client_transactions (client_id, transaction_date, amount, transaction_type, status, description, reference_number)
    SELECT 'CLT002', DATEADD(day, -n, GETDATE()),
           ROUND(
               CASE WHEN n % 4 = 0 THEN 11000.00
                    ELSE 85000.00 + ((180 - n) * 220.0)   -- accelerating growth
               END, 2),
           CASE WHEN n % 8 = 0 THEN 'DEBIT' ELSE 'CREDIT' END,
           'COMPLETED',
           'ACH Transfer – GLOBAL TECH',
           CONCAT('GLBL-', FORMAT(DATEADD(day, -n, GETDATE()), 'yyyyMMdd'))
    FROM (VALUES
        (1),(2),(4),(6),(8),(10),(13),(16),(20),(25),(30),
        (35),(40),(50),(60),(70),(80),(90),(100),(110),(120),
        (130),(140),(150),(160),(170),(180)
    ) v(n);
END
GO

-- CLT003 – RETAIL SOLUTIONS (SUSPENDED)
-- Healthy Jan–Feb, then returns multiply, credits collapse, account suspended.
-- Transactions < 120 days ago show degradation; older ones look normal.
IF NOT EXISTS (SELECT 1 FROM client_transactions WHERE client_id = 'CLT003')
BEGIN
    INSERT INTO client_transactions (client_id, transaction_date, amount, transaction_type, status, description, reference_number)
    SELECT 'CLT003', DATEADD(day, -n, GETDATE()),
           3400.50,
           -- Older (>90 days): mostly credits. Recent: mostly debits/returns.
           CASE
               WHEN n > 90  THEN 'CREDIT'                          -- Jan–Mar: healthy
               WHEN n > 60  THEN CASE WHEN n % 2 = 0 THEN 'DEBIT' ELSE 'CREDIT' END  -- Apr: mixed
               ELSE 'DEBIT'                                         -- May–Jun: all returns
           END,
           -- Recent transactions mostly FAILED
           CASE WHEN n <= 30 THEN 'FAILED' WHEN n <= 60 THEN 'REVERSED' ELSE 'COMPLETED' END,
           'ACH – RETAIL SOLUTIONS',
           CONCAT('RTSL-', n)
    FROM (VALUES
        (1),(3),(5),(8),(12),(15),(20),(25),(30),
        (35),(40),(45),(50),(55),(60),
        (70),(80),(90),(100),(110),(120),(140),(160),(180)
    ) v(n);
END
GO

-- CLT004 – HEALTH SERVICES LLC
-- Higher volumes in winter (days 120–180), tapering toward summer.
IF NOT EXISTS (SELECT 1 FROM client_transactions WHERE client_id = 'CLT004')
BEGIN
    INSERT INTO client_transactions (client_id, transaction_date, amount, transaction_type, status, description, reference_number)
    SELECT 'CLT004', DATEADD(day, -n, GETDATE()),
           -- Winter billing: larger amounts further back; plateau recently
           CASE
               WHEN n > 120 THEN CASE WHEN n % 3 = 0 THEN 18500.00 ELSE 13000.00 END
               WHEN n > 60  THEN CASE WHEN n % 3 = 0 THEN 14500.00 ELSE 10000.00 END
               ELSE              CASE WHEN n % 3 = 0 THEN 11000.00 ELSE  7500.00 END
           END,
           CASE WHEN n % 6 = 0 THEN 'DEBIT' ELSE 'CREDIT' END,
           'COMPLETED',
           'ACH Payment – HEALTH SVC',
           CONCAT('HLTH-', n)
    FROM (VALUES
        (1),(4),(7),(10),(14),(18),(22),(28),(35),(42),(50),
        (60),(70),(80),(90),(100),(110),(120),(135),(150),(165),(180)
    ) v(n);
END
GO

-- CLT005 – PAYROLL EXPRESS
-- Very high volume; March bonus run visible (~day 80–90 back); one failed batch.
IF NOT EXISTS (SELECT 1 FROM client_transactions WHERE client_id = 'CLT005')
BEGIN
    INSERT INTO client_transactions (client_id, transaction_date, amount, transaction_type, status, description, reference_number)
    SELECT 'CLT005', DATEADD(day, -n, GETDATE()),
           CASE
               -- March bonus run window (approx 80–95 days ago)
               WHEN n BETWEEN 80 AND 95 THEN
                   CASE WHEN n % 2 = 0 THEN 67500.00 ELSE 48000.00 END
               ELSE
                   CASE WHEN n % 2 = 0 THEN 44000.00 ELSE 28500.00 END
           END,
           CASE WHEN n % 3 = 0 THEN 'DEBIT' ELSE 'CREDIT' END,
           -- One failed batch around day 5
           CASE WHEN n = 5 THEN 'FAILED' ELSE 'COMPLETED' END,
           'Payroll ACH – PAYROLL EXPRESS',
           CONCAT('PYRL-', n)
    FROM (VALUES
        (1),(2),(3),(4),(5),(6),(7),(8),(9),(10),
        (12),(14),(16),(18),(20),(25),(30),(35),(40),(45),(50),
        (60),(70),(80),(85),(90),(95),(100),(110),(120),
        (130),(140),(150),(160),(170),(180)
    ) v(n);
END
GO

PRINT 'PaymentDB initialisation complete (6 months of transaction history seeded).';
GO

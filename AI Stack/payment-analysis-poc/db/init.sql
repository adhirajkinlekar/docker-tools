-- ============================================================
-- PaymentDB initialisation
-- 8 clients matching the production-grade seed data:
--
--   CLT001  ACME MANUFACTURING     ACTIVE     bi-weekly payroll
--   CLT002  SWIFT PAYROLL LLC      ACTIVE     weekly high-volume payroll
--   CLT003  NEXUS SUPPLY CHAIN     ACTIVE     twice-monthly vendor payments
--   CLT004  MERIDIAN HEALTH SYS    ACTIVE     monthly insurance disbursements
--   CLT005  COASTAL RETAIL PRTNRS  ACTIVE     tri-weekly merchant settlements
--   CLT006  VERTEX TECHNOLOGIES    ACTIVE     twice-monthly SaaS payouts (high growth)
--   CLT007  HARBOR LOGISTICS LLC   SUSPENDED  weekly freight – last drop Feb 2026
--   CLT008  PINNACLE TRADING CO    CLOSED     large batches – debit spike Sep 2025,
--                                             closed Oct 2025
--
-- Transaction history covers 13 months (Jun 2025 → Jun 2026) to span both
-- Archive (12-month) and Active (current) periods.
-- ============================================================

IF DB_ID('PaymentDB') IS NULL
    CREATE DATABASE PaymentDB;
GO

USE PaymentDB;
GO

-- ─────────────────────────────────────────────────────────────
-- Tables
-- ─────────────────────────────────────────────────────────────

-- ach_batches: structured ACH batch data written by the indexer on every file
-- indexed from the Archive folder.  This is the authoritative store for all
-- analytical queries (aggregations, trends, rankings).  Qdrant holds the
-- vectors for semantic search; this table holds the numbers for SQL analytics.
IF OBJECT_ID('ach_batches', 'U') IS NULL
BEGIN
    CREATE TABLE ach_batches (
        batch_id          INT IDENTITY(1,1) PRIMARY KEY,
        -- qdrant_point_id is the MD5-derived UUID computed by the indexer.
        -- It is the cross-system dedup key — the same value is used as the
        -- Qdrant point ID, so MERGE ON this column is fully idempotent.
        qdrant_point_id   VARCHAR(36)    NOT NULL,
        filename          VARCHAR(200)   NOT NULL,
        folder            VARCHAR(50)    NOT NULL DEFAULT 'Archive',
        company_name      VARCHAR(100),
        company_id        VARCHAR(10),
        entry_class       VARCHAR(3),
        entry_description VARCHAR(50),
        effective_date    DATE,
        total_credit      DECIMAL(15,2)  NOT NULL DEFAULT 0,
        total_debit       DECIMAL(15,2)  NOT NULL DEFAULT 0,
        entry_count       INT            NOT NULL DEFAULT 0,
        indexed_at        DATETIME       NOT NULL DEFAULT GETDATE(),
        CONSTRAINT uq_ach_batch UNIQUE (qdrant_point_id)
    );
    CREATE INDEX ix_ach_batches_company ON ach_batches (company_name);
    CREATE INDEX ix_ach_batches_date    ON ach_batches (effective_date);
    CREATE INDEX ix_ach_batches_class   ON ach_batches (entry_class);
END
GO

IF OBJECT_ID('client_transactions', 'U') IS NULL
BEGIN
    CREATE TABLE client_transactions (
        transaction_id   INT IDENTITY(1,1) PRIMARY KEY,
        client_id        VARCHAR(20)    NOT NULL,
        transaction_date DATE           NOT NULL,
        amount           DECIMAL(15,2)  NOT NULL,
        transaction_type VARCHAR(10)    NOT NULL,
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
-- Clients
-- ─────────────────────────────────────────────────────────────

MERGE clients AS tgt
USING (VALUES
    ('CLT001','ACME MANUFACTURING',    '1234567890','ACTIVE',     500000.00,'Manufacturing',     'LOW',   '2018-03-15','2026-06-01'),
    ('CLT002','SWIFT PAYROLL LLC',     '9988776655','ACTIVE',    2500000.00,'Financial Services','LOW',   '2017-08-05','2026-06-06'),
    ('CLT003','NEXUS SUPPLY CHAIN',    '7654321098','ACTIVE',     800000.00,'Supply Chain',      'LOW',   '2019-11-12','2026-06-05'),
    ('CLT004','MERIDIAN HEALTH SYS',   '5566778899','ACTIVE',    3000000.00,'Healthcare',        'LOW',   '2016-04-20','2026-06-03'),
    ('CLT005','COASTAL RETAIL PRTNRS', '1122334455','ACTIVE',     350000.00,'Retail',            'MEDIUM','2020-09-07','2026-06-09'),
    ('CLT006','VERTEX TECHNOLOGIES',   '0987654321','ACTIVE',    1200000.00,'Technology',        'LOW',   '2021-02-28','2026-06-10'),
    ('CLT007','HARBOR LOGISTICS LLC',  '4455667788','SUSPENDED',  400000.00,'Logistics',         'HIGH',  '2019-05-14','2026-02-15'),
    ('CLT008','PINNACLE TRADING CO',   '2233445566','CLOSED',    1000000.00,'Financial Services','HIGH',  '2020-07-01','2025-10-31')
) AS src (client_id, company_name, ach_company_id, account_status, credit_limit,
          industry, risk_rating, onboarded_date, last_activity_date)
ON tgt.client_id = src.client_id
WHEN NOT MATCHED THEN INSERT
    (client_id, company_name, ach_company_id, account_status, credit_limit,
     industry, risk_rating, onboarded_date, last_activity_date)
    VALUES (src.client_id, src.company_name, src.ach_company_id, src.account_status,
            src.credit_limit, src.industry, src.risk_rating,
            CAST(src.onboarded_date AS DATE), CAST(src.last_activity_date AS DATE));
GO

-- ─────────────────────────────────────────────────────────────
-- Helper: 395-day date spine (covers ~13 months back from today)
-- One row per business day roughly matching ACH drop patterns.
-- We generate it once in a CTE and join/filter per company.
-- ─────────────────────────────────────────────────────────────

-- CLT001 – ACME MANUFACTURING
-- Bi-weekly payroll (1st + 15th of month), 3% MoM growth.
-- ~22–38 employees, credit + occasional small debit.
IF NOT EXISTS (SELECT 1 FROM client_transactions WHERE client_id = 'CLT001')
BEGIN
    -- Generate bi-weekly dates for 13 months and insert one row per drop.
    -- We use a numbers table trick via VALUES to enumerate the 26 bi-weekly dates.
    WITH drops AS (
        SELECT CAST(DATEADD(day,
            CASE n%2 WHEN 0 THEN 0 ELSE 14 END,
            CAST(CONCAT(
                CAST(YEAR(DATEADD(month, -(12 - (n/2)), GETDATE())) AS VARCHAR),
                '-',
                RIGHT('0' + CAST(MONTH(DATEADD(month, -(12 - (n/2)), GETDATE())) AS VARCHAR), 2),
                '-01'
            ) AS DATE)
        ) AS DATE) AS drop_date,
        n
        FROM (
            SELECT TOP 26 ROW_NUMBER() OVER (ORDER BY (SELECT NULL)) - 1 AS n
            FROM sys.objects
        ) nums
    ),
    biz_drops AS (
        SELECT
            CASE
                WHEN DATEPART(WEEKDAY, drop_date) = 1 THEN DATEADD(day, 1, drop_date)
                WHEN DATEPART(WEEKDAY, drop_date) = 7 THEN DATEADD(day, 2, drop_date)
                ELSE drop_date
            END AS txn_date,
            n
        FROM drops
        WHERE drop_date <= GETDATE()
          AND drop_date >= DATEADD(month, -13, GETDATE())
    )
    INSERT INTO client_transactions
        (client_id, transaction_date, amount, transaction_type, status, description, reference_number)
    SELECT
        'CLT001',
        txn_date,
        -- ~3% MoM growth: more recent = larger
        ROUND(
            CASE WHEN n % 9 = 0
                 THEN 850.00 + (n * 12.0)           -- occasional small debit (return)
                 ELSE 3200.00 + ((26 - n) * 145.0)  -- growing credit batch total
            END, 2),
        CASE WHEN n % 9 = 0 THEN 'DEBIT' ELSE 'CREDIT' END,
        'COMPLETED',
        'ACH Payroll – ACME MANUFACTURING',
        CONCAT('ACME-', FORMAT(txn_date, 'yyyyMMdd'), '-', RIGHT('00' + CAST(n AS VARCHAR), 2))
    FROM biz_drops;
END
GO

-- CLT002 – SWIFT PAYROLL LLC
-- Weekly Friday drops, high volume, stable growth 1.5% MoM.
-- March has bonus run (~45% larger).
IF NOT EXISTS (SELECT 1 FROM client_transactions WHERE client_id = 'CLT002')
BEGIN
    WITH fridays AS (
        SELECT
            DATEADD(week, -n, CAST(
                DATEADD(day, -(DATEPART(WEEKDAY, GETDATE()) + 1) % 7, GETDATE())
            AS DATE)) AS txn_date,
            n
        FROM (
            SELECT TOP 56 ROW_NUMBER() OVER (ORDER BY (SELECT NULL)) - 1 AS n
            FROM sys.objects
        ) nums
        WHERE DATEADD(week, -n, CAST(
            DATEADD(day, -(DATEPART(WEEKDAY, GETDATE()) + 1) % 7, GETDATE())
        AS DATE)) >= DATEADD(month, -13, GETDATE())
    )
    INSERT INTO client_transactions
        (client_id, transaction_date, amount, transaction_type, status, description, reference_number)
    SELECT
        'CLT002',
        txn_date,
        ROUND(
            CASE
                -- March bonus window (~weeks 14–17 ago relative to Jun 2026)
                WHEN MONTH(txn_date) = 3
                THEN CASE WHEN n % 3 = 0 THEN 14500.00 ELSE 680000.00 + ((56 - n) * 800.0) END
                WHEN n % 5 = 0
                THEN 22000.00                              -- periodic debit (adjustment)
                ELSE 460000.00 + ((56 - n) * 1200.0)      -- stable growth
            END, 2),
        CASE WHEN n % 5 = 0 THEN 'DEBIT' ELSE 'CREDIT' END,
        CASE WHEN n = 3 THEN 'FAILED' ELSE 'COMPLETED' END,
        'Weekly Payroll ACH – SWIFT PAYROLL',
        CONCAT('SWFT-', FORMAT(txn_date, 'yyyyMMdd'))
    FROM fridays;
END
GO

-- CLT003 – NEXUS SUPPLY CHAIN
-- 5th and 20th each month, vendor disbursements, Q4 seasonal peak.
IF NOT EXISTS (SELECT 1 FROM client_transactions WHERE client_id = 'CLT003')
BEGIN
    WITH drops AS (
        SELECT
            CAST(CONCAT(
                CAST(YEAR(DATEADD(month, -m, GETDATE())) AS VARCHAR), '-',
                RIGHT('0' + CAST(MONTH(DATEADD(month, -m, GETDATE())) AS VARCHAR), 2), '-',
                dom
            ) AS DATE) AS raw_date,
            m, dom
        FROM
            (SELECT TOP 13 ROW_NUMBER() OVER (ORDER BY (SELECT NULL)) - 1 AS m FROM sys.objects) months
            CROSS JOIN (VALUES ('05'),('20')) d(dom)
    ),
    biz_drops AS (
        SELECT
            CASE
                WHEN DATEPART(WEEKDAY, raw_date) = 1 THEN DATEADD(day, 1, raw_date)
                WHEN DATEPART(WEEKDAY, raw_date) = 7 THEN DATEADD(day, 2, raw_date)
                ELSE raw_date
            END AS txn_date,
            m, dom,
            MONTH(raw_date) AS drop_month
        FROM drops
        WHERE raw_date <= GETDATE()
    )
    INSERT INTO client_transactions
        (client_id, transaction_date, amount, transaction_type, status, description, reference_number)
    SELECT
        'CLT003',
        txn_date,
        ROUND(
            -- Q4 seasonal boost (Oct-Dec ~35% higher)
            CASE
                WHEN drop_month IN (10, 11, 12) THEN
                    CASE WHEN m % 7 = 0 THEN 18000.00 ELSE 330000.00 + ((26 - m*2) * 500.0) END * 1.35
                ELSE
                    CASE WHEN m % 7 = 0 THEN 12000.00 ELSE 245000.00 + ((26 - m*2) * 500.0) END
            END, 2),
        CASE WHEN m % 7 = 0 THEN 'DEBIT' ELSE 'CREDIT' END,
        'COMPLETED',
        CONCAT('ACH Vendor Pmt – NEXUS ', CASE dom WHEN '05' THEN 'MID' ELSE 'EOM' END),
        CONCAT('NXSC-', FORMAT(txn_date, 'yyyyMMdd'))
    FROM biz_drops;
END
GO

-- CLT004 – MERIDIAN HEALTH SYS
-- Monthly on the 3rd, large insurance disbursements.
-- Q1 (Jan-Mar) 25-35% higher volumes.
IF NOT EXISTS (SELECT 1 FROM client_transactions WHERE client_id = 'CLT004')
BEGIN
    WITH monthly AS (
        SELECT
            CAST(CONCAT(
                CAST(YEAR(DATEADD(month, -m, GETDATE())) AS VARCHAR), '-',
                RIGHT('0' + CAST(MONTH(DATEADD(month, -m, GETDATE())) AS VARCHAR), 2), '-03'
            ) AS DATE) AS raw_date,
            m,
            MONTH(DATEADD(month, -m, GETDATE())) AS drop_month
        FROM (SELECT TOP 13 ROW_NUMBER() OVER (ORDER BY (SELECT NULL)) - 1 AS m FROM sys.objects) nums
    ),
    biz_drops AS (
        SELECT
            CASE
                WHEN DATEPART(WEEKDAY, raw_date) = 1 THEN DATEADD(day, 1, raw_date)
                WHEN DATEPART(WEEKDAY, raw_date) = 7 THEN DATEADD(day, 2, raw_date)
                ELSE raw_date
            END AS txn_date,
            m, drop_month
        FROM monthly
        WHERE raw_date <= GETDATE()
    )
    INSERT INTO client_transactions
        (client_id, transaction_date, amount, transaction_type, status, description, reference_number)
    SELECT
        'CLT004',
        txn_date,
        ROUND(
            CASE drop_month
                WHEN 1 THEN CASE WHEN m % 5 = 0 THEN 95000.00 ELSE 1750000.00 END  -- Jan heavy
                WHEN 2 THEN CASE WHEN m % 5 = 0 THEN 88000.00 ELSE 1620000.00 END  -- Feb heavy
                WHEN 3 THEN CASE WHEN m % 5 = 0 THEN 78000.00 ELSE 1500000.00 END  -- Mar elevated
                ELSE         CASE WHEN m % 5 = 0 THEN 65000.00 ELSE 1200000.00 END  -- rest of year
            END, 2),
        CASE WHEN m % 5 = 0 THEN 'DEBIT' ELSE 'CREDIT' END,
        'COMPLETED',
        'Monthly Insurance Disbursement – MERIDIAN',
        CONCAT('MRDH-', FORMAT(txn_date, 'yyyyMM'))
    FROM biz_drops;
END
GO

-- CLT005 – COASTAL RETAIL PRTNRS
-- Every 3 business days, merchant settlements.
-- Q4 (Nov-Dec) +55%, January +25% (returns). Higher debit ratio throughout.
IF NOT EXISTS (SELECT 1 FROM client_transactions WHERE client_id = 'CLT005')
BEGIN
    -- Approximate every-3-biz-day with roughly 86 drops in 13 months
    WITH nums AS (
        SELECT TOP 90 ROW_NUMBER() OVER (ORDER BY (SELECT NULL)) - 1 AS n
        FROM sys.objects a CROSS JOIN sys.objects b
    ),
    all_biz AS (
        SELECT
            DATEADD(day, -n*3, GETDATE()) AS raw_date,
            n,
            MONTH(DATEADD(day, -n*3, GETDATE())) AS drop_month
        FROM nums
        WHERE DATEPART(WEEKDAY, DATEADD(day, -n*3, GETDATE())) NOT IN (1, 7)
          AND DATEADD(day, -n*3, GETDATE()) >= DATEADD(month, -13, GETDATE())
    )
    INSERT INTO client_transactions
        (client_id, transaction_date, amount, transaction_type, status, description, reference_number)
    SELECT
        'CLT005',
        CAST(raw_date AS DATE),
        ROUND(
            CASE drop_month
                WHEN 11 THEN CASE WHEN n % 6 = 0 THEN 9500.00 ELSE 88000.00  END  -- Nov
                WHEN 12 THEN CASE WHEN n % 5 = 0 THEN 12000.00 ELSE 98000.00 END  -- Dec peak
                WHEN 1  THEN CASE WHEN n % 4 = 0 THEN 11000.00 ELSE 78000.00 END  -- Jan returns
                ELSE         CASE WHEN n % 6 = 0 THEN 6500.00  ELSE 58000.00 END  -- normal
            END, 2),
        CASE WHEN n % 6 = 0 THEN 'DEBIT' ELSE 'CREDIT' END,
        'COMPLETED',
        'Merchant Settlement – COASTAL RETAIL',
        CONCAT('CRPL-', FORMAT(CAST(raw_date AS DATE), 'yyyyMMdd'))
    FROM all_biz;
END
GO

-- CLT006 – VERTEX TECHNOLOGIES
-- 10th and 25th each month, SaaS payouts, aggressive 8% MoM growth.
IF NOT EXISTS (SELECT 1 FROM client_transactions WHERE client_id = 'CLT006')
BEGIN
    WITH drops AS (
        SELECT
            CAST(CONCAT(
                CAST(YEAR(DATEADD(month, -m, GETDATE())) AS VARCHAR), '-',
                RIGHT('0' + CAST(MONTH(DATEADD(month, -m, GETDATE())) AS VARCHAR), 2), '-',
                dom
            ) AS DATE) AS raw_date,
            m
        FROM
            (SELECT TOP 13 ROW_NUMBER() OVER (ORDER BY (SELECT NULL)) - 1 AS m FROM sys.objects) months
            CROSS JOIN (VALUES ('10'),('25')) d(dom)
    ),
    biz_drops AS (
        SELECT
            CASE
                WHEN DATEPART(WEEKDAY, raw_date) = 1 THEN DATEADD(day, 1, raw_date)
                WHEN DATEPART(WEEKDAY, raw_date) = 7 THEN DATEADD(day, 2, raw_date)
                ELSE raw_date
            END AS txn_date,
            m
        FROM drops
        WHERE raw_date <= GETDATE()
    )
    INSERT INTO client_transactions
        (client_id, transaction_date, amount, transaction_type, status, description, reference_number)
    SELECT
        'CLT006',
        txn_date,
        -- 8% MoM growth: older drops (higher m) = smaller amounts
        ROUND(
            CASE WHEN m % 8 = 0
                 THEN 8500.00 * POWER(CAST(0.92 AS FLOAT), m)   -- occasional debit
                 ELSE 165000.00 * POWER(CAST(1.08 AS FLOAT), 13 - m)  -- growing credits
            END, 2),
        CASE WHEN m % 8 = 0 THEN 'DEBIT' ELSE 'CREDIT' END,
        'COMPLETED',
        'SaaS Payout ACH – VERTEX TECHNOLOGIES',
        CONCAT('VRTX-', FORMAT(txn_date, 'yyyyMMdd'))
    FROM biz_drops;
END
GO

-- CLT007 – HARBOR LOGISTICS LLC (SUSPENDED)
-- Weekly Wednesday drops, declining volumes, last active Feb 15 2026.
-- Increasing debit ratio (returns/chargebacks) signals distress.
IF NOT EXISTS (SELECT 1 FROM client_transactions WHERE client_id = 'CLT007')
BEGIN
    WITH wednesdays AS (
        SELECT
            DATEADD(week, -n,
                DATEADD(day,
                    -(DATEPART(WEEKDAY, '2026-02-15') - 4 + 7) % 7,
                    '2026-02-15')
            ) AS txn_date,
            n
        FROM (
            SELECT TOP 38 ROW_NUMBER() OVER (ORDER BY (SELECT NULL)) - 1 AS n
            FROM sys.objects
        ) nums
        WHERE DATEADD(week, -n,
            DATEADD(day,
                -(DATEPART(WEEKDAY, '2026-02-15') - 4 + 7) % 7,
                '2026-02-15')
        ) >= DATEADD(month, -13, GETDATE())
    )
    INSERT INTO client_transactions
        (client_id, transaction_date, amount, transaction_type, status, description, reference_number)
    SELECT
        'CLT007',
        txn_date,
        ROUND(
            -- Declining: older (higher n) = larger amounts; recent = tiny
            CASE
                WHEN n <= 4  THEN CASE WHEN n % 2 = 0 THEN 28000.00 ELSE 8500.00  END  -- near end: mostly returns
                WHEN n <= 12 THEN CASE WHEN n % 3 = 0 THEN 18000.00 ELSE 55000.00 END  -- deteriorating
                WHEN n <= 24 THEN CASE WHEN n % 5 = 0 THEN 12000.00 ELSE 82000.00 END  -- declining normal
                ELSE              CASE WHEN n % 7 = 0 THEN 9500.00  ELSE 98000.00 END  -- peak activity
            END, 2),
        CASE
            WHEN n <= 4  THEN 'DEBIT'   -- last few weeks: all returns
            WHEN n <= 12 THEN CASE WHEN n % 2 = 0 THEN 'DEBIT' ELSE 'CREDIT' END
            ELSE CASE WHEN n % 7 = 0 THEN 'DEBIT' ELSE 'CREDIT' END
        END,
        CASE WHEN n <= 4 THEN 'REVERSED' WHEN n <= 2 THEN 'FAILED' ELSE 'COMPLETED' END,
        'Freight Payment ACH – HARBOR LOGISTICS',
        CONCAT('HBLG-', FORMAT(txn_date, 'yyyyMMdd'))
    FROM wednesdays;
END
GO

-- CLT008 – PINNACLE TRADING CO (CLOSED)
-- Monthly on 8th, large erratic batches.
-- Sep 2025: massive debit spike (suspected fraud/reversal pattern).
-- Last active Oct 2025.
IF NOT EXISTS (SELECT 1 FROM client_transactions WHERE client_id = 'CLT008')
BEGIN
    WITH monthly AS (
        SELECT
            CAST(CONCAT(
                CAST(YEAR(DATEADD(month, -m, '2025-10-31')) AS VARCHAR), '-',
                RIGHT('0' + CAST(MONTH(DATEADD(month, -m, '2025-10-31')) AS VARCHAR), 2), '-08'
            ) AS DATE) AS raw_date,
            m,
            MONTH(DATEADD(month, -m, '2025-10-31')) AS drop_month,
            YEAR(DATEADD(month, -m, '2025-10-31'))  AS drop_year
        FROM (
            SELECT TOP 5 ROW_NUMBER() OVER (ORDER BY (SELECT NULL)) - 1 AS m
            FROM sys.objects
        ) nums  -- Oct 2025 back 4 months = Jun 2025
    ),
    biz_drops AS (
        SELECT
            CASE
                WHEN DATEPART(WEEKDAY, raw_date) = 1 THEN DATEADD(day, 1, raw_date)
                WHEN DATEPART(WEEKDAY, raw_date) = 7 THEN DATEADD(day, 2, raw_date)
                ELSE raw_date
            END AS txn_date,
            m, drop_month, drop_year
        FROM monthly
    )
    INSERT INTO client_transactions
        (client_id, transaction_date, amount, transaction_type, status, description, reference_number)
    SELECT
        'CLT008',
        txn_date,
        ROUND(
            CASE drop_month
                WHEN 9 THEN                           -- September: debit spike
                    CASE WHEN m % 2 = 0 THEN 2800000.00 ELSE 420000.00 END
                WHEN 10 THEN 180000.00                -- October: last, small
                ELSE                                  -- Jun-Aug: normal large batches
                    CASE WHEN m % 3 = 0 THEN 95000.00 ELSE 680000.00 END
            END, 2),
        CASE drop_month
            WHEN 9  THEN CASE WHEN m % 2 = 0 THEN 'DEBIT' ELSE 'CREDIT' END  -- spike: mostly debits
            WHEN 10 THEN 'CREDIT'
            ELSE CASE WHEN m % 3 = 0 THEN 'DEBIT' ELSE 'CREDIT' END
        END,
        CASE WHEN drop_month = 9 AND m % 2 = 0 THEN 'REVERSED' ELSE 'COMPLETED' END,
        CONCAT(
            CASE drop_month WHEN 9 THEN 'SUSPICIOUS ' ELSE '' END,
            'Trade Settlement – PINNACLE TRADING'
        ),
        CONCAT('PNTC-', FORMAT(txn_date, 'yyyyMM'))
    FROM biz_drops;
END
GO

PRINT 'PaymentDB initialisation complete – 8 clients, 13-month transaction history seeded.';
GO

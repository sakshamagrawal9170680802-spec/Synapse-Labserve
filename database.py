# asyncpg is a Python library used to communicate with PostgreSQL asynchronously.It is a PostgreSQL database driver.A driver is a library that allows your application to communicate with a particular database.
import asyncpg
from config import get_settings

# asyncpg.Pool ek object hai jo PostgreSQL ke multiple connections ko manage karta hai.
# Python mein variable ke beginning mein single underscore conventionally indicate karta hai ki ye internal implementation detail hai.
_pool: asyncpg.Pool | None=None

async def get_db_pool()->asyncpg.Pool:
    """Returns the shared PostgreSQL connection pool, creating it if needed."""
    # global bata raha hai ki _pool naam se koi local variable mat banao balki jo bhar hai ussi ko istemal karo
    global _pool
    if _pool is None:
        settings=get_settings()
        _pool=await asyncpg.create_pool(
            dsn=settings.database_url,
            #it tells pool will start with 2 connections and can maximize it to upto 10 connections if needed 
            min_size=2,
            max_size=10
        )
    return _pool

async def close_db_pool()->None:
    """Gracefully closes all database connections when the server shuts down."""
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool=None

async def create_tables()->None:
    """Creates all LabServe tables if they do not already exist."""
    pool = await get_db_pool()
    #from multiple database connections we are acquiring a connection as conn than executing sql queries
    async with pool.acquire() as conn:
        await conn.execute("""
            
            CREATE EXTENSION IF NOT EXISTS btree_gist;

            CREATE TABLE IF NOT EXISTS operators (
                operator_id VARCHAR(20) PRIMARY KEY,
                name VARCHAR(100) NOT NULL,
                clearance_level INT NOT NULL CHECK (clearance_level BETWEEN 1 AND 5),
                is_active BOOLEAN NOT NULL DEFAULT TRUE
            );

            CREATE TABLE IF NOT EXISTS inventory (
                item_id VARCHAR(20) PRIMARY KEY,
                name VARCHAR(100) NOT NULL,
                category VARCHAR(50) NOT NULL,
                quantity_available INT NOT NULL CHECK (quantity_available >= 0),
                unit_cost NUMERIC(10, 2) NOT NULL CHECK (unit_cost >= 0),
                min_clearance INT NOT NULL DEFAULT 1 CHECK (min_clearance BETWEEN 1 AND 5),
                is_borrowable BOOLEAN NOT NULL DEFAULT TRUE
            );

            CREATE TABLE IF NOT EXISTS fabrication_bays (
                bay_id VARCHAR(20) PRIMARY KEY,
                name VARCHAR(100) NOT NULL,
                bay_type VARCHAR(50) NOT NULL,
                status VARCHAR(20) NOT NULL DEFAULT 'AVAILABLE' CHECK (status IN ('AVAILABLE', 'MAINTENANCE', 'UNAVAILABLE')),
                min_clearance INT NOT NULL DEFAULT 1 CHECK (min_clearance BETWEEN 1 AND 5)
            );

            CREATE TABLE IF NOT EXISTS bay_bookings (
                booking_id SERIAL PRIMARY KEY,
                bay_id VARCHAR(20) NOT NULL REFERENCES fabrication_bays(bay_id),
                operator_id VARCHAR(20) NOT NULL REFERENCES operators(operator_id),
                start_time TIMESTAMPTZ NOT NULL,
                end_time TIMESTAMPTZ NOT NULL,
                purpose TEXT NOT NULL,
                status VARCHAR(20) NOT NULL DEFAULT 'CONFIRMED'
                    CHECK (status IN ('CONFIRMED', 'CANCELLED', 'COMPLETED')),
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                CHECK (end_time > start_time),
                EXCLUDE USING gist (
                    bay_id WITH =,
                    tstzrange(start_time, end_time, '[)') WITH &&
                ) WHERE (status = 'CONFIRMED')
            );

            CREATE TABLE IF NOT EXISTS budgets (
                department VARCHAR(50) PRIMARY KEY,
                total_allocated NUMERIC(12, 2) NOT NULL CHECK (total_allocated>=0),
                spent NUMERIC(12, 2) NOT NULL DEFAULT 0 CHECK (spent >= 0 AND spent <= total_allocated)
            );

            CREATE TABLE IF NOT EXISTS borrow_records (
                record_id SERIAL PRIMARY KEY,
                item_id VARCHAR(20) NOT NULL REFERENCES inventory(item_id),
                operator_id VARCHAR(20) NOT NULL REFERENCES operators(operator_id),
                department VARCHAR(50) NOT NULL REFERENCES budgets(department),
                quantity_borrowed INT NOT NULL CHECK (quantity_borrowed > 0),
                quantity_returned INT NOT NULL DEFAULT 0 CHECK (quantity_returned >= 0 AND quantity_returned <= quantity_borrowed),
                status VARCHAR(20) NOT NULL DEFAULT 'BORROWED' CHECK (status IN ('BORROWED', 'RETURNED')),
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                CHECK (
                    (status = 'BORROWED' AND quantity_returned < quantity_borrowed)
                    OR (status = 'RETURNED' AND quantity_returned = quantity_borrowed)
                )
            );

            CREATE TABLE IF NOT EXISTS purchase_orders (
                order_id SERIAL PRIMARY KEY,
                item_id VARCHAR(20) NOT NULL REFERENCES inventory(item_id),
                operator_id VARCHAR(20) NOT NULL REFERENCES operators(operator_id),
                department VARCHAR(50) NOT NULL REFERENCES budgets(department),
                quantity INT NOT NULL CHECK (quantity > 0),
                total_cost NUMERIC(12, 2) NOT NULL CHECK(total_cost>=0),
                status VARCHAR(30) NOT NULL DEFAULT 'PENDING' CHECK (status IN ('PENDING', 'AWAITING_CHIEF_APPROVAL', 'APPROVED', 'REJECTED')),
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );

            CREATE TABLE IF NOT EXISTS department_inventory (
                department VARCHAR(50) NOT NULL REFERENCES budgets(department),
                item_id VARCHAR(20) NOT NULL REFERENCES inventory(item_id),
                quantity_owned INT NOT NULL DEFAULT 0 CHECK (quantity_owned >= 0),
                quantity_available INT NOT NULL DEFAULT 0 CHECK (quantity_available >= 0),
                quantity_loaned INT NOT NULL DEFAULT 0 CHECK (quantity_loaned >= 0),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                PRIMARY KEY (department, item_id),
                CHECK (quantity_owned = quantity_available + quantity_loaned)
            );
        """)
import asyncio
from database import get_db_pool, close_db_pool, create_tables
from datetime import datetime,timezone

async def seed_database() -> None:
    """Wipes existing records and inserts a fresh, deterministic dataset."""
    await create_tables()
    pool = await get_db_pool()

    async with pool.acquire() as conn:
        async with conn.transaction():
            # Saare 8 tables aur auto-increment IDs cleanly reset karo
            await conn.execute("""
                TRUNCATE TABLE
                    department_inventory,
                    purchase_orders,
                    borrow_records,
                    bay_bookings,
                    budgets,
                    fabrication_bays,
                    inventory,
                    operators
                RESTART IDENTITY CASCADE;
            """)

            # 1. Operators (varying clearance levels + 1 inactive account)
            await conn.executemany(
                """
                INSERT INTO operators (operator_id, name, clearance_level, is_active)
                VALUES ($1, $2, $3, $4)
                """,
                [
                    ("OP-101", "Rohan Verma", 1, True),
                    ("OP-202", "Meera Nair", 3, True),
                    ("OP-900", "Chief Viktor Vance", 5, True),
                    ("OP-404", "Karan Roy", 2, False),
                ],
            )

            # 2. Inventory
            await conn.executemany(
                """
                INSERT INTO inventory (
                    item_id, name, category, quantity_available, unit_cost, min_clearance, is_borrowable
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7)
                """,
                [
                    ("PART-01", "Neural Interface Cable", "Electronics", 15, 450.00, 1, True),
                    ("PART-02", "Titanium Chassis Frame", "Structural", 4, 1800.00, 2, True),
                    ("PART-03", "Quantum Optical Core", "Photonics", 0, 4500.00, 3, False),
                    ("PART-04", "Standard Optical Sensor", "Photonics", 8, 950.00, 1, True),
                    ("PART-05", "Bio-Synthetic Plasma Gel", "Bio-Chem", 2, 1200.00, 4, False),
                ],
            )

            # 3. Fabrication Bays
            await conn.executemany(
                """
                INSERT INTO fabrication_bays (bay_id, name, bay_type, status, min_clearance)
                VALUES ($1, $2, $3, $4, $5)
                """,
                [
                    ("BAY-A", "Alpha Assembly Bay", "General", "AVAILABLE", 1),
                    ("BAY-B", "Beta Photonics Cleanroom", "Photonics", "AVAILABLE", 3),
                    ("BAY-C", "Gamma Bio-Synthesis Chamber", "Bio-Chem", "MAINTENANCE", 4),
                ],
            )

            # 4. Pre-existing Bay Booking (2049-11-18 10:00 to 11:00 UTC — to test overlap conflicts)
            await conn.execute(
                """
                INSERT INTO bay_bookings (bay_id, operator_id, start_time, end_time, purpose, status)
                VALUES ($1, $2, $3, $4, $5, $6)
                """,
                "BAY-A",
                "OP-202",
                datetime(2049, 11, 18, 10, 0, tzinfo=timezone.utc),
                datetime(2049, 11, 18, 11, 0, tzinfo=timezone.utc),
                "Routine Actuator Calibration",
                "CONFIRMED",
            )

            # 5. Department Budgets
            await conn.executemany(
                """
                INSERT INTO budgets (department, total_allocated, spent)
                VALUES ($1, $2, $3)
                """,
                [
                    ("SYNTHETICS_CORE", 25000.00, 5000.00),
                    ("R_AND_D", 3000.00, 2200.00),
                ],
            )

            # 6. Initial Departmental Stock Ownership (owned = available + loaned)
            await conn.executemany(
                """
                INSERT INTO department_inventory (
                    department, item_id, quantity_owned, quantity_available, quantity_loaned
                )
                VALUES ($1, $2, $3, $4, $5)
                """,
                [
                    ("SYNTHETICS_CORE", "PART-01", 10, 10, 0),
                    ("R_AND_D", "PART-01", 5, 5, 0),
                    ("SYNTHETICS_CORE", "PART-02", 4, 4, 0),
                    ("R_AND_D", "PART-04", 8, 8, 0),
                    ("SYNTHETICS_CORE", "PART-05", 2, 2, 0),
                ],
            )

    await close_db_pool()
    print("Database seeded successfully.")


if __name__ == "__main__":
    asyncio.run(seed_database())
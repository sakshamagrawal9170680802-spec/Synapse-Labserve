import asyncpg
from database import get_db_pool
from rules import RULES
from tools.compliance import verify_operator
from datetime import datetime,timezone

async def borrow_return(operator_id: str,item_id: str,quantity: int,department: str,action: str = "BORROW",) -> dict:
    """Tool 5: Borrows or returns department-owned inventory items while enforcing rules."""
    if not isinstance(operator_id, str) or not operator_id.strip():
            return {
                "status": "REFUSED",
                "rule_violated": "INVALID_OPERATOR_ID",
                "reason": "A valid operator ID must be provided.",
            }
    
    if not isinstance(item_id, str) or not item_id.strip():
        return {
            "status": "REFUSED",
            "rule_violated": "INVALID_ITEM_ID",
            "reason": "A valid item ID must be provided.",
        }

    if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity <= 0:
        return {
            "status": "REFUSED",
            "rule_violated": "INVALID_QUANTITY",
            "reason": "Quantity must be a positive integer.",
        }

    if not isinstance(department, str) or not department.strip():
        return {
            "status": "REFUSED",
            "rule_violated": "INVALID_DEPARTMENT",
            "reason": "A valid department name must be provided.",
        }

    clean_action = action.strip().upper() if isinstance(action, str) else ""
    if clean_action not in ("BORROW", "RETURN"):
        return {
            "status": "REFUSED",
            "rule_violated": "INVALID_ACTION",
            "reason": "Action must be either 'BORROW' or 'RETURN'.",
        }

    # 1. Verify operator exists and is active
    op_check = await verify_operator(operator_id=operator_id, required_clearance=1, enforce_clearance=False)
    if op_check["status"] != "ALLOWED":
        return op_check

    operator = op_check["operator"]
    clean_item_id = item_id.strip().upper()
    clean_op_id = operator["operator_id"]
    clean_dept = department.strip().upper()

    pool = await get_db_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            # 2. Fetch and lock global inventory row first (consistent lock order prevents deadlocks)
            item = await conn.fetchrow(
                """
                SELECT item_id, name, quantity_available, min_clearance, is_borrowable
                FROM inventory
                WHERE item_id = $1
                FOR UPDATE
                """,
                clean_item_id,
            )
            if item is None:
                return {
                    "status": "REFUSED",
                    "rule_violated": "UNKNOWN_ITEM",
                    "reason": f"Item '{item_id}' does not exist in inventory.",
                }

            # 3. Enforce item clearance rule
            if RULES.enforce_item_clearance and operator["clearance_level"] < item["min_clearance"]:
                return {
                    "status": "REFUSED",
                    "rule_violated": "INSUFFICIENT_CLEARANCE",
                    "reason": (
                        f"Operator {operator['name']} has clearance level {operator['clearance_level']}, "
                        f"but level {item['min_clearance']} is required."
                    ),
                }

            # 4. Verify department exists
            dept_exists = await conn.fetchval(
                "SELECT 1 FROM budgets WHERE department = $1",
                clean_dept,
            )
            if not dept_exists:
                return {
                    "status": "REFUSED",
                    "rule_violated": "UNKNOWN_DEPARTMENT",
                    "reason": f"Department '{department}' does not exist.",
                }

            if clean_action == "BORROW":
                if not item["is_borrowable"]:
                    return {
                        "status": "REFUSED",
                        "rule_violated": "ITEM_NOT_BORROWABLE",
                        "reason": f"{item['name']} ({clean_item_id}) cannot be borrowed; it must be purchased.",
                    }

                if quantity > RULES.max_borrow_quantity:
                    return {
                        "status": "REFUSED",
                        "rule_violated": "BORROW_LIMIT_EXCEEDED",
                        "reason": (
                            f"Requested {quantity} units, but max borrow limit is "
                            f"{RULES.max_borrow_quantity} units per request."
                        ),
                    }

                # 5. Lock and check department_inventory availability
                dept_stock = await conn.fetchrow(
                    """
                    SELECT quantity_available
                    FROM department_inventory
                    WHERE department = $1 AND item_id = $2
                    FOR UPDATE
                    """,
                    clean_dept,
                    clean_item_id,
                )
                dept_available = dept_stock["quantity_available"] if dept_stock else 0
                if dept_available < quantity:
                    return {
                        "status": "REFUSED",
                        "rule_violated": "INSUFFICIENT_DEPARTMENT_STOCK",
                        "reason": (
                            f"Department {clean_dept} only has {dept_available} available unit(s) "
                            f"of {item['name']} ({clean_item_id}), requested {quantity}."
                        ),
                    }

                if item["quantity_available"] < quantity:
                    return {
                        "status": "REFUSED",
                        "rule_violated": "INSUFFICIENT_STOCK",
                        "reason": (
                            f"Only {item['quantity_available']} units of {item['name']} available "
                            f"in warehouse (requested {quantity})."
                        ),
                    }

                # 6. Shift departmental stock from available -> loaned, deduct global stock, and log borrow
                await conn.execute(
                    """
                    UPDATE department_inventory
                    SET quantity_available = quantity_available - $1,
                        quantity_loaned = quantity_loaned + $1,
                        updated_at = NOW()
                    WHERE department = $2 AND item_id = $3
                    """,
                    quantity,
                    clean_dept,
                    clean_item_id,
                )
                await conn.execute(
                    "UPDATE inventory SET quantity_available = quantity_available - $1 WHERE item_id = $2",
                    quantity,
                    clean_item_id,
                )
                record = await conn.fetchrow(
                    """
                    INSERT INTO borrow_records (
                        item_id, operator_id, department, quantity_borrowed, quantity_returned, status
                    )
                    VALUES ($1, $2, $3, $4, 0, 'BORROWED')
                    RETURNING record_id
                    """,
                    clean_item_id,
                    clean_op_id,
                    clean_dept,
                    quantity,
                )
                return {
                    "status": "SUCCESS",
                    "action": "BORROW",
                    "record_id": record["record_id"],
                    "item_id": clean_item_id,
                    "item_name": item["name"],
                    "department": clean_dept,
                    "quantity": quantity,
                    "reason": f"Borrowed {quantity}x {item['name']} from {clean_dept}.",
                }

            # RETURN flow: operator can only return unreturned units borrowed from THIS department
            active_borrows = await conn.fetch(
                """
                SELECT record_id, quantity_borrowed, quantity_returned
                FROM borrow_records
                WHERE item_id = $1
                  AND operator_id = $2
                  AND department = $3
                  AND status = 'BORROWED'
                ORDER BY created_at ASC, record_id ASC
                FOR UPDATE
                """,
                clean_item_id,
                clean_op_id,
                clean_dept,
            )
            total_unreturned = sum(row["quantity_borrowed"] - row["quantity_returned"] for row in active_borrows)
            if total_unreturned < quantity:
                return {
                    "status": "REFUSED",
                    "rule_violated": "INVALID_RETURN_QUANTITY",
                    "reason": (
                        f"Operator {clean_op_id} only has {total_unreturned} unreturned unit(s) of "
                        f"{clean_item_id} from {clean_dept} (attempted to return {quantity})."
                    ),
                }

            remaining_to_return = quantity
            for row in active_borrows:
                if remaining_to_return == 0:
                    break

                unreturned_in_row = row["quantity_borrowed"] - row["quantity_returned"]
                returned_from_row = min(unreturned_in_row, remaining_to_return)
                new_returned_total = row["quantity_returned"] + returned_from_row
                new_status = (
                    "RETURNED" if new_returned_total == row["quantity_borrowed"] else "BORROWED"
                )

                await conn.execute(
                    """
                    UPDATE borrow_records
                    SET quantity_returned = $1,
                        status = $2
                    WHERE record_id = $3
                    """,
                    new_returned_total,
                    new_status,
                    row["record_id"],
                )
                remaining_to_return -= returned_from_row

            # Shift departmental stock back from loaned -> available and restore global stock
            await conn.execute(
                """
                UPDATE department_inventory
                SET quantity_available = quantity_available + $1,
                    quantity_loaned = quantity_loaned - $1,
                    updated_at = NOW()
                WHERE department = $2 AND item_id = $3
                """,
                quantity,
                clean_dept,
                clean_item_id,
            )
            await conn.execute(
                "UPDATE inventory SET quantity_available = quantity_available + $1 WHERE item_id = $2",
                quantity,
                clean_item_id,
            )

    return {
        "status": "SUCCESS",
        "action": "RETURN",
        "item_id": clean_item_id,
        "item_name": item["name"],
        "department": clean_dept,
        "quantity": quantity,
        "reason": f"Returned {quantity}x {item['name']} to {clean_dept}.",
    }


def _parse_iso_timestamp(value: str) -> datetime | None:
    """Parses an ISO-8601 string into a timezone-aware datetime (defaults to UTC if naive)."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        dt = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


async def bay_booking(operator_id: str,bay_id: str,start_time: str,end_time: str,purpose: str = "Fabrication Run",action: str = "BOOK",) -> dict:
    """Tool 6: Books or cancels a fabrication bay window while preventing overlapping slots."""
    if not isinstance(operator_id, str) or not operator_id.strip():
            return {
                "status": "REFUSED",
                "rule_violated": "INVALID_OPERATOR_ID",
                "reason": "A valid operator ID must be provided.",
            }
    
    if not isinstance(bay_id, str) or not bay_id.strip():
        return {
            "status": "REFUSED",
            "rule_violated": "INVALID_BAY_ID",
            "reason": "A valid bay ID must be provided.",
        }

    start_dt = _parse_iso_timestamp(start_time)
    end_dt = _parse_iso_timestamp(end_time)
    if start_dt is None or end_dt is None:
        return {
            "status": "REFUSED",
            "rule_violated": "INVALID_TIMESTAMP",
            "reason": "start_time and end_time must be valid ISO-8601 timestamps (e.g., '2049-11-18T10:00:00Z').",
        }

    if end_dt <= start_dt:
        return {
            "status": "REFUSED",
            "rule_violated": "INVALID_TIME_RANGE",
            "reason": "end_time must be strictly after start_time.",
        }

    if not isinstance(purpose, str):
        return {
            "status": "REFUSED",
            "rule_violated": "INVALID_PURPOSE",
            "reason": "Purpose must be a string.",
        }

    clean_action = action.strip().upper() if isinstance(action, str) else ""
    if clean_action not in ("BOOK", "CANCEL"):
        return {
            "status": "REFUSED",
            "rule_violated": "INVALID_ACTION",
            "reason": "Action must be either 'BOOK' or 'CANCEL'.",
        }

    op_check = await verify_operator(operator_id=operator_id, required_clearance=1, enforce_clearance=False)
    if op_check["status"] != "ALLOWED":
        return op_check

    operator = op_check["operator"]
    clean_bay_id = bay_id.strip().upper()
    clean_op_id = operator["operator_id"]

    pool = await get_db_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            bay = await conn.fetchrow(
                """
                SELECT bay_id, name, bay_type, status, min_clearance
                FROM fabrication_bays
                WHERE bay_id = $1
                FOR UPDATE
                """,
                clean_bay_id,
            )
            if bay is None:
                return {
                    "status": "REFUSED",
                    "rule_violated": "UNKNOWN_BAY",
                    "reason": f"Fabrication bay '{bay_id}' does not exist.",
                }

            if RULES.enforce_bay_clearance and operator["clearance_level"] < bay["min_clearance"]:
                return {
                    "status": "REFUSED",
                    "rule_violated": "INSUFFICIENT_CLEARANCE",
                    "reason": (
                        f"Operator {operator['name']} has clearance level {operator['clearance_level']}, "
                        f"but level {bay['min_clearance']} is required."
                    ),
                }

            if clean_action == "CANCEL":
                updated = await conn.fetchrow(
                    """
                    UPDATE bay_bookings
                    SET status = 'CANCELLED'
                    WHERE bay_id = $1
                      AND start_time = $2
                      AND end_time = $3
                      AND operator_id = $4
                      AND status = 'CONFIRMED'
                    RETURNING booking_id
                    """,
                    clean_bay_id,
                    start_dt,
                    end_dt,
                    clean_op_id,
                )
                if updated is None:
                    return {
                        "status": "REFUSED",
                        "rule_violated": "BOOKING_NOT_FOUND",
                        "reason": (
                            f"No active booking found for {clean_op_id} in {clean_bay_id} "
                            f"from {start_dt.isoformat()} to {end_dt.isoformat()}."
                        ),
                    }
                return {
                    "status": "SUCCESS",
                    "action": "CANCEL",
                    "booking_id": updated["booking_id"],
                    "bay_id": clean_bay_id,
                    "start_time": start_dt.isoformat(),
                    "end_time": end_dt.isoformat(),
                    "reason": f"Cancelled booking #{updated['booking_id']} for {clean_bay_id}.",
                }

            # BOOK flow
            if bay["status"] == "UNAVAILABLE":
                return {
                    "status": "REFUSED",
                    "rule_violated": "BAY_UNAVAILABLE",
                    "reason": f"{bay['name']} ({clean_bay_id}) is currently marked UNAVAILABLE.",
                }

            if bay["status"] == "MAINTENANCE" and not RULES.allow_maintenance_bay_booking:
                return {
                    "status": "REFUSED",
                    "rule_violated": "BAY_IN_MAINTENANCE",
                    "reason": f"{bay['name']} ({clean_bay_id}) is under MAINTENANCE and cannot be booked.",
                }

            # Check overlapping bookings using PostgreSQL range overlap operator (&&)
            existing = await conn.fetchrow(
                """
                SELECT booking_id, operator_id, start_time, end_time, purpose
                FROM bay_bookings
                WHERE bay_id = $1
                  AND status = 'CONFIRMED'
                  AND tstzrange(start_time, end_time, '[)') && tstzrange($2, $3, '[)')
                ORDER BY start_time ASC
                LIMIT 1
                """,
                clean_bay_id,
                start_dt,
                end_dt,
            )
            if existing is not None:
                return {
                    "status": "REFUSED",
                    "rule_violated": "BAY_TIME_OVERLAP",
                    "reason": (
                        f"{bay['name']} ({clean_bay_id}) has an overlapping booking from "
                        f"{existing['start_time'].isoformat()} to {existing['end_time'].isoformat()} "
                        f"by {existing['operator_id']} for '{existing['purpose']}'."
                    ),
                    "existing_booking": {
                        "booking_id": existing["booking_id"],
                        "operator_id": existing["operator_id"],
                        "start_time": existing["start_time"].isoformat(),
                        "end_time": existing["end_time"].isoformat(),
                        "purpose": existing["purpose"],
                    },
                }

            try:
                booking = await conn.fetchrow(
                    """
                    INSERT INTO bay_bookings (bay_id, operator_id, start_time, end_time, purpose, status)
                    VALUES ($1, $2, $3, $4, $5, 'CONFIRMED')
                    RETURNING booking_id
                    """,
                    clean_bay_id,
                    clean_op_id,
                    start_dt,
                    end_dt,
                    purpose.strip() or "Fabrication Run",
                )
            except asyncpg.ExclusionViolationError:
                return {
                    "status": "REFUSED",
                    "rule_violated": "BAY_TIME_OVERLAP",
                    "reason": f"{clean_bay_id} was just booked for an overlapping time window.",
                }

    return {
        "status": "SUCCESS",
        "action": "BOOK",
        "booking_id": booking["booking_id"],
        "bay_id": clean_bay_id,
        "bay_name": bay["name"],
        "start_time": start_dt.isoformat(),
        "end_time": end_dt.isoformat(),
        "purpose": purpose.strip() or "Fabrication Run",
        "reason": (
            f"Confirmed booking #{booking['booking_id']} for {bay['name']} "
            f"({start_dt.isoformat()} to {end_dt.isoformat()})."
        ),
    }
from decimal import Decimal
from database import get_db_pool
from rules import RULES
from tools.compliance import verify_operator


async def inventory_search(query: str = "",category: str | None = None,only_available: bool = False,) -> dict:
    """Tool 3: Searches Foundry inventory by name, item_id, or category with departmental stock breakdown."""
    # Validate query
    if not isinstance(query, str):
        return {
            "status": "REFUSED",
            "rule_violated": "INVALID_QUERY",
            "reason": "Search query must be a string.",
        }

    # Validate category
    if category is not None and not isinstance(category, str):
        return {
            "status": "REFUSED",
            "rule_violated": "INVALID_CATEGORY",
            "reason": "Category must be a string or None.",
        }

    # Validate only_available
    if not isinstance(only_available, bool):
        return {
            "status": "REFUSED",
            "rule_violated": "INVALID_AVAILABILITY_FILTER",
            "reason": "only_available must be a boolean value.",
        }

    search_pattern = f"%{query.strip()}%"
    category_clean = category.strip() if category and category.strip() else None

    pool = await get_db_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            # LEFT JOIN ensures items with 0 stock (not yet in department_inventory) still appear in search results
            """
            SELECT
                i.item_id,
                i.name,
                i.category,
                i.quantity_available,
                i.unit_cost,
                i.min_clearance,
                i.is_borrowable,
                di.department,
                di.quantity_owned AS dept_owned,
                di.quantity_available AS dept_available,
                di.quantity_loaned AS dept_loaned
            FROM inventory i
            LEFT JOIN department_inventory di
                ON i.item_id = di.item_id
            WHERE ($1 = '%%' OR i.name ILIKE $1 OR i.item_id ILIKE $1)
              AND ($2::VARCHAR IS NULL OR i.category ILIKE $2)
              AND ($3::BOOLEAN = FALSE OR i.quantity_available > 0)
            ORDER BY i.item_id ASC, di.department ASC
            """,
            search_pattern,
            category_clean,
            only_available,
        )

    # Group multiple department rows under their parent item cleanly in Python
    items_by_id: dict[str, dict] = {}
    for row in rows:
        item_id = row["item_id"]
        if item_id not in items_by_id:
            items_by_id[item_id] = {
                "item_id": item_id,
                "name": row["name"],
                "category": row["category"],
                "quantity_available": row["quantity_available"],
                "unit_cost": float(row["unit_cost"]),
                "min_clearance": row["min_clearance"],
                "is_borrowable": row["is_borrowable"],
                "department_breakdown": [],
            }

        if row["department"] is not None:
            items_by_id[item_id]["department_breakdown"].append(
                {
                    "department": row["department"],
                    "quantity_owned": row["dept_owned"],
                    "quantity_available": row["dept_available"],
                    "quantity_loaned": row["dept_loaned"],
                }
            )

    items = list(items_by_id.values())
    return {
        "status": "SUCCESS",
        "count": len(items),
        "items": items,
    }


async def ordering(operator_id: str,item_id: str,quantity: int,department: str,chief_approved: bool = False,) -> dict:
    """Tool 4: Places a purchase order after enforcing clearance, budget, and Chief sign-off rules."""
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
            "reason": "Order quantity must be a positive integer.",
        }

    if not isinstance(department, str) or not department.strip():
        return {
            "status": "REFUSED",
            "rule_violated": "INVALID_DEPARTMENT",
            "reason": "A valid department name must be provided.",
        }

    if not isinstance(chief_approved, bool):
        return {
            "status": "REFUSED",
            "rule_violated": "INVALID_APPROVAL_FLAG",
            "reason": "chief_approved must be a boolean value.",
        }

    # 1. Verify operator is active first
    op_check = await verify_operator(operator_id=operator_id, required_clearance=1, enforce_clearance=False)
    if op_check["status"] != "ALLOWED":
        return op_check

    operator = op_check["operator"]
    clean_item_id = item_id.strip().upper()
    clean_dept = department.strip().upper()
    clean_op_id = operator["operator_id"]

    pool = await get_db_pool()
    async with pool.acquire() as conn:
        # Database transaction start karta hai. Transaction ka basic principle hai: Ya toh transaction ke andar ke saare database changes commit honge, ya failure hone par rollback honge.
        async with conn.transaction():
            # 2. Fetch and lock inventory item
            # FOR UPDATE ensure karta hai ki ek transaction ke lock hold karne ke dauran doosri transaction us row ko conflicting update nahi kar sakegi.
            item = await conn.fetchrow(
                """
                SELECT item_id, name, unit_cost, min_clearance
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

            # 3. Enforce item clearance rule using already-fetched operator data (avoids extra DB query under lock)
            if RULES.enforce_item_clearance and operator["clearance_level"] < item["min_clearance"]:
                return {
                    "status": "REFUSED",
                    "rule_violated": "INSUFFICIENT_CLEARANCE",
                    "reason": (
                        f"Operator {operator['name']} has clearance level {operator['clearance_level']}, "
                        f"but level {item['min_clearance']} is required."
                    ),
                }

            total_cost = Decimal(str(item["unit_cost"])) * quantity

            # 4. Fetch and lock department budget
            budget = await conn.fetchrow(
                """
                SELECT department, total_allocated, spent
                FROM budgets
                WHERE department = $1
                FOR UPDATE
                """,
                clean_dept,
            )
            if budget is None:
                return {
                    "status": "REFUSED",
                    "rule_violated": "UNKNOWN_DEPARTMENT",
                    "reason": f"Department '{department}' does not exist.",
                }

            remaining = Decimal(str(budget["total_allocated"])) - Decimal(str(budget["spent"]))
            if total_cost > remaining:
                return {
                    "status": "REFUSED",
                    "rule_violated": "INSUFFICIENT_BUDGET",
                    "reason": (
                        f"Department {clean_dept} has ₹{remaining:.2f} remaining, "
                        f"which cannot cover ₹{total_cost:.2f}."
                    ),
                }

            # 5. Enforce Chief's Sign-Off threshold (> ₹2,000 by default)
            threshold = Decimal(str(RULES.chief_approval_threshold))
            if total_cost > threshold and not chief_approved:
                order_row = await conn.fetchrow(
                    """
                    INSERT INTO purchase_orders (item_id, operator_id, department, quantity, total_cost, status)
                    VALUES ($1, $2, $3, $4, $5, 'AWAITING_CHIEF_APPROVAL')
                    RETURNING order_id
                    """,
                    clean_item_id,
                    clean_op_id,
                    clean_dept,
                    quantity,
                    total_cost,
                )
                return {
                    "status": "REQUIRES_CHIEF_APPROVAL",
                    "order_id": order_row["order_id"],
                    "item_id": clean_item_id,
                    "item_name": item["name"],
                    "quantity": quantity,
                    "total_cost": float(total_cost),
                    "department": clean_dept,
                    "reason": (
                        f"Order total ₹{total_cost:.2f} exceeds ₹{threshold:.2f}. "
                        "Paused awaiting Chief sign-off."
                    ),
                }

            # 6. Commit funds, restock global & departmental inventory, and record approved order
            await conn.execute(
                "UPDATE budgets SET spent = spent + $1 WHERE department = $2",
                total_cost,
                clean_dept,
            )
            await conn.execute(
                "UPDATE inventory SET quantity_available = quantity_available + $1 WHERE item_id = $2",
                quantity,
                clean_item_id,
            )
            order_row = await conn.fetchrow(
                """
                INSERT INTO purchase_orders (item_id, operator_id, department, quantity, total_cost, status)
                VALUES ($1, $2, $3, $4, $5, 'APPROVED')
                RETURNING order_id
                """,
                clean_item_id,
                clean_op_id,
                clean_dept,
                quantity,
                total_cost,
            )
            await conn.execute(
                """
                INSERT INTO department_inventory (
                    department, item_id, quantity_owned, quantity_available, quantity_loaned, updated_at
                )
                VALUES ($1, $2, $3, $3, 0, NOW())
                ON CONFLICT (department, item_id)
                DO UPDATE SET
                    quantity_owned = department_inventory.quantity_owned + EXCLUDED.quantity_owned,
                    quantity_available = department_inventory.quantity_available + EXCLUDED.quantity_available,
                    updated_at = NOW()
                """,
                clean_dept,
                clean_item_id,
                quantity,
            )

    return {
        "status": "APPROVED",
        "order_id": order_row["order_id"],
        "item_id": clean_item_id,
        "item_name": item["name"],
        "quantity": quantity,
        "total_cost": float(total_cost),
        "department": clean_dept,
        "reason": "Purchase order approved and inventory restocked.",
    }
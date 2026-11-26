#Python mein financial calculations ke liye Decimal use karte hain.
#0.1 + 0.2 Normal floating-point arithmetic mein result kabhi-kabhi exactly 0.3 nahi hota.
#Decimal("0.1") + Decimal("0.2") =Decimal("0.3") Financial calculations mein precision important hai, isliye hum Decimal use kar rahe hain.
from decimal import Decimal,InvalidOperation
from database import get_db_pool
from rules import RULES


async def verify_operator(operator_id: str,required_clearance: int = 1,enforce_clearance: bool = True,) -> dict:
    """Internal guard used by all tools to verify operator existence, active status, and clearance."""
    if not isinstance(operator_id, str) or not operator_id.strip():
        return {
            "status": "REFUSED",
            "rule_violated": "INVALID_OPERATOR_ID",
            "reason": "A valid operator ID must be provided.",
        }

    if (isinstance(required_clearance, bool) or not isinstance(required_clearance, int) or not 1 <= required_clearance <= 5):
        return {
            "status": "REFUSED",
            "rule_violated": "INVALID_CLEARANCE_LEVEL",
            "reason": "Required clearance must be an integer between 1 and 5.",
        }

    if not isinstance(enforce_clearance, bool):
        return {
            "status": "REFUSED",
            "rule_violated": "INVALID_CLEARANCE_POLICY",
            "reason": "enforce_clearance must be a boolean.",
        }
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        #fetchrow() Ye query execute karke first matching row return karta hai.Agar operator nahi mila, toh None return hoga.
        row = await conn.fetchrow(
            """
            SELECT operator_id, name, clearance_level, is_active
            FROM operators
            WHERE operator_id = $1
            """,
            operator_id.strip().upper(),
        )

    if row is None:
        return {
            "status": "REFUSED",
            "rule_violated": "UNKNOWN_OPERATOR",
            "reason": f"Operator '{operator_id}' does not exist in Foundry records.",
        }

    if RULES.require_active_operator and not row["is_active"]:
        return {
            "status": "REFUSED",
            "rule_violated": "INACTIVE_OPERATOR",
            "reason": f"Operator {row['name']} ({row['operator_id']}) is inactive/suspended.",
        }

    if enforce_clearance and row["clearance_level"] < required_clearance:
        return {
            "status": "REFUSED",
            "rule_violated": "INSUFFICIENT_CLEARANCE",
            "reason": (
                f"Operator {row['name']} has clearance level {row['clearance_level']}, "
                f"but level {required_clearance} is required."
            ),
        }

    return {
        "status": "ALLOWED",
        "operator": {
            "operator_id": row["operator_id"],
            "name": row["name"],
            "clearance_level": row["clearance_level"],
            "is_active": row["is_active"],
        },
    }


async def operator_clearance_check(operator_id: str, required_clearance: int = 1) -> dict:
    """Tool 1: Checks whether an operator is active and meets a required clearance level."""
    return await verify_operator(
        operator_id=operator_id,
        required_clearance=required_clearance,
        enforce_clearance=True,
    )

# Tool interface aur internal helper ko separate rakhna.
# - verify_operator() internal reusable function hai.
# - operator_clearance_check() MCP ke through expose hone wala tool hai.
#jaise database springboot mein repository aur service ko alag rakhte hai repository mein humara SQL se related code hota hai aur service mein uska buisness logic

async def budget_check(department: str, estimated_cost: float) -> dict:
    """Tool 2: Checks department budget availability and flags orders exceeding the Chief threshold."""
    if not isinstance(department, str) or not department.strip():
        return {
            "status": "REFUSED",
            "rule_violated": "INVALID_DEPARTMENT",
            "reason": "A valid department name must be provided.",
        }
    
    try:
        cost_decimal = Decimal(str(estimated_cost))
    except (InvalidOperation, ValueError, TypeError):
        return {
            "status": "REFUSED",
            "rule_violated": "INVALID_AMOUNT",
            "reason": "Estimated cost must be a valid positive number.",
        }

    if not cost_decimal.is_finite() or cost_decimal <= 0:
        return {
            "status": "REFUSED",
            "rule_violated": "INVALID_AMOUNT",
            "reason": "Estimated cost must be a finite amount greater than ₹0.",
        }

    pool = await get_db_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT department, total_allocated, spent
            FROM budgets
            WHERE department = $1
            """,
            department.strip().upper(),
        )

    if row is None:
        return {
            "status": "REFUSED",
            "rule_violated": "UNKNOWN_DEPARTMENT",
            "reason": f"Department '{department}' does not exist.",
        }

    total_allocated = Decimal(str(row["total_allocated"]))
    spent = Decimal(str(row["spent"]))
    remaining = total_allocated - spent

    if cost_decimal > remaining:
        return {
            "status": "REFUSED",
            "rule_violated": "INSUFFICIENT_BUDGET",
            "reason": (
                f"Department {row['department']} only has ₹{float(remaining):.2f} remaining, "
                f"which cannot cover ₹{float(cost_decimal):.2f}."
            ),
            "remaining_budget": float(remaining),
        }

    threshold = Decimal(str(RULES.chief_approval_threshold))
    requires_chief = cost_decimal > threshold

    return {
        "status": "REQUIRES_CHIEF_APPROVAL" if requires_chief else "APPROVED",
        "department": row["department"],
        "estimated_cost": float(cost_decimal),
        "remaining_budget": float(remaining),
        "chief_approval_required": requires_chief,
        "reason": (
            f"Cost ₹{float(cost_decimal):.2f} exceeds ₹{float(threshold):.2f}; Chief sign-off required."
            if requires_chief
            else "Within department budget and standard approval limits."
        ),
    }
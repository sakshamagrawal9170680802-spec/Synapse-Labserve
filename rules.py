from pydantic import BaseModel, ConfigDict, Field, StrictBool


class RulebookConfig(BaseModel):
    # validate_assignment=True ensures Pydantic re-validates even when fields
    # are modified AFTER the object is created (crucial for live rule updates).
    model_config = ConfigDict(validate_assignment=True)

    chief_approval_threshold: float = Field(default=2000.00, gt=0)
    max_borrow_quantity: int = Field(default=5, ge=1)
    require_active_operator: StrictBool = True
    allow_maintenance_bay_booking: StrictBool = False
    enforce_item_clearance: StrictBool = True
    enforce_bay_clearance: StrictBool = True


# Single live instance used across all tools and the MCP rulebook resource
RULES = RulebookConfig()

#here **kwargs shows multiple key:value pair can be accepted by this function
def update_rules(**changes) -> RulebookConfig:
    """Safely applies live rule updates (Directive Drop), raising ValidationError on bad input."""
    for key, value in changes.items():
        #hasattr(a,b) checks whether key b is present in a
        if not hasattr(RULES, key):
            raise ValueError(f"Unknown rule '{key}'.")
        #setattr(a,b,c) sets a.b=c
        setattr(RULES, key, value)
    return RULES


def get_rulebook_text() -> str:
    """Builds the official rulebook dynamically from current RULES values."""
    operator_status_rule = (
        "Only active operators (is_active = True) may request parts, book bays, or place orders."
        if RULES.require_active_operator
        else "Operator active-status enforcement is disabled."
    )

    item_clearance_rule = (
        "An operator's clearance_level must be greater than or equal to the min_clearance required by any part."
        if RULES.enforce_item_clearance
        else "Item clearance enforcement is disabled."
    )

    bay_clearance_rule = (
        "An operator's clearance_level must be greater than or equal to the min_clearance required by any fabrication bay."
        if RULES.enforce_bay_clearance
        else "Bay clearance enforcement is disabled."
    )

    maintenance_bay_rule = (
        "Bays marked 'MAINTENANCE' or 'UNAVAILABLE' cannot be booked."
        if not RULES.allow_maintenance_bay_booking
        else "Maintenance bay booking is enabled, subject to other booking constraints. Bays marked 'UNAVAILABLE' cannot be booked."
    )

    return f"""
LABSERVE OPERATIONAL RULEBOOK (LAW-ENFORCED)
===========================================
1. Operator Status: {operator_status_rule}
2. Item Clearance: {item_clearance_rule}
3. Bay Clearance: {bay_clearance_rule}
4. Chief's Sign-Off: Any purchase order exceeding ₹{RULES.chief_approval_threshold:.2f} requires explicit Chief approval before funds are deducted or stock is ordered.
5. Budget Ceiling: Orders cannot exceed the requesting department's remaining allocated budget.
6. Borrow Limits: Operators may borrow at most {RULES.max_borrow_quantity} units of a borrowable item per request. Non-borrowable items must be purchased.
7. Bay Availability: {maintenance_bay_rule} Double-booking the same bay and time slot is strictly prohibited.
""".strip()
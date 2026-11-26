from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
import logging
import sys

from mcp.server.fastmcp import FastMCP

from database import close_db_pool, create_tables, get_db_pool
from rules import get_rulebook_text
from tools.bookings import bay_booking, borrow_return
from tools.compliance import budget_check, operator_clearance_check
from tools.inventory import inventory_search, ordering

# stdout is strictly reserved for MCP JSON-RPC messages; always route logs to stderr.
#logging.basicConfig() global/default logging configuration set karta hai
#yahan hum bata rahe hai ki logging.info stdout nahi balki stderr ki tarah treat hoga 
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("labserve.mcp")

#asynchronous generator function converted to asyncronous context manager with help of decorator asynccontextmanager
#@asynccontextmanager
# async def my_resource():
#     print("Resource STARTED")

#     try:
#         yield
#     finally:
#         print("Resource CLOSED")


# async with my_resource():
#     print("Doing some work")

# Output:
# Resource STARTED
# Doing some work
# Resource CLOSED


#-> AsyncIterator[None]
# Ab ye important hai.
# Iska matlab function asynchronous iteration/generator style mein None type ki value produce karta hai.
#Yahan None kyun?
#Kyunki tumhara yield koi value return nahi kar raha:

#finally isliye maan lo is function ke saath use hue with mein koi error aa jayr tabhi finally ensure karega ki woh run ho aur pool close kare
@asynccontextmanager
async def server_lifespan(server: FastMCP) -> AsyncIterator[None]:
    """Initializes PostgreSQL pool and schema on MCP startup, and closes pool on shutdown."""
    logger.info("Starting LabServe MCP Server and initializing database pool...")
    
    try:
        await get_db_pool()
        await create_tables()
        yield
    finally:
        logger.info("Shutting down LabServe MCP Server and closing database pool...")
        await close_db_pool()


mcp = FastMCP(
    #Server's name
    name="LabServe Foundry MCP",
    #mere server ke lifespan srver_lifespan function se link karo matlab pehle woh run hoga yield tak pahuchenge fir mcp server banega aur fir pehle mcp server close hoga fir server_lifespan ka finally run hoga
    lifespan=server_lifespan,
)

# Register the 6 law-enforced tools directly from the tools/ layer
mcp.add_tool(inventory_search)
mcp.add_tool(operator_clearance_check)
mcp.add_tool(borrow_return)
mcp.add_tool(bay_booking)
mcp.add_tool(ordering)
mcp.add_tool(budget_check)


# yeh mcp ko resource provide kar raha hai 
@mcp.resource(
    # resource ko uniquely identify karne ka url
    "labserve://rulebook",
    name="Foundry Operational Rulebook",
    description="Official law-enforced rules governing operator clearance, budgets, borrowing, and bay bookings.",
    mime_type="text/plain",
)
def read_rulebook() -> str:
    """Exposes the live operational rulebook as an MCP system resource."""
    return get_rulebook_text()


if __name__ == "__main__":
    mcp.run(transport="stdio")


#uses of yield


#1. pauses the function when used
# def get_numbers():
#     print("Starting")

#     yield 10

#     print("Getting next number")

#     yield 20

#     print("Finished")



# numbers = get_numbers()

# print(next(numbers))
# print(next(numbers))


# Output:


# Starting
# 10
# Getting next number
# 20

#2. used in generator to define values
#basically tum ek function banate ho jo 
# kuch aisa hota hai
# async def get_items():
#   yield "Cable"
#    yield "Sensor"
#    yield "Chassis" 
#isse asyn generator kahte hai aur isse hi async iterator ki tarah bhi use kar sakte hai
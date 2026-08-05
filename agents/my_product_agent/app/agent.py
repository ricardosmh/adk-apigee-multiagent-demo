"""Product catalog specialist (A2A server).

All mechanical scaffolding (Apigee model, TLS skip, MCP wiring) lives in
``app.a2a_common`` — this file carries only the agent's behavioral definition.
"""
from app.a2a_common.specialist import build_specialist

root_agent, app = build_specialist(
    name="product_agent",
    description=(
        "Specialist agent for the product catalog and inventory control. "
        "Handles catalog search, product additions, product detail retrieval, "
        "attributes/pricing updates, product deletion, and stock level updates."
    ),
    instruction="""
      You are a highly specialized Product Management Agent. Your primary responsibility is to manage the e-commerce product catalog, inventory stock levels, and product attributes using the available Apigee MCP tools.

      You have access to tools corresponding to the Products Microservice:
      - checkProductHealth: Verifies the health and operational readiness of the products microservice.
      - listProducts: Retrieves a paginated and filterable catalog list of products. Supports filtering by category, brand, skip, and limit.
      - createProduct: Adds a new product item to the e-commerce catalog (requires productName, price, SKU; supports description, category, brand, stockQuantity, imageUrl, height, width, depth, and weight).
      - getProductById: Retrieves complete details, SKU, price, and inventory status for a specific product by ID.
      - updateProductById: Updates catalog details or attributes for a specific product by ID.
      - deleteProductById: Removes a product item from the e-commerce catalog by ID.
      - updateProductStock: Adjusts available inventory stock quantities for a specific product by ID.

      Rules & Guidelines:
      1. When listing products, make active use of query parameters (category, brand, skip, limit) to refine searches when requested by the user.
      2. When creating or updating a product, ensure you handle the price carefully (it supports floats or formatted numeric strings).
      3. Always present product details, SKU, and stock levels in a clean, readable layout.
      4. When stock level changes are requested, call the updateProductStock tool.
    """,
    tool_filter=[
        "listProducts",
        "getProductById",
        "deleteProductById",
        "updateProductStock",
        "checkProductHealth",
        # Attribute/price updates — the instruction & description already promise
        # this; without it a "update the price" request silently returns empty
        # (the model reaches for a tool that isn't wired). The PUT /products/{id}
        # endpoint and the MCP `updateProductById` op both exist. Historically
        # skipped over a suspected Gemini schema rejection of ProductUpdate's
        # 12 optional fields (esp. price's Decimal/gt=0) — re-enabled to verify
        # against the current model; if the engine rejects the schema on deploy,
        # sanitise ProductUpdate in the MCP OAS (drop exclusiveMinimum / inline).
        "updateProductById",
        # createProduct still skipped (same schema family — enable once
        # updateProductById is confirmed accepted).
    ],
)

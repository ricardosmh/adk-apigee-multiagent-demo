# Orders Service (`orders-service`)

## Purpose
The Orders Service is a robust microservice built with FastAPI and SQLAlchemy. It manages customer orders, line items, total amount calculation, order statuses, and shipment tracking numbers for the e-commerce platform.

## Local Development & Running

### Prerequisites
- Python 3.11+
- MySQL Server (running locally or via Docker)

### Setup Instructions
1. Navigate to the service directory:
   ```bash
   cd orders-service
   ```
2. Create and activate a virtual environment:
   ```bash
   python -m venv venv
   source venv/bin/activate  # On Windows use `venv\Scripts\activate`
   ```
3. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```
4. Configure Environment Variables (create a `.env` file):
   ```env
   DB_USER=root
   DB_PASSWORD=yourpassword
   DB_HOST=localhost
   DB_PORT=3306
   DB_NAME=database
   # Optional: PRODUCTS_SERVICE_URL=https://products-xxxxx-uc.a.run.app
   ```
5. Run the application locally:
   ```bash
   uvicorn app.main:app --reload --port 8082
   ```
6. Access the interactive API documentation (OpenAPI/Swagger UI) at:
   - http://localhost:8082/docs
   - OpenAPI Spec: http://localhost:8082/openapi.json

## Deployment

Deployed to Cloud Run **exclusively** by the manifest-driven tool — IAM-only
(no unauthenticated access; Apigee's invoker SA is the only caller), custom
audience, Direct VPC egress to the PSC-only Cloud SQL instance, IAM database
auth (the DB user is the runtime SA):

```bash
python services/provision/deploy_services.py --check
python services/provision/deploy_services.py orders
```

Everything the deploy needs (project, network, env, audience) comes from
[`../services-manifest.yaml`](../services-manifest.yaml) — see
[`../README.md`](../README.md) and
[`../provision/README.md`](../provision/README.md).

## API Endpoints Summary

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/orders` | Create a new order. Calculates `totalAmount` automatically based on product prices. |
| `GET` | `/orders` | Retrieve a list of all orders (supports pagination). |
| `GET` | `/orders/{id}` | Retrieve a single order and all its line items. |
| `GET` | `/customers/{id}/orders` | Retrieve all orders placed by a specific customer. |
| `PUT` | `/orders/{id}/status`| Update the status (e.g., `shipped`, `delivered`) and tracking number of an order. |
| `GET` | `/openapi.json` | OpenAPI specification generated automatically by FastAPI. |

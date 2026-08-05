# Products Service (`products-service`)

## Purpose
The Products Service is a robust microservice built with FastAPI and SQLAlchemy. It manages the product catalog, inventory stock quantities, pricing, and specifications for the e-commerce platform.

## Local Development & Running

### Prerequisites
- Python 3.11+
- MySQL Server (running locally or via Docker)

### Setup Instructions
1. Navigate to the service directory:
   ```bash
   cd products-service
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
   ```
5. Run the application locally:
   ```bash
   uvicorn app.main:app --reload --port 8081
   ```
6. Access the interactive API documentation (OpenAPI/Swagger UI) at:
   - http://localhost:8081/docs
   - OpenAPI Spec: http://localhost:8081/openapi.json

## Deployment

Deployed to Cloud Run **exclusively** by the manifest-driven tool — IAM-only
(no unauthenticated access; Apigee's invoker SA is the only caller), custom
audience, Direct VPC egress to the PSC-only Cloud SQL instance, IAM database
auth (the DB user is the runtime SA):

```bash
python services/provision/deploy_services.py --check
python services/provision/deploy_services.py products
```

Everything the deploy needs (project, network, env, audience) comes from
[`../services-manifest.yaml`](../services-manifest.yaml) — see
[`../README.md`](../README.md) and
[`../provision/README.md`](../provision/README.md).

## API Endpoints Summary

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/products` | Create a new product in the catalog. |
| `GET` | `/products` | Retrieve a list of all products (supports filtering by `category` or `brand`, and pagination). |
| `GET` | `/products/{id}` | Retrieve a single product by its ID. |
| `PUT` | `/products/{id}` | Update an existing product's details. |
| `PATCH`| `/products/{id}/stock` | Update only the `stockQuantity` of a product. |
| `DELETE`| `/products/{id}` | Delete a product from the catalog. |
| `GET` | `/openapi.json` | OpenAPI specification generated automatically by FastAPI. |

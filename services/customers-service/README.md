# Customers Service (`customers-service`)

## Purpose
The Customers Service is a lightweight, production-ready microservice built with FastAPI and SQLAlchemy. It is responsible for managing customer profiles, billing/shipping addresses, and account statuses for the e-commerce platform.

## Local Development & Running

### Prerequisites
- Python 3.11+
- MySQL Server (running locally or via Docker)

### Setup Instructions
1. Navigate to the service directory:
   ```bash
   cd customers-service
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
   uvicorn app.main:app --reload --port 8080
   ```
6. Access the interactive API documentation (OpenAPI/Swagger UI) at:
   - http://localhost:8080/docs
   - OpenAPI Spec: http://localhost:8080/openapi.json

## Deployment

Deployed to Cloud Run **exclusively** by the manifest-driven tool — IAM-only
(no unauthenticated access; Apigee's invoker SA is the only caller), custom
audience, Direct VPC egress to the PSC-only Cloud SQL instance, IAM database
auth (the DB user is the runtime SA):

```bash
python services/provision/deploy_services.py --check
python services/provision/deploy_services.py customers
```

Everything the deploy needs (project, network, env, audience) comes from
[`../services-manifest.yaml`](../services-manifest.yaml) — see
[`../README.md`](../README.md) and
[`../provision/README.md`](../provision/README.md).

## API Endpoints Summary

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/customers` | Create a new customer profile. |
| `GET` | `/customers` | Retrieve a list of all customers (supports pagination). |
| `GET` | `/customers/{id}` | Retrieve a single customer by their ID. |
| `PUT` | `/customers/{id}` | Update an existing customer's details. |
| `DELETE`| `/customers/{id}` | Delete a customer profile. |
| `GET` | `/openapi.json` | OpenAPI specification generated automatically by FastAPI. |

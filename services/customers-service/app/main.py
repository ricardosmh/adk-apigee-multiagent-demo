from contextlib import asynccontextmanager
from datetime import date, datetime
from typing import List
from fastapi import FastAPI, Depends, HTTPException, status
from sqlalchemy.orm import Session
import threading

from app.database import engine, get_db, Base
from app.models import Customer
from app.schemas import CustomerCreate, CustomerUpdate, CustomerResponse

@asynccontextmanager
async def lifespan(app: FastAPI):
    def init_db():
        try:
            Base.metadata.create_all(bind=engine)
            print("Database tables verified/created successfully.")
        except Exception as e:
            print(f"Warning: Could not verify/create database tables: {e}")

    threading.Thread(target=init_db, daemon=True).start()
    yield

app = FastAPI(
    title="Customers Service",
    description="Microservice for managing e-commerce customers.",
    version="1.0.0",
    lifespan=lifespan,
    root_path="/customer-management",
    servers=[{"url": "/customer-management", "description": "Gateway base path"}]
)

from app.ailog import install_ai_logging
install_ai_logging(app)

@app.get("/health", status_code=status.HTTP_200_OK, tags=["Health"])
def health_check():
    return {"status": "ok", "service": "customers-service"}

@app.post("/customers", response_model=CustomerResponse, status_code=status.HTTP_201_CREATED, tags=["Customers"])
def create_customer(customer: CustomerCreate, db: Session = Depends(get_db)):
    existing_customer = db.query(Customer).filter(
        (Customer.email == customer.email) | (Customer.username == customer.username)
    ).first()
    if existing_customer:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Customer with this email or username already exists."
        )
    
    db_customer = Customer(
        **customer.model_dump(),
        creationDate=date.today(),
        lastLogin=datetime.now()
    )
    db.add(db_customer)
    db.commit()
    db.refresh(db_customer)
    return db_customer

@app.get("/customers", response_model=List[CustomerResponse], tags=["Customers"])
def get_customers(skip: int = 0, limit: int = 100, db: Session = Depends(get_db)):
    customers = db.query(Customer).offset(skip).limit(limit).all()
    return customers

@app.get("/customers/{customer_id}", response_model=CustomerResponse, tags=["Customers"])
def get_customer(customer_id: int, db: Session = Depends(get_db)):
    customer = db.query(Customer).filter(Customer.customerId == customer_id).first()
    if not customer:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Customer with ID {customer_id} not found."
        )
    return customer

@app.put("/customers/{customer_id}", response_model=CustomerResponse, tags=["Customers"])
def update_customer(customer_id: int, customer_update: CustomerUpdate, db: Session = Depends(get_db)):
    db_customer = db.query(Customer).filter(Customer.customerId == customer_id).first()
    if not db_customer:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Customer with ID {customer_id} not found."
        )
    
    update_data = customer_update.model_dump(exclude_unset=True)
    for key, value in update_data.items():
        setattr(db_customer, key, value)
        
    db.commit()
    db.refresh(db_customer)
    return db_customer

@app.delete("/customers/{customer_id}", status_code=status.HTTP_200_OK, tags=["Customers"])
def delete_customer(customer_id: int, db: Session = Depends(get_db)):
    db_customer = db.query(Customer).filter(Customer.customerId == customer_id).first()
    if not db_customer:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Customer with ID {customer_id} not found."
        )
    db.delete(db_customer)
    db.commit()
    return {"detail": f"Customer {customer_id} successfully deleted."}

from contextlib import asynccontextmanager
from datetime import date
from decimal import Decimal
from typing import List
from fastapi import FastAPI, Depends, HTTPException, status
from sqlalchemy.orm import Session
import httpx
import threading

from app.config import settings
from app.database import engine, get_db, Base
from app.models import Order, OrderItem, Product
from app.schemas import OrderCreate, OrderStatusUpdate, OrderResponse

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
    title="Orders Service",
    description="Microservice for managing e-commerce customer orders and line items.",
    version="1.0.0",
    lifespan=lifespan,
    root_path="/order-management",
    servers=[{"url": "/order-management", "description": "Gateway base path"}]
)

from app.ailog import install_ai_logging
install_ai_logging(app)

@app.get("/health", status_code=status.HTTP_200_OK, tags=["Health"])
def health_check():
    return {"status": "ok", "service": "orders-service"}

@app.post("/orders", response_model=OrderResponse, status_code=status.HTTP_201_CREATED, tags=["Orders"])
def create_order(order: OrderCreate, db: Session = Depends(get_db)):
    if not order.items:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Order must contain at least one item."
        )

    total_amount = Decimal("0.00")
    order_items = []

    for item in order.items:
        price = None
        if settings.PRODUCTS_SERVICE_URL:
            try:
                base_url = settings.PRODUCTS_SERVICE_URL.rstrip("/")
                response = httpx.get(f"{base_url}/product-management/products/{item.productId}", timeout=5.0)
                if response.status_code == 200:
                    product_data = response.json()
                    price = Decimal(str(product_data["price"]))
                elif response.status_code == 404:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail=f"Product ID {item.productId} not found in products service."
                    )
            except httpx.RequestError:
                pass
                
        if price is None:
            db_product = db.query(Product).filter(Product.productId == item.productId).first()
            if not db_product:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Product ID {item.productId} not found in database."
                )
            price = db_product.price
            
        item_total = price * item.quantity
        total_amount += item_total
        
        order_items.append(OrderItem(
            productId=item.productId,
            quantity=item.quantity,
            price=price
        ))

    db_order = Order(
        customerId=order.customerId,
        orderDate=date.today(),
        totalAmount=total_amount,
        orderStatus="pending"
    )
    
    db.add(db_order)
    db.commit()
    db.refresh(db_order)

    for oi in order_items:
        oi.orderId = db_order.orderId
        db.add(oi)
        
    db.commit()
    db.refresh(db_order)
    return db_order

@app.get("/orders", response_model=List[OrderResponse], tags=["Orders"])
def get_orders(skip: int = 0, limit: int = 100, db: Session = Depends(get_db)):
    orders = db.query(Order).offset(skip).limit(limit).all()
    return orders

@app.get("/orders/{order_id}", response_model=OrderResponse, tags=["Orders"])
def get_order(order_id: int, db: Session = Depends(get_db)):
    order = db.query(Order).filter(Order.orderId == order_id).first()
    if not order:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Order with ID {order_id} not found."
        )
    return order

@app.get("/customers/{customer_id}/orders", response_model=List[OrderResponse], tags=["Orders"])
def get_customer_orders(customer_id: int, db: Session = Depends(get_db)):
    orders = db.query(Order).filter(Order.customerId == customer_id).all()
    return orders

@app.put("/orders/{order_id}/status", response_model=OrderResponse, tags=["Orders"])
def update_order_status(order_id: int, status_update: OrderStatusUpdate, db: Session = Depends(get_db)):
    db_order = db.query(Order).filter(Order.orderId == order_id).first()
    if not db_order:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Order with ID {order_id} not found."
        )
    
    db_order.orderStatus = status_update.orderStatus
    if status_update.trackingNumber is not None:
        db_order.trackingNumber = status_update.trackingNumber
        
    db.commit()
    db.refresh(db_order)
    return db_order

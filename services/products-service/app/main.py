from contextlib import asynccontextmanager
from typing import List, Optional
from fastapi import FastAPI, Depends, HTTPException, status, Query
from sqlalchemy.orm import Session
import threading

from app.database import engine, get_db, Base
from app.models import Product
from app.schemas import ProductCreate, ProductUpdate, ProductStockUpdate, ProductResponse

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
    title="Products Service",
    description="Microservice for managing e-commerce product catalog and inventory.",
    version="1.0.0",
    lifespan=lifespan,
    root_path="/product-management",
    servers=[{"url": "/product-management", "description": "Gateway base path"}]
)

from app.ailog import install_ai_logging
install_ai_logging(app)

@app.get("/health", status_code=status.HTTP_200_OK, tags=["Health"])
def health_check():
    return {"status": "ok", "service": "products-service"}

@app.post("/products", response_model=ProductResponse, status_code=status.HTTP_201_CREATED, tags=["Products"])
def create_product(product: ProductCreate, db: Session = Depends(get_db)):
    existing_product = db.query(Product).filter(Product.SKU == product.SKU).first()
    if existing_product:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Product with SKU {product.SKU} already exists."
        )
    
    db_product = Product(**product.model_dump())
    db.add(db_product)
    db.commit()
    db.refresh(db_product)
    return db_product

@app.get("/products", response_model=List[ProductResponse], tags=["Products"])
def get_products(
    category: Optional[str] = Query(None, description="Filter by category"),
    brand: Optional[str] = Query(None, description="Filter by brand"),
    skip: int = 0,
    limit: int = 100,
    db: Session = Depends(get_db)
):
    query = db.query(Product)
    if category:
        query = query.filter(Product.category == category)
    if brand:
        query = query.filter(Product.brand == brand)
        
    products = query.offset(skip).limit(limit).all()
    return products

@app.get("/products/{product_id}", response_model=ProductResponse, tags=["Products"])
def get_product(product_id: int, db: Session = Depends(get_db)):
    product = db.query(Product).filter(Product.productId == product_id).first()
    if not product:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Product with ID {product_id} not found."
        )
    return product

@app.put("/products/{product_id}", response_model=ProductResponse, tags=["Products"])
def update_product(product_id: int, product_update: ProductUpdate, db: Session = Depends(get_db)):
    db_product = db.query(Product).filter(Product.productId == product_id).first()
    if not db_product:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Product with ID {product_id} not found."
        )
    
    update_data = product_update.model_dump(exclude_unset=True)
    if "SKU" in update_data and update_data["SKU"] != db_product.SKU:
        existing_sku = db.query(Product).filter(Product.SKU == update_data["SKU"]).first()
        if existing_sku:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Product with SKU {update_data['SKU']} already exists."
            )

    for key, value in update_data.items():
        setattr(db_product, key, value)
        
    db.commit()
    db.refresh(db_product)
    return db_product

@app.patch("/products/{product_id}/stock", response_model=ProductResponse, tags=["Products"])
def update_product_stock(product_id: int, stock_update: ProductStockUpdate, db: Session = Depends(get_db)):
    db_product = db.query(Product).filter(Product.productId == product_id).first()
    if not db_product:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Product with ID {product_id} not found."
        )
    
    db_product.stockQuantity = stock_update.stockQuantity
    db.commit()
    db.refresh(db_product)
    return db_product

@app.delete("/products/{product_id}", status_code=status.HTTP_200_OK, tags=["Products"])
def delete_product(product_id: int, db: Session = Depends(get_db)):
    db_product = db.query(Product).filter(Product.productId == product_id).first()
    if not db_product:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Product with ID {product_id} not found."
        )
    db.delete(db_product)
    db.commit()
    return {"detail": f"Product {product_id} successfully deleted."}

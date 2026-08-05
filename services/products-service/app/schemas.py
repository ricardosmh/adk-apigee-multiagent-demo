from pydantic import BaseModel, Field, ConfigDict
from typing import Optional
from decimal import Decimal

class ProductBase(BaseModel):
    productName: str
    price: Decimal = Field(..., gt=0, description="Price of the product")
    description: Optional[str] = None
    SKU: str
    category: Optional[str] = None
    brand: Optional[str] = None
    stockQuantity: Optional[int] = 0
    imageUrl: Optional[str] = None
    dimension_height: Optional[str] = None
    dimension_width: Optional[str] = None
    dimension_depth: Optional[str] = None
    weight: Optional[str] = None

class ProductCreate(ProductBase):
    pass

class ProductUpdate(BaseModel):
    productName: Optional[str] = None
    price: Optional[Decimal] = Field(None, gt=0)
    description: Optional[str] = None
    SKU: Optional[str] = None
    category: Optional[str] = None
    brand: Optional[str] = None
    stockQuantity: Optional[int] = None
    imageUrl: Optional[str] = None
    dimension_height: Optional[str] = None
    dimension_width: Optional[str] = None
    dimension_depth: Optional[str] = None
    weight: Optional[str] = None

class ProductStockUpdate(BaseModel):
    stockQuantity: int

class ProductResponse(ProductBase):
    productId: int

    model_config = ConfigDict(from_attributes=True)

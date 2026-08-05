from pydantic import BaseModel, ConfigDict
from typing import List, Optional
from datetime import date
from decimal import Decimal

class OrderItemCreate(BaseModel):
    productId: int
    quantity: int

class OrderCreate(BaseModel):
    customerId: int
    items: List[OrderItemCreate]

class OrderStatusUpdate(BaseModel):
    orderStatus: str
    trackingNumber: Optional[str] = None

class OrderItemResponse(BaseModel):
    id: int
    orderId: Optional[int] = None
    productId: Optional[int] = None
    quantity: Optional[int] = None
    price: Optional[Decimal] = None

    model_config = ConfigDict(from_attributes=True)

class OrderResponse(BaseModel):
    orderId: int
    orderDate: Optional[date] = None
    totalAmount: Optional[Decimal] = None
    customerId: Optional[int] = None
    orderStatus: Optional[str] = None
    trackingNumber: Optional[str] = None
    items: List[OrderItemResponse] = []

    model_config = ConfigDict(from_attributes=True)

from pydantic import BaseModel, EmailStr, ConfigDict
from typing import Optional
from datetime import date, datetime

class CustomerBase(BaseModel):
    firstName: Optional[str] = None
    lastName: Optional[str] = None
    dateOfBirth: Optional[date] = None
    gender: Optional[str] = None
    email: Optional[EmailStr] = None
    phone: Optional[str] = None
    
    shipping_street: Optional[str] = None
    shipping_city: Optional[str] = None
    shipping_state: Optional[str] = None
    shipping_postalCode: Optional[str] = None
    shipping_country: Optional[str] = None
    
    billing_street: Optional[str] = None
    billing_city: Optional[str] = None
    billing_state: Optional[str] = None
    billing_postalCode: Optional[str] = None
    billing_country: Optional[str] = None
    
    username: Optional[str] = None
    accountStatus: Optional[str] = "active"

class CustomerCreate(CustomerBase):
    email: EmailStr
    username: str

class CustomerUpdate(CustomerBase):
    pass

class CustomerResponse(CustomerBase):
    customerId: int
    creationDate: Optional[date] = None
    lastLogin: Optional[datetime] = None

    model_config = ConfigDict(from_attributes=True)

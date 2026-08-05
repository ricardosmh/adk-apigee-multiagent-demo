from sqlalchemy import Column, Integer, String, Date, DateTime
from app.database import Base

class Customer(Base):
    __tablename__ = "customers"

    customerId = Column(Integer, primary_key=True, index=True, autoincrement=True)
    firstName = Column(String(255), nullable=True)
    lastName = Column(String(255), nullable=True)
    dateOfBirth = Column(Date, nullable=True)
    gender = Column(String(50), nullable=True)
    email = Column(String(255), unique=True, index=True, nullable=True)
    phone = Column(String(50), nullable=True)
    
    shipping_street = Column(String(255), nullable=True)
    shipping_city = Column(String(100), nullable=True)
    shipping_state = Column(String(100), nullable=True)
    shipping_postalCode = Column(String(20), nullable=True)
    shipping_country = Column(String(100), nullable=True)
    
    billing_street = Column(String(255), nullable=True)
    billing_city = Column(String(100), nullable=True)
    billing_state = Column(String(100), nullable=True)
    billing_postalCode = Column(String(20), nullable=True)
    billing_country = Column(String(100), nullable=True)
    
    username = Column(String(255), unique=True, index=True, nullable=True)
    creationDate = Column(Date, nullable=True)
    lastLogin = Column(DateTime, nullable=True)
    accountStatus = Column(String(50), nullable=True)
